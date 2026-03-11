import os
import json
import logging
import asyncio
import threading
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes, ChatMemberHandler
)
from dotenv import load_dotenv
import tzevaadom

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 10000))
SUBSCRIPTIONS_FILE = 'subscriptions.json'

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== РЕГИОНЫ ====================

REGIONS = {
    "all":         ("🌍 Вся страна",        None),
    "tel_aviv":    ("🏙 Тель-Авив и центр", ["Tel Aviv", "Dan Region", "Sharon", "Shfela"]),
    "jerusalem":   ("🏛 Иерусалим",         ["Jerusalem"]),
    "haifa":       ("🌊 Хайфа",             ["Haifa"]),
    "south":       ("🏜 Юг",               ["Lakhish", "Ashdod", "Ashkelon", "Beer Sheva", "Negev"]),
    "gaza_border": ("🔴 Граница Газы",      ["Gaza Envelope", "Shaar Hanegev", "Sdot Negev"]),
    "north":       ("🏔 Север и Голаны",    ["Galilee", "Golan", "Acre", "Tiberias", "North"]),
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
    await query.edit_message_text(f"✅ Регион выбран: {region_label}\n\nБот будет присылать 🛡🛡🛡 при тревоге.")

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

# ==================== ЛОГИКА ТРЕВОГ ====================

def alert_matches_region(zone_en: str, region_key: str) -> bool:
    if region_key == "all":
        return True
    keywords = REGIONS[region_key][1] or []
    return any(kw.lower() in zone_en.lower() for kw in keywords)

def start_alert_listener(app: Application, loop: asyncio.AbstractEventLoop):
    def handler(alerts: list):
        logger.info(f"Alert received: {[a.get('name_en') for a in alerts]}")
        notified = set()
        for alert in alerts:
            zone_en = alert.get("zone_en", "")
            for chat_id, region_key in list(subscriptions.items()):
                if chat_id in notified:
                    continue
                if alert_matches_region(zone_en, region_key):
                    notified.add(chat_id)
                    future = asyncio.run_coroutine_threadsafe(
                        app.bot.send_message(chat_id=int(chat_id), text="🛡🛡🛡"),
                        loop
                    )
                    try:
                        future.result(timeout=10)
                    except Exception as e:
                        logger.error(f"Failed to send to {chat_id}: {e}")

    try:
        logger.info("Starting tzevaadom alerts_listener...")
        tzevaadom.alerts_listener(handler)
        logger.info("tzevaadom alerts_listener started successfully")
    except Exception as e:
        logger.error(f"Failed to start alerts_listener: {e}")

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

    # Запускаем tzevaadom в отдельном потоке с явной передачей loop
    loop = asyncio.get_event_loop()
    t = threading.Thread(target=start_alert_listener, args=(application, loop), daemon=True)
    t.start()
    logger.info("Alert listener thread started")

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
