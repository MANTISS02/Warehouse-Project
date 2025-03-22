import logging 
import asyncio
import platform
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram import Update, BotCommand
from config import TELEGRAM_TOKEN
from database import init_db, add_scan_history
from bot_handlers import (
    start, help_command, find_command, 
    recent_command, clear_command, search_command,
    scan_command, handle_message, last_scan_report_command
)
import signal
import sys
import subprocess

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

application = None

def signal_handler(signum, frame):
    logger.info("Получен сигнал завершения. Останавливаем бота...")
    if application:
        application.stop()
    sys.exit(0)

async def shutdown(signal, loop):
    logger.info(f"Получен сигнал {signal.name}...")
    
    try:
        if platform.system() == 'Windows':
            subprocess.run('taskkill /F /FI "WINDOWTITLE eq flight.py*"', shell=True)
        else:
            subprocess.run("pkill -f 'python.*flight.py'", shell=True)
        
        add_scan_history("scan", None, "stopped", None)
    except Exception as e:
        logger.error(f"Ошибка при остановке процессов сканирования: {str(e)}")
    
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    logger.info(f"Отмена {len(tasks)} задач")
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

def setup_signal_handlers():
    if platform.system() != 'Windows':
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGQUIT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(shutdown(s, loop))
            )
    else:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGBREAK):
            signal.signal(sig, signal_handler)

async def setup_commands(application: Application):
    commands = [
        BotCommand("start", "Запустить бота и показать главное меню"),
        BotCommand("help", "Показать справку"),
        BotCommand("find", "Найти предмет по ID или названию"),
        BotCommand("search", "Поиск по расположению"),
        BotCommand("recent", "Показать историю операций"),
        BotCommand("scan", "Запустить сканирование"),
        BotCommand("last_scan_report", "Отчет о последнем сканировании"),
        BotCommand("clear", "Очистить историю")
    ]
    await application.bot.set_my_commands(commands)

def run():
    global application
    
    try:
        init_db(check_existing=True)
        logger.info("База данных успешно инициализирована")
    except Exception as e:
        logger.error(f"Ошибка при инициализации базы данных: {str(e)}")
        raise
    
    if not TELEGRAM_TOKEN:
        raise ValueError("TELEGRAM_TOKEN не может быть пустым. Проверьте файл .env")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    asyncio.get_event_loop().run_until_complete(setup_commands(application))
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("find", find_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("clear", clear_command))
    application.add_handler(CommandHandler("scan", scan_command))
    application.add_handler(CommandHandler("last_scan_report", last_scan_report_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    try:
        setup_signal_handlers()
        
        logger.info("Запуск бота...")
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except KeyboardInterrupt:
        logger.info("Получен сигнал прерывания...")
    finally:
        if application:
            application.stop()
        logger.info("Бот остановлен")

if __name__ == '__main__':
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Произошла ошибка: {str(e)}")
        logger.exception(e)
        raise 