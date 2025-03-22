from telegram import Bot, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CallbackContext, ContextTypes, CommandHandler, MessageHandler, filters
from database import (
    find_item, get_shelf_items, get_recent_scans, 
    add_scan_history, Item, clear_scan_history, search_items, get_all_items,
    create_scan_session, end_scan_session, get_last_successful_session, init_db, get_last_successful_sessions,
    ScanSession, Session
)
from typing import cast
import asyncio
import logging
import subprocess
import sys
import os
import time
from flight import ARUCO_LOCATIONS
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

LOCATION_TO_ARUCO = {}
for aruco_id, (shelf, position) in ARUCO_LOCATIONS.items():
    location = (f"Стеллаж {shelf}", f"Полка {position}")
    if location not in LOCATION_TO_ARUCO:
        LOCATION_TO_ARUCO[location] = []
    LOCATION_TO_ARUCO[location].append(aruco_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    if not update.message:
        return
        
    keyboard = [
        [KeyboardButton("🔍 Поиск предметов"), KeyboardButton("📦 Поиск по расположению")],
        [KeyboardButton("📋 История"), KeyboardButton("🎥 Сканировать")],
        [KeyboardButton("📊 Отчет о сканировании"), KeyboardButton("❓ Помощь")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # win + . для смайлов
    welcome_text = """
👋 Привет! Я бот для автоматизации поиска и учёта предметов на складе.

Используйте кнопки меню для навигации:

🔍 Поиск предметов - поиск по ID или названию
📦 Поиск по расположению - поиск по стеллажу/полке
📋 История - последние операции сканирования
🎥 Сканировать - запуск сканирования QR-кодов
📊 Отчет о сканировании - результаты последнего сканирования
❓ Помощь - подробная справка
    """
    await update.message.reply_text(welcome_text, reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    if not update.message:
        return
        
    locations = set()
    for shelf, position in ARUCO_LOCATIONS.values():
        locations.add(f"Стеллаж {shelf}, Полка {position}")
    locations_str = "\n".join([f"• {loc}" for loc in sorted(locations)])
        
    help_text = f"""
📖 Подробная справка по использованию бота:

🔍 Поиск предметов:
• Нажмите кнопку "🔍 Поиск предметов"
• Введите ID (для точного поиска) или название предмета
• Примеры: 12345 или "ноутбук"

📦 Поиск по расположению:
• Нажмите кнопку "📦 Поиск по расположению"
• Выберите стеллаж или полку из доступных вариантов:
{locations_str}

📋 История операций:
• Нажмите кнопку "📋 История" для просмотра последних операций сканирования

🎥 Сканирование QR-кодов:
• Нажмите кнопку "🎥 Сканировать" для запуска сканирования
• Окно с камерой откроется на компьютере
• Для завершения сканирования нажмите ESC в окне с камерой

📊 Отчет о сканировании:
• Нажмите кнопку "📊 Отчет о сканировании" для просмотра результатов последнего успешного сканирования

❗️ Примечания:
• При поиске по ID требуется точное совпадение
• При поиске по названию достаточно части слова
• Поиск не чувствителен к регистру
• Местоположение определяется автоматически по ArUco маркерам
    """
    await update.message.reply_text(help_text)

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /search"""
    if not update.message:
        return
        
    if not context.args:
        locations = set()
        for shelf, position in ARUCO_LOCATIONS.values():
            locations.add(f"Стеллаж {shelf}, Полка {position}")
        locations_str = "\n".join([f"• {loc}" for loc in sorted(locations)])
        
        help_text = f"""
Пожалуйста, укажите параметры поиска:
/search shelf 1 - поиск на стеллаже 1
/search pos 2 - поиск на полке 2
/search all - показать все предметы

Доступные местоположения:
{locations_str}
        """
        await update.message.reply_text(help_text)
        return

    try:
        if len(context.args) == 1 and context.args[0].lower() == 'all':
            items = get_all_items()
            if items:
                response = "Все предметы в базе данных:\n\n"
                for item in items:
                    response += f"- {item.name} (ID: {item.qr_code})\n"
                    response += f"  Расположение: {item.shelf}, {item.position}\n"
                add_scan_history("search", None, "list_all")
            else:
                response = "База данных пуста"
                add_scan_history("search", None, "empty_db")
            
            await update.message.reply_text(response)
            return

        shelf = None
        position = None
        i = 0
        while i < len(context.args):
            if context.args[i].lower() == 'shelf' and i + 1 < len(context.args):
                shelf = context.args[i + 1]
                i += 2
            elif context.args[i].lower() == 'pos' and i + 1 < len(context.args):
                position = context.args[i + 1]
                i += 2
            else:
                i += 1

        # Выполняем поиск
        items = search_items(shelf, position)
        
        if items:
            response = "Найденные предметы:\n\n"
            for item in items:
                # Находим ArUco ID для этого местоположения
                location = (item.shelf, item.position)
                aruco_ids = LOCATION_TO_ARUCO.get(location, [])
                aruco_str = f" (ArUco ID: {', '.join(map(str, aruco_ids))})" if aruco_ids else ""
                
                response += f"- {item.name} (ID: {item.qr_code})\n"
                response += f"  Расположение: {item.shelf}, {item.position}{aruco_str}\n"
            
            # Добавляем запись в историю
            search_params = []
            if shelf:
                search_params.append(f"стеллаж {shelf}")
            if position:
                search_params.append(f"полка {position}")
            add_scan_history("search", None, f"found: {', '.join(search_params)}")
        else:
            search_params = []
            if shelf:
                search_params.append(f"стеллаж {shelf}")
            if position:
                search_params.append(f"полка {position}")
            response = f"Предметы не найдены (параметры поиска: {', '.join(search_params)})"
            add_scan_history("search", None, "not_found")
        
        await update.message.reply_text(response)
                
    except Exception as e:
        error_msg = f"Произошла ошибка при выполнении поиска: {str(e)}"
        logger.error(error_msg)
        await update.message.reply_text("Произошла ошибка при выполнении поиска. Пожалуйста, попробуйте позже.")

async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /recent - показывает последние операции"""
    if not update.message:
        return

    try:
        text = update.message.text if update.message.text else ""
        
        # Если команда вызвана напрямую через /recent
        if text == "/recent":
            keyboard = [
                [KeyboardButton("📋 Последние 10 операций")],
                [KeyboardButton("📋 Вся история")],
                [KeyboardButton("🔙 Вернуться в главное меню")]
            ]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text(
                "Выберите количество операций для отображения:",
                reply_markup=reply_markup
            )
            return
            
        show_all = text == "📋 Вся история"
        limit = None if show_all else 10
        recent_scans = get_recent_scans(limit)
        
        if not recent_scans:
            await update.message.reply_text("История операций пуста")
            return
            
        response = f"📋 {'Вся история' if show_all else 'Последние 10'} операций:\n\n"
        
        db_session = Session()
        try:
            # Группируем сканирования по сессиям
            current_session = None
            
            for scan in recent_scans:
                if scan.session_uuid != current_session:
                    current_session = scan.session_uuid
                
                # Конвертируем время в МСК (UTC+3)
                msk_time = scan.timestamp + timedelta(hours=3)
                time_str = msk_time.strftime("%d.%m.%Y %H:%M")
                
                operation_type = {
                    'scan': 'Сканирование QR',
                    'find': 'Поиск предмета',
                    'search': 'Поиск по расположению',
                    'clear': 'Очистка истории'
                }.get(scan.operation, scan.operation)
                
                if scan.result.startswith('success'):
                    if scan.item_id:
                        item = db_session.query(Item).get(scan.item_id)
                        if item:
                            result = f"✅ Успешно: {item.name} (QR: {item.qr_code})"
                        else:
                            result = "✅ Успешно"
                    else:
                        result = "✅ Успешно"
                elif scan.result.startswith('not_found'):
                    search_term = scan.result.split(': ')[-1] if ': ' in scan.result else ''
                    result = f"❌ Не найдено{f' ({search_term})' if search_term else ''}"
                elif scan.result == 'stopped':
                    result = "🛑 Остановлено"
                else:
                    result = f"ℹ️ {scan.result}"
                
                session_info = ""
                if scan.session_uuid:
                    scan_session = db_session.query(ScanSession).filter_by(session_uuid=scan.session_uuid).first()
                    if scan_session:
                        session_info = f" (Сессия: {scan_session.session_uuid[:8]})"
                
                response += f"🕒 {time_str}\n"
                response += f"📌 {operation_type}{session_info}\n"
                response += f"{result}\n\n"
        finally:
            db_session.close()
        
        keyboard = [
            [KeyboardButton("🔍 Поиск предметов"), KeyboardButton("📦 Поиск по расположению")],
            [KeyboardButton("📋 История"), KeyboardButton("🎥 Сканировать")],
            [KeyboardButton("📊 Отчет о сканировании"), KeyboardButton("❓ Помощь")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        
        max_length = 4096
        if len(response) > max_length:
            parts = [response[i:i + max_length] for i in range(0, len(response), max_length)]
            for i, part in enumerate(parts):
                if i == len(parts) - 1:
                    await update.message.reply_text(part, reply_markup=reply_markup)
                else:
                    await update.message.reply_text(part)
        else:
            await update.message.reply_text(response, reply_markup=reply_markup)
        
    except Exception as e:
        error_msg = f"Ошибка при получении истории: {str(e)}"
        logger.error(error_msg)
        await update.message.reply_text("Произошла ошибка при получении истории. Пожалуйста, попробуйте позже.")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /clear"""
    if not update.message:
        return
        
    if clear_scan_history():
        await update.message.reply_text("История сканирования успешно очищена!")
    else:
        await update.message.reply_text("Произошла ошибка при очистке истории сканирования.")

async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /find"""
    if not update.message:
        return
        
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажите ID или название предмета\nПример: /find 12345")
        return

    search_query = ' '.join(context.args)
    item = find_item(search_query)
    
    if item:
        response = f"Предмет найден!\nНазвание: {item.name}\nРасположение: {item.shelf}, {item.position}"
        item_id = cast(int, item.id) if isinstance(item, Item) else None
        add_scan_history("find", item_id, "success")
    else:
        response = "Предмет не найден"
        add_scan_history("find", None, f"not_found: {search_query}")
    
    await update.message.reply_text(response)

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запуск сканирования QR-кодов"""
    try:
        # Проверяем состояние базы данных
        try:
            init_db(check_existing=True)
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка базы данных: {str(e)}")
            return
            
        chat_id = update.effective_chat.id
        scan_session = create_scan_session()
        
        # Путь к скрипту сканирования
        qr_script_path = os.path.join(os.path.dirname(__file__), "flight.py")
        
        if not os.path.exists(qr_script_path):
            await update.message.reply_text(f"❌ Ошибка: Файл {qr_script_path} не найден!")
            return
            
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 1
        
        subprocess.Popen(
            [sys.executable, qr_script_path, str(chat_id), scan_session.session_uuid],
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0,
            startupinfo=startupinfo
        )
        
        await update.message.reply_text("✅ Сканирование запущено. Для завершения нажмите ESC в окне сканирования.")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")
        return

async def last_scan_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /last_scan_report - показывает результаты последних сканирований дрона"""
    if not update.message:
        return
        
    keyboard = [
        [KeyboardButton("📊 Последний отчет"), KeyboardButton("📊 Последние 3 отчета")],
        [KeyboardButton("📊 Последние 5 отчетов"), KeyboardButton("📊 Все отчеты")],
        [KeyboardButton("🔙 Вернуться в главное меню")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if update.message.text == "🔙 Вернуться в главное меню":
        keyboard = [
            [KeyboardButton("🔍 Поиск предметов"), KeyboardButton("📦 Поиск по расположению")],
            [KeyboardButton("📋 История"), KeyboardButton("🎥 Сканировать")],
            [KeyboardButton("📊 Отчет о сканировании"), KeyboardButton("❓ Помощь")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Главное меню:", reply_markup=reply_markup)
        return
    
    text = update.message.text.lower()
    if "последние" in text and any(str(num) in text for num in ["3", "5"]):
        limit = 3 if "3" in text else 5
    elif "все" in text:
        limit = 100
    else:
        limit = 1
    
    sessions_data = get_last_successful_sessions(limit)
    
    if not sessions_data:
        await update.message.reply_text(
            "📊 Не найдено успешных сканирований дрона", 
            reply_markup=reply_markup
        )
        return
    
    # Формируем общий отчет
    response = f"📊 Отчет о последних {len(sessions_data)} сканированиях:\n\n"
    
    for session_num, (session, items) in enumerate(sessions_data, 1):
        if session_num > 1:
            response += "\n" + "=" * 30 + "\n\n"
        
        # Конвертируем время в МСК (UTC+3)
        msk_time = session.start_time + timedelta(hours=3)
        response += f"🕒 Сессия {session_num} ({msk_time.strftime('%d.%m.%Y %H:%M')})\n"
        
        items_by_location = {}
        for item in items:
            location = (item.shelf, item.position)
            if location not in items_by_location:
                items_by_location[location] = []
            items_by_location[location].append(item)
        
        total_items = sum(len(items) for items in items_by_location.values())
        response += f"📦 Успешно отсканировано: {total_items} предметов\n\n"
        
        for location, items in sorted(items_by_location.items()):
            shelf, position = location
            response += f"\n📍 {shelf}, {position}:\n"
            for item in items:
                qr_num = ''.join(filter(str.isdigit, item.qr_code))
                response += f"- {item.name} (QR ID: {qr_num})\n"
    
    max_length = 4096
    if len(response) > max_length:
        parts = [response[i:i + max_length] for i in range(0, len(response), max_length)]
        for i, part in enumerate(parts):
            if i == 0:
                await update.message.reply_text(part, reply_markup=reply_markup)
            else:
                await update.message.reply_text(part)
    else:
        await update.message.reply_text(response, reply_markup=reply_markup)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    if not update.message or not update.message.text:
        return

    text = update.message.text

    if text == "/":
        keyboard = [
            [KeyboardButton("/start Справка")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Выберите команду:", 
            reply_markup=reply_markup
        )
        return

    if text == "/start Справка":
        await start(update, context)
        return

    if text == "🔍 Поиск предметов":
        await update.message.reply_text(
            "🔍 Введите ID или название предмета для поиска\n\n"
            "Примеры:\n"
            "• По ID: 12345\n"
            "• По названию: Ноутбук\n"
        )
    elif text == "📦 Поиск по расположению":
        keyboard = []
        shelves = set()
        positions = set()
        
        for shelf, position in ARUCO_LOCATIONS.values():
            shelves.add(f"Стеллаж {shelf}")
            positions.add(f"Полка {position}")
        
        for shelf in sorted(shelves):
            keyboard.append([KeyboardButton(shelf)])
            
        for position in sorted(positions):
            keyboard.append([KeyboardButton(position)])
            
        keyboard.append([KeyboardButton("Показать все"), KeyboardButton("◀️ Назад")])
        
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Выберите параметры поиска:", 
            reply_markup=reply_markup
        )
    elif text == "📋 История":
        keyboard = [
            [KeyboardButton("📋 Последние 10 операций")],
            [KeyboardButton("📋 Вся история")],
            [KeyboardButton("🔙 Вернуться в главное меню")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            "Выберите количество операций для отображения:",
            reply_markup=reply_markup
        )
    elif text == "📋 Последние 10 операций" or text == "📋 Вся история":
        await recent_command(update, context)
    elif text == "🎥 Сканировать":
        await scan_command(update, context)
    elif text == "📊 Отчет о сканировании":
        keyboard = [
            [KeyboardButton("📊 Последний отчет"), KeyboardButton("📊 Последние 3 отчета")],
            [KeyboardButton("📊 Последние 5 отчетов"), KeyboardButton("📊 Все отчеты")],
            [KeyboardButton("🔙 Вернуться в главное меню")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Выберите тип отчета:", reply_markup=reply_markup)
    elif text == "📊 Последний отчет" or text == "📊 Последние 3 отчета" or text == "📊 Последние 5 отчетов" or text == "📊 Все отчеты":
        await last_scan_report_command(update, context)
    elif text == "❓ Помощь":
        await help_command(update, context)
    elif text == "🔙 Вернуться в главное меню":
        keyboard = [
            [KeyboardButton("🔍 Поиск предметов"), KeyboardButton("📦 Поиск по расположению")],
            [KeyboardButton("📋 История"), KeyboardButton("🎥 Сканировать")],
            [KeyboardButton("📊 Отчет о сканировании"), KeyboardButton("❓ Помощь")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Главное меню:", reply_markup=reply_markup)
    elif text == "◀️ Назад":
        keyboard = [
            [KeyboardButton("🔍 Поиск предметов"), KeyboardButton("📦 Поиск по расположению")],
            [KeyboardButton("📋 История"), KeyboardButton("🎥 Сканировать")],
            [KeyboardButton("📊 Отчет о сканировании"), KeyboardButton("❓ Помощь")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Главное меню:", reply_markup=reply_markup)
    elif text.startswith("Стеллаж"):
        shelf = text.split()[1]
        items = search_items(shelf, None)
        if items:
            response = f"Предметы на стеллаже {shelf}:\n\n"
            for item in items:
                qr_num = ''.join(filter(str.isdigit, item.qr_code))
                response += f"- {item.name} (QR ID: {qr_num})\n"
                response += f"  Расположение: {item.shelf}, {item.position}\n"
        else:
            response = f"Предметы на стеллаже {shelf} не найдены"
        await update.message.reply_text(response)
    elif text.startswith("Полка"):
        position = text.split()[1]
        items = search_items(None, position)
        if items:
            response = f"Предметы на полке {position}:\n\n"
            for item in items:
                qr_num = ''.join(filter(str.isdigit, item.qr_code))
                response += f"- {item.name} (QR ID: {qr_num})\n"
                response += f"  Расположение: {item.shelf}, {item.position}\n"
        else:
            response = f"Предметы на полке {position} не найдены"
        await update.message.reply_text(response)
    elif text == "Показать все":
        items = get_all_items()
        if items:
            response = "Все предметы в базе данных:\n\n"
            for item in items:
                qr_num = ''.join(filter(str.isdigit, item.qr_code))
                response += f"- {item.name} (QR ID: {qr_num})\n"
                response += f"  Расположение: {item.shelf}, {item.position}\n"
        else:
            response = "База данных пуста"
        await update.message.reply_text(response)
    else:
        item = find_item(text)
        if item:
            qr_num = ''.join(filter(str.isdigit, item.qr_code))
            response = f"""Предмет найден!
Название: {item.name}
QR ID: {qr_num}
Расположение: {item.shelf}, {item.position}"""
            item_id = cast(int, item.id) if isinstance(item, Item) else None
            add_scan_history("find", item_id, "success")
        else:
            response = "Предмет не найден"
            add_scan_history("find", None, f"not_found: {text}")
        await update.message.reply_text(response) 

