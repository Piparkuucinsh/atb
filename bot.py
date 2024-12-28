import pytz
from datetime import datetime, time
import asyncio
from telegram import Update
import telegram
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError
import sqlite3
from contextlib import contextmanager
from dotenv import load_dotenv
import os
from loguru import logger
from collections import defaultdict

load_dotenv()

# Bot configuration
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

TIMEZONE = pytz.timezone("Europe/Riga")

@contextmanager
def get_db():
    """Context manager for database connections"""
    db = sqlite3.connect('accountability.db')
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initialize the database schema"""
    with get_db() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL
            );
            
            CREATE TABLE IF NOT EXISTS daily_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                UNIQUE(channel_id, date)
            );
            
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (channel_id) REFERENCES channels (channel_id)
            );
        ''')
        db.commit()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle start command - only works in channels"""
    # logger.debug(f"Start command received from {update.message.from_user.username}")
    message = update.message or update.channel_post
    if not message:
        logger.warning("No message object found in update")
        return
        
    if message.chat.type != 'channel':
        await context.bot.send_message(
            message.chat.id,
            "❌ This bot only works in channels. Please add me to a channel as an admin."
        )
        return
        
    channel_id = message.chat.id
    try:
        # Test bot permissions
        test_message = await context.bot.send_message(
            channel_id,
            "🔄 Testing bot permissions..."
        )
        await test_message.delete()
        
        # Store channel configuration with default timezone
        with get_db() as db:
            db.execute(
                'INSERT OR REPLACE INTO channels (channel_id, timezone) VALUES (?, ?)',
                (channel_id, TIMEZONE.zone)
            )
            db.commit()
        
        await context.bot.send_message(
            channel_id,
            "✨ Accountability bot activated!\n"
            f"Using timezone: {TIMEZONE.zone}\n"
            "Daily tracking will start tomorrow at 6 AM.\n\n"
            "💡 Channel members can comment on daily posts with their tasks.\n"
            "Admins can change timezone with: /timezone Europe/Paris"
        )
        logger.info(f"Bot activated in channel {channel_id}")
        
    except TelegramError as e:
        error_message = (
            "❌ Activation failed. Please make sure I am an admin with these permissions:\n"
            "• Send Messages\n"
            "• Delete Messages\n"
            f"\nError: {str(e)}"
        )
        try:
            await context.bot.send_message(channel_id, error_message)
        except TelegramError as e2:
            logger.error(f"Could not send error message: {e2}")
        logger.error(f"Activation failed in channel {channel_id}: {e}")

