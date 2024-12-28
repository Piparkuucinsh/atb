import pytz
from datetime import datetime, time
import asyncio
from telegram import Update
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
            
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                task TEXT NOT NULL,
                status TEXT,
                message_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        db.commit()

# Modify the channel configuration functions
async def link_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link a channel to the bot via DM"""
    if not update.message or update.message.chat.type != 'private':
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "Please provide the channel ID/username.\n"
            "Usage: /link_channel @channel_name [timezone]"
        )
        return

    channel_id = context.args[0]
    
    # Check if timezone was provided
    timezone_str = TIMEZONE.zone
    if len(context.args) > 1:
        try:
            timezone = pytz.timezone(context.args[1])
            timezone_str = timezone.zone
        except pytz.exceptions.UnknownTimeZoneError:
            await update.message.reply_text(
                "Invalid timezone! Please use a valid timezone name (e.g., 'Europe/Paris', 'America/New_York').\n"
                "See full list here: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
            )
            return

    try:
        # Try to send a test message to verify bot's access
        chat = await context.bot.get_chat(channel_id)
        test_message = await context.bot.send_message(
            chat.id,
            "🔄 Testing bot permissions..."
        )
        await test_message.delete()
        
        # If successful, store channel configuration
        with get_db() as db:
            db.execute(
                'INSERT OR REPLACE INTO channels (channel_id, timezone) VALUES (?, ?)',
                (chat.id, timezone_str)
            )
            db.commit()
        
        await update.message.reply_text(
            f"✅ Successfully linked to channel: {chat.title}\n"
            f"Timezone set to: {timezone_str}\n"
            "Daily accountability tracking will start tomorrow at 6 AM in your timezone."
        )
        
        await context.bot.send_message(
            chat.id,
            "✨ Accountability bot successfully connected!\n"
            f"Using timezone: {timezone_str}\n"
            "Daily tracking will start tomorrow at 6 AM."
        )
        
    except TelegramError as e:
        await update.message.reply_text(
            "❌ Failed to link channel. Please ensure:\n"
            "1. The channel ID/username is correct\n"
            "2. I am an admin in the channel\n"
            "3. I have permission to send messages\n\n"
            f"Error: {str(e)}"
        )

async def start_daily_routine(context: ContextTypes.DEFAULT_TYPE):
    """Posts daily message with member names and starts tracking"""
    logger.info("Starting daily routine")
    with get_db() as db:
        channels = db.execute('SELECT channel_id, timezone FROM channels').fetchall()
        
    for channel in channels:
        try:
            # Get channel members
            chat = await context.bot.get_chat(channel['channel_id'])
            members = await chat.get_administrators()
            
            timezone = pytz.timezone(channel['timezone'])
            # Create and send daily message
            message = "🌟 Daily Accountability - {}\n\n".format(
                datetime.now(timezone).strftime("%Y-%m-%d")
            )
            for member in members:
                if not member.user.is_bot:
                    message += f"@{member.user.username}\n"

            await context.bot.send_message(channel['channel_id'], message)
            logger.info(f"Posted daily message to channel {channel['channel_id']}")

        except TelegramError as e:
            logger.exception(f"Error in daily routine for channel {channel['channel_id']}: {e}")

    # Clear previous day's tasks
    with get_db() as db:
        db.execute('DELETE FROM tasks')
        db.commit()

async def handle_task_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user comments with their daily tasks"""
    message = update.message
    if message is None or message.from_user is None:
        return
    user_id = message.from_user.id

    if (message.reply_to_message and 
        message.reply_to_message.from_user and 
        message.reply_to_message.from_user.id == context.bot.id):
        
        task = message.text
        with get_db() as db:
            db.execute(
                'INSERT INTO tasks (user_id, task, message_id) VALUES (?, ?, ?)',
                (user_id, task, message.message_id)
            )
            db.commit()

        # Schedule reminders
        for hours in range(3, 24, 3):
            if context.job_queue is None:
                return
            context.job_queue.run_once(
                send_reminder,
                datetime.now(TIMEZONE).replace(hour=hours, minute=0, second=0).time(),
                data={"user_id": user_id, "task": task},
            )

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Send reminder DM to user"""
    job = context.job

    if job is None or job.data is None or type(job.data) != dict:
        logger.warning("Invalid job data in send_reminder")
        return

    user_id = job.data["user_id"]
    task = job.data["task"]

    try:
        await context.bot.send_message(
            user_id,
            f"Reminder for your task:\n{task}\n\nReply with ✅ for success or ❌ for failure",
        )
        logger.info(f"Sent reminder to user {user_id}")
    except TelegramError as e:
        logger.exception(f"Error sending reminder to user {user_id}: {e}")

async def handle_status_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle task status updates"""
    message = update.message
    if message is None or message.from_user is None:
        return

    user_id = message.from_user.id

    if message.reply_to_message:
        status_text = message.text
        if status_text == "✅":
            status = "completed"
        elif status_text == "❌":
            status = "failed"
        else:
            return

        # Update task status
        with get_db() as db:
            db.execute(
                'UPDATE tasks SET status = ? WHERE user_id = ? AND message_id = ?',
                (status, user_id, message.reply_to_message.message_id)
            )
            db.commit()

