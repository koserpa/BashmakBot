"""Точка входу бота. Уся бізнес-логіка винесена в окремі модулі:
- config.py         — конфіг з .env
- bot_state.py       — спільний рантайм-стан (BOT_ID, history, idle-трекінг)
- search_utils.py    — Tavily, курс валют, погода, синхронізація дати
- media_utils.py     — робота з документами/медіа
- gemini_client.py   — звернення до Gemini API
- handlers.py        — усі Telegram-хендлери
"""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

import bot_state
from config import BOT_TOKEN, GEMINI_API_KEY, GEMINI_MODEL
from handlers import idle_chat_watcher, register_handlers
from search_utils import date_sync_watcher, refresh_current_date

logging.basicConfig(level=logging.INFO)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)
log = logging.getLogger("Bashma4ek_Bot")


def _validate_config() -> None:
    """Падаємо одразу зі зрозумілим повідомленням, якщо чогось не вистачає
    в .env — краще явна помилка при старті, ніж незрозумілий збій пізніше."""
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not GEMINI_MODEL:
        missing.append("GEMINI_MODEL")
    if missing:
        raise RuntimeError(
            "Не задані обов'язкові змінні оточення: " + ", ".join(missing)
        )
    from config import TAVILY_API_KEY
    if not TAVILY_API_KEY:
        log.warning(
            "TAVILY_API_KEY не задано — пошук в інтернеті (новини, "
            "картинки, довільні факти) працювати не буде. Курс валют і "
            "погода все одно працюють через окремі безкоштовні API."
        )


async def _start_health_server() -> None:
    """Koyeb (безкоштовний план) вимагає Web Service з відкритим портом —
    піднімаємо мінімальний HTTP-сервер для health-check поруч з polling'ом."""
    port = int(os.getenv("PORT", "8000"))

    async def health(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info(f"Health-check сервер запущено на порту {port}")


async def main():
    _validate_config()

    print(f"Поточна модель в боті: {GEMINI_MODEL}")
    log.info("Бот запускається...")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    me = await bot.get_me()
    bot_state.set_bot_identity(me.id, me.username, me.full_name) # type: ignore
    log.info(f"Бот авторизований як @{me.username} (id={me.id})")

    register_handlers(dp, bot)

    await _start_health_server()

    await refresh_current_date()  # синхронний перший фетч ще до старту polling
    asyncio.create_task(date_sync_watcher())
    log.info("Синхронізація дати з інтернетом запущена (кожні 8г)")

    asyncio.create_task(idle_chat_watcher(bot))
    idle_hours = float(os.getenv("IDLE_HOURS", "7"))
    log.info(f"Спостерігач за тишею в чаті запущено (поріг {idle_hours}г)")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