async def set_timezone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set channel timezone"""
    if not update.message or not context.args:
        return

    channel_id = update.message.chat.id
    
    try:
        new_timezone = pytz.timezone(context.args[0])
        with get_db() as db:
            db.execute(
                'UPDATE channels SET timezone = ? WHERE channel_id = ?',
                (new_timezone.zone, channel_id)
            )
            db.commit()
        
        await update.message.reply_text(
            f"✅ Timezone updated to: {new_timezone.zone}"
        )
        logger.info(f"Timezone updated for channel {channel_id}: {new_timezone.zone}")
        
    except pytz.exceptions.UnknownTimeZoneError:
        await update.message.reply_text(
            "❌ Invalid timezone! Examples:\n"
            "• /timezone Europe/London\n"
            "• /timezone America/New_York\n"
            "• /timezone Asia/Tokyo\n\n"
            "Full list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )

async def start_daily_routine(context: ContextTypes.DEFAULT_TYPE):
    """Posts individual daily messages for each channel member"""
    logger.info("Starting daily routine")
    with get_db() as db:
        channels = db.execute('SELECT channel_id, timezone FROM channels').fetchall()
        
    for channel in channels:
        try:
            timezone = pytz.timezone(channel['timezone'])
            current_date = datetime.now(timezone).strftime("%Y-%m-%d")
            
            # Get channel members who are not bots
            try:
                chat_members = await context.bot.get_chat_administrators(channel['channel_id'])
                
                for member in chat_members:
                    if member.user.is_bot:
                        continue
                        
                    mention = f"@{member.user.username}" if member.user.username else member.user.first_name
                    
                    # Create personalized message
                    message = (
                        f"👋 Hey {mention}!\n"
                        f"🌟 Daily Accountability - {current_date}\n\n"
                        "What are your main tasks for today?\n"
                        "Comment on this message with your tasks in the format:\n"
                        "task: <task description>\n\n"
                        "✅ Mark tasks as done by replying to the comment with a ✅ \n"
                        "❌ Mark tasks as failed by replying to the comment with a ❌"
                    )

                    sent_message = await context.bot.send_message(
                        channel['channel_id'], 
                        message,
                        allow_sending_without_reply=True
                    )
                    
                    # Store the daily message ID for tracking replies
                    with get_db() as db:
                        db.execute('''
                            INSERT INTO daily_messages (channel_id, message_id, date)
                            VALUES (?, ?, ?)
                        ''', (channel['channel_id'], sent_message.message_id, current_date))
                        db.commit()
                        
                    # Add small delay between messages to avoid flooding
                    await asyncio.sleep(1)

                logger.info(f"Posted individual daily messages in channel {channel['channel_id']}")

            except TelegramError as e:
                logger.exception(f"Failed to get members for channel {channel['channel_id']}: {e}")

        except TelegramError as e:
            logger.exception(f"Error in daily routine for channel {channel['channel_id']}: {e}")

async def handle_task_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user comments on daily messages"""
    message = update.message
    if not message or not message.reply_to_message:
        return
    
    # Check if this is a status update (✅ or ❌)
    if message.text in ['✅', '❌']:
        await handle_task_status(message, context)
        return
    
    # Check if the comment is replying to a daily message
    with get_db() as db:
        daily_message = db.execute('''
            SELECT * FROM daily_messages 
            WHERE channel_id = ? AND message_id = ?
        ''', (message.chat.id, message.reply_to_message.message_id)).fetchone()

    # Check if message starts with "task:"
    if not message.text or not message.text.lower().startswith('task:'):
        return

    # Remove "task:" prefix and strip whitespace
    task_text = message.text[5:].strip()
    if not task_text:
        try:
            await message.reply_text("❌ Please provide a task description after 'task:'")
            await message.delete()
        except TelegramError:
            pass
        return

    if not daily_message:
        return

    # Check if the original message mentions the user
    user = message.from_user
    if user is None:
        return

    original_message = message.reply_to_message.text
    user_mention = f"@{user.username}" if user.username else user.first_name

    if not original_message:
        return
    
    # Only allow task creation if the daily message was meant for this user
    if user_mention not in original_message:
        try:
            await message.reply_text("❌ You can only add tasks to your own daily message!")
            await message.delete()
        except TelegramError:
            logger.exception("Failed to send task creation error message")
            pass
        return

    # Store the task
    with get_db() as db:
        db.execute('''
            INSERT INTO tasks (user_id, channel_id, task, message_id, created_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (user.id, message.chat.id, task_text, message.message_id))
        db.commit()

    logger.info(f"Stored task for user {user.id} in channel {message.chat.id}")

async def handle_task_status(message, context: ContextTypes.DEFAULT_TYPE):
    """Handle task completion status updates"""
    if not message.reply_to_message:
        return

    with get_db() as db:
        # Find the original task
        task = db.execute('''
            SELECT * FROM tasks 
            WHERE message_id = ? AND channel_id = ? AND user_id = ?
        ''', (message.reply_to_message.message_id, message.chat.id, message.from_user.id)).fetchone()

        if task:
            # Update task status
            status = 'completed' if message.text == '✅' else 'failed'
            db.execute('''
                UPDATE tasks 
                SET status = ? 
                WHERE message_id = ? AND channel_id = ? AND user_id = ?
            ''', (status, message.reply_to_message.message_id, message.chat.id, message.from_user.id))
            db.commit()
            
            # Add reaction to confirm status update
            try:
                await message.reply_to_message.react('👍')
            except TelegramError:
                logger.exception("Failed to add reaction to task status update")
                pass

async def send_daily_recap(context: ContextTypes.DEFAULT_TYPE):
    """Send recap of completed and failed tasks before the next day starts"""
    logger.info("Sending daily recap")
    
    with get_db() as db:
        channels = db.execute('SELECT channel_id, timezone FROM channels').fetchall()
        
    for channel in channels:
        try:
            timezone = pytz.timezone(channel['timezone'])
            current_date = datetime.now(timezone).strftime("%Y-%m-%d")
            
            # Get all tasks for the day
            with get_db() as db:
                tasks = db.execute('''
                    SELECT t.*, dm.date
                    FROM tasks t
                    JOIN daily_messages dm ON t.channel_id = dm.channel_id
                    WHERE dm.date = ? AND t.channel_id = ?
                ''', (current_date, channel['channel_id'])).fetchall()

            # Group tasks by user
            user_tasks = defaultdict(lambda: {'completed': [], 'failed': [], 'pending': []})
            for task in tasks:
                user_tasks[task['user_id']][task['status']].append(task['task'])

            # Create recap message
            recap = f"📊 Daily Recap - {current_date}\n\n"
            
            for user_id, status in user_tasks.items():
                completed = len(status['completed'])
                failed = len(status['failed'])
                pending = len(status['pending'])
                total = completed + failed + pending
                
                try:
                    # Get user info directly from Telegram
                    chat_member = await context.bot.get_chat_member(channel['channel_id'], user_id)
                    user_mention = f"@{chat_member.user.username}" if chat_member.user.username else chat_member.user.first_name
                    
                    recap += f"{user_mention}'s Progress:\n"
                    recap += f"✅ Completed: {completed}/{total}\n"
                    if failed > 0:
                        recap += f"❌ Failed: {failed}\n"
                    if pending > 0:
                        recap += f"⏳ Pending: {pending}\n"
                    
                    if failed > 0:
                        recap += "Failed tasks:\n"
                        for task in status['failed']:
                            recap += f"• {task}\n"
                    
                    recap += "\n"
                except TelegramError:
                    logger.error(f"Could not get user info for user_id {user_id}")
                    continue

            await context.bot.send_message(
                channel['channel_id'],
                recap
            )

        except TelegramError as e:
            logger.exception(f"Error sending recap for channel {channel['channel_id']}: {e}")

async def setup_commands(context: ContextTypes.DEFAULT_TYPE):
    """Set up bot commands in Telegram"""
    commands = [
        ("start", "Start the accountability bot in this channel"),
        ("timezone", "Set the timezone for this channel (e.g., /timezone Europe/London)")
    ]
    
    # Set commands for channels
    await context.bot.set_my_commands(
        commands,
        scope=telegram.BotCommandScopeAllChatAdministrators()  # Makes commands visible to admins in all chats
    )
    logger.info("Bot commands configured")

def main():
    """Start the bot"""
    logger.info("Initializing bot")
    init_db()
    
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in the environment variables")
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in the environment variables")

    application = Application.builder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler(
        "start", 
        start, 
        filters.ChatType.CHANNEL | filters.UpdateType.CHANNEL_POST
    ))
    application.add_handler(CommandHandler(
        "timezone", 
        set_timezone, 
        filters.ChatType.CHANNEL | filters.UpdateType.CHANNEL_POST
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.ChatType.CHANNEL, 
        handle_task_comment
    ))

    if not application.job_queue:
        logger.error("Job queue is not initialized")
        return

    # Set up commands
    application.job_queue.run_once(setup_commands, 1)  # Run after 1 second to ensure bot is fully started

    # Schedule daily routines
    job_queue = application.job_queue
    if job_queue is None:
        return

    # Schedule new daily message at 6 AM in default timezone
    job_queue.run_daily(
        start_daily_routine, 
        time(6, 0, tzinfo=TIMEZONE)
    )

    # Schedule daily recap at 11:30 PM in channel timezone
    job_queue.run_daily(
        send_daily_recap,
        time(5, 55, tzinfo=TIMEZONE)
    )

    logger.info("Bot started successfully")
    application.run_polling()


if __name__ == "__main__":
    main()
