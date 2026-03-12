import os
import json
import logging
import asyncio
import re
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes, ChatMemberHandler
)
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 10000))
SUBSCRIPTIONS_FILE = 'subscriptions.json'

# Telethon (user account для чтения канала)
TELEGRAM_API_ID = int(os.getenv('TELEGRAM_API_ID'))
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')
SESSION_BASE64 = os.getenv('SESSION_BASE64')
ROCKET_ALERT_CHANNEL = 'RocketAlert'  # @RocketAlert

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== РЕГИОНЫ ====================
# Ключи — зоны из заголовков сообщений канала (zone headers)

REGIONS = {
    "all":              ("🌍 Вся страна",         None),
    "tel_aviv":         ("🏙 Тель-Авив и центр",  ["Tel Aviv", "Dan Region", "Sharon", "Yarkon"]),
    "jerusalem":        ("🏛 Иерусалим",           ["Jerusalem"]),
    "haifa":            ("🌊 Хайфа",               ["Menashe", "HaMifratz", "HaCarmel"]),
    "south":            ("🏜 Юг",                  ["Lakhish", "Western Lakhish", "Shfela", "Shfelat Yehuda", "Negev"]),
    "gaza_border":      ("🔴 Граница Газы",         ["Gaza Envelope", "Shaar Hanegev", "Sdot Negev"]),
    "north":            ("🏔 Север и Голаны",       ["Upper Galilee", "Center Galilee", "Lower Galilee",
                                                     "Confrontation Line", "Northern Golan", "Southern Golan",
                                                     "HaAmakim", "Samaria", "Judea"]),
}

# ==================== ХРАНИЛИЩЕ ПОДПИСОК ====================

def load_subscriptions() -> dict:
    if os.path.exists(SUBSCRIPTIONS_FILE):
        with open(SUBSCRIPTIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_subscriptions(subs: dict):
    with open(SUBSCRIPTIONS_FILE, 'w') as f:
        json.dump(subs, f)

subscriptions: dict = load_subscriptions()

# ==================== ПАРСИНГ СООБЩЕНИЯ ====================

def parse_alert_zones(text: str) -> list:
    """Извлекает список зон из сообщения канала"""
    zones = []
    # Ищем строки вида "ZoneName:" (не содержат пробелов в начале)
    for line in text.splitlines():
        line = line.strip()
        if line.endswith(':') and line != ':':
            zone = line[:-1]  # убираем двоеточие
            zones.append(zone)
    return zones

def is_alert_message(text: str) -> bool:
    """Проверяет что сообщение — тревога (не пустое, не просто ссылка)"""
    return bool(text and ('alert' in text.lower()) and ':' in text)

# ==================== ЛОГИКА МАТЧИНГА ====================

def alert_matches_region(zones: list, region_key: str) -> bool:
    if region_key == "all":
        return bool(zones)
    keywords = REGIONS[region_key][1] or []
    for zone in zones:
        for kw in keywords:
            if kw.lower() in zone.lower():
                return True
    return False

# ==================== ОБРАБОТЧИКИ БОТА ====================

def build_region_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"region:{key}")]
        for key, (label, _) in REGIONS.items()
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🛡 Бот оповещений о ракетных атаках.\n\nВыберите регион для получения уведомлений:",
        reply_markup=build_region_keyboard()
    )

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Выберите регион для уведомлений:",
        reply_markup=build_region_keyboard()
    )

async def handle_region_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    region_key = query.data.split(":")[1]
    chat_id = str(query.message.chat_id)

    if region_key not in REGIONS:
        await query.edit_message_text("❌ Неизвестный регион.")
        return

    subscriptions[chat_id] = region_key
    save_subscriptions(subscriptions)
    region_label = REGIONS[region_key][0]
    await query.edit_message_text(
        f"✅ Регион выбран: {region_label}\n\nБот будет присылать 🛡🛡🛡 при тревоге."
    )

async def handle_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.my_chat_member
    if result and result.new_chat_member.status in ("member", "administrator"):
        await context.bot.send_message(
            chat_id=result.chat.id,
            text="👋 Привет! Я бот оповещений о ракетных атаках.\nВыберите регион:",
            reply_markup=build_region_keyboard()
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# ==================== TELETHON LISTENER ====================

async def start_telethon_listener(bot_app: Application):
    # Загружаем сессию из переменной окружения
    if SESSION_BASE64:
        import base64
        session_bytes = base64.b64decode(SESSION_BASE64)
        with open('session.session', 'wb') as f:
            f.write(session_bytes)
        logger.info("Session loaded from SESSION_BASE64")
    else:
        logger.warning("SESSION_BASE64 not set, trying existing session file")

    client = TelegramClient('session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start()
    logger.info("Telethon client started as bot")

    try:
        entity = await client.get_entity(ROCKET_ALERT_CHANNEL)
        logger.info(f"Successfully connected to channel: {entity.title}")
    except Exception as e:
        logger.error(f"Failed to get channel entity: {e}")
        return

    last_message_id = None

    # Получаем ID последнего сообщения при старте чтобы не слать старые тревоги
    async for msg in client.iter_messages(entity, limit=1):
        last_message_id = msg.id
    logger.info(f"Starting from message id: {last_message_id}")

    while True:
        try:
            new_messages = []
            async for msg in client.iter_messages(entity, min_id=last_message_id, limit=20):
                new_messages.append(msg)

            # iter_messages возвращает от новых к старым — разворачиваем
            for msg in reversed(new_messages):
                last_message_id = max(last_message_id or 0, msg.id)
                text = msg.text or ""
                if not is_alert_message(text):
                    continue

                zones = parse_alert_zones(text)
                if not zones:
                    continue

                logger.info(f"Alert detected [msg {msg.id}], zones: {zones}")

                for chat_id, region_key in list(subscriptions.items()):
                    if alert_matches_region(zones, region_key):
                        try:
                            await bot_app.bot.send_message(chat_id=int(chat_id), text="🛡🛡🛡")
                        except Exception as e:
                            logger.error(f"Failed to send to {chat_id}: {e}")

        except Exception as e:
            logger.error(f"Polling error: {e}")

        await asyncio.sleep(3)

# ==================== ВЕБ-СЕРВЕР ====================

async def health_check_handler(request):
    return web.Response(text="Bot is alive!", status=200)

async def telegram_webhook_handler(request):
    try:
        bot_app = request.app['bot_app']
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return web.Response()
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return web.Response(status=500)

# ==================== ЗАПУСК ====================

async def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CallbackQueryHandler(handle_region_choice, pattern=r"^region:"))
    application.add_handler(ChatMemberHandler(handle_new_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_error_handler(error_handler)

    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL не установлен!")
        return

    await application.initialize()
    await application.start()

    webhook_path = f"{WEBHOOK_URL}/webhook"
    await application.bot.set_webhook(url=webhook_path, drop_pending_updates=True)
    logger.info(f"Webhook set to {webhook_path}")

    # Запускаем Telethon как asyncio task
    asyncio.create_task(start_telethon_listener(application))
    logger.info("Telethon listener task created")

    # Веб-сервер
    app = web.Application()
    app['bot_app'] = application
    app.router.add_get('/health', health_check_handler)
    app.router.add_post('/webhook', telegram_webhook_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Server running on port {PORT}")

    await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