async def post_daily_recap(context):
    """Post recap of failed tasks"""
    with get_db() as db:
        channels = db.execute('SELECT channel_id FROM channels').fetchall()
        
    for channel in channels:
        with get_db() as db:
            failed_tasks = db.execute('''
                SELECT user_id, task FROM tasks 
                WHERE status = 'failed'
            ''').fetchall()
            
        if failed_tasks:
            recap_lines = []
            for task in failed_tasks:
                try:
                    user = await context.bot.get_chat(task['user_id'])
                    recap_lines.append(f"@{user.username}: {task['task']}")
                except TelegramError as e:
                    logger.exception(f"Error in daily recap for user {task['user_id']}: {e}")
                    
            if recap_lines:
                recap = "📊 Yesterday's Incomplete Tasks:\n\n" + "\n".join(recap_lines)
                await context.bot.send_message(channel['channel_id'], recap)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle start command in DM or channel"""
    if not update.message:
        return
        
    if update.message.chat.type == 'private':
        await update.message.reply_text(
            "👋 Hello! I'm an accountability bot.\n\n"
            "To add me to your channel:\n"
            "1. Add me as an admin to your channel\n"
            "2. Send /link_channel <channel_id> [timezone] here\n\n"
            "Example: /link_channel @mychannel Europe/Riga\n"
            "Timezone is optional (default: Europe/Riga)"
        )
        return
        
    if update.message.chat.type in ['group', 'supergroup', 'channel']:
        channel_id = update.message.chat.id
        
        # Check if timezone was provided
        timezone_str = TIMEZONE.zone
        if context.args and context.args[0]:
            try:
                timezone = pytz.timezone(context.args[0])
                timezone_str = timezone.zone
            except pytz.exceptions.UnknownTimeZoneError:
                await update.message.reply_text(
                    "Invalid timezone! Please use a valid timezone name (e.g., 'Europe/Paris', 'America/New_York').\n"
                    "See full list here: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
                )
                return

        # Store channel configuration in database
        with get_db() as db:
            db.execute(
                'INSERT OR REPLACE INTO channels (channel_id, timezone) VALUES (?, ?)',
                (channel_id, timezone_str)
            )
            db.commit()
        
        await update.message.reply_text(
            f"Bot initialized for this channel with timezone: {timezone_str}!\n"
            "Daily accountability tracking will start tomorrow at 6 AM in your timezone."
        )

def main():
    """Start the bot"""
    logger.info("Initializing bot")
    # Initialize database
    init_db()
    
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set in the environment variables")
        raise ValueError("TELEGRAM_BOT_TOKEN is not set in the environment variables")

    application = Application.builder().token(TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("link_channel", link_channel))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_task_comment)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(r"^[✅❌]$"), handle_status_update)
    )

    # Schedule daily routines
    job_queue = application.job_queue

    if job_queue is None:
        return

    daily_time = time(6, 0, tzinfo=TIMEZONE)  # 6 AM UTC+2

    # Schedule recap before new daily message
    job_queue.run_daily(post_daily_recap, daily_time.replace(minute=59))
    # Schedule new daily message
    job_queue.run_daily(start_daily_routine, daily_time)

    # Start the bot
    logger.info("Bot started successfully")
    application.run_polling()


if __name__ == "__main__":
    main()
