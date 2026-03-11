import os
import json
import logging
import asyncio
import aiohttp
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes, ChatMemberHandler
)
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL = os.getenv('WEBHOOK_URL')
PORT = int(os.getenv('PORT', 10000))
SUBSCRIPTIONS_FILE = 'subscriptions.json'

OREF_URL = "https://www.oref.org.il/WarningMessages/alert/alerts.json"
OREF_HEADERS = {
    "Referer": "https://www.oref.org.il/",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0",
}
POLL_INTERVAL = 3  # секунды

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== РЕГИОНЫ ====================

REGIONS = {
    "all":         ("🌍 Вся страна",        None),
    "tel_aviv":    ("🏙 Тель-Авив и центр", ["תל אביב", "דן", "שרון", "שפלה"]),
    "jerusalem":   ("🏛 Иерусалим",         ["ירושלים"]),
    "haifa":       ("🌊 Хайфа",             ["חיפה", "כרמל"]),
    "south":       ("🏜 Юг",               ["לכיש", "אשדוד", "אשקלון", "באר שבע", "נגב"]),
    "gaza_border": ("🔴 Граница Газы",      ["שדרות", "נתיבות", "שער הנגב", "שדות נגב", "עוטף עזה"]),
    "north":       ("🏔 Север и Голаны",    ["גליל", "גולן", "עכו", "טבריה", "צפון"]),
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

# ==================== POLLING ТРЕВОГ ====================

def alert_matches_region(cities: list, region_key: str) -> bool:
    if region_key == "all":
        return True
    keywords = REGIONS[region_key][1] or []
    for city in cities:
        for kw in keywords:
            if kw in city:
                return True
    return False

async def alert_polling_loop(app: Application):
    last_alert_id = None
    logger.info("Alert polling loop started")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(OREF_URL, headers=OREF_HEADERS, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    text = await resp.text(encoding='utf-8-sig')
                    text = text.strip()

                    if not text:
                        last_alert_id = None
                        await asyncio.sleep(POLL_INTERVAL)
                        continue

                    data = json.loads(text)
                    alert_id = data.get("id")
                    cities = data.get("data", [])

                    if alert_id and alert_id != last_alert_id and cities:
                        logger.info(f"New alert [{alert_id}]: {cities}")
                        last_alert_id = alert_id

                        for chat_id, region_key in list(subscriptions.items()):
                            if alert_matches_region(cities, region_key):
                                try:
                                    await app.bot.send_message(chat_id=int(chat_id), text="🛡🛡🛡")
                                except Exception as e:
                                    logger.error(f"Failed to send to {chat_id}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Polling error: {e}")

            await asyncio.sleep(POLL_INTERVAL)

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

    asyncio.create_task(alert_polling_loop(application))
    logger.info("Alert polling started")

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
