"""Точка входу бота. Уся бізнес-логіка винесена в окремі модулі:
- config.py         — конфіг з .env
- bot_state.py       — спільний рантайм-стан (BOT_ID, history, idle-трекінг)
- search_utils.py    — Tavily, курс валют, погода, синхронізація дати
- media_utils.py     — робота з документами/медіа
- gemini_client.py   — звернення до Gemini API
- handlers.py        — усі Telegram-хендлери
- plugins/         — плагінна система (авто-завантаження)
"""
import asyncio
import json
import logging
import os
import time
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

import bot_state
import state_store
import tasks
from config import BOT_TOKEN, GEMINI_API_KEY, GEMINI_MODEL, STATE_CHAT_ID
from handlers import idle_chat_watcher, register_handlers
from plugins import load_plugins
from reminder_utils import reminders_watcher
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
    піднімаємо мінімальний HTTP-сервер для health-check поруч з polling'ом.

    /health віддає не просто «OK», а стан VULCAN: коли був останній успішний
    полінг розкладу, скільки цілей і чи жива сесія. HTTP-код завжди 200:
    Koyeb на 503 перезапускає сервіс, а перезапуск не лікує мертву сесію
    VULCAN (лише втрачає ephemeral state і спамить дублями карток).
    """
    port = int(os.getenv("PORT", "8000"))
    from vulcan_notify import health_info

    async def health(request):
        info = health_info()
        stale_after = max(45, info["poll_interval_min"] * 3) * 60
        last = info.get("last_poll_at")
        age = None
        if last:
            try:
                age = int(time.time() - datetime.fromisoformat(last).timestamp())
            except (TypeError, ValueError):
                age = None
        degraded = bool(
            not info.get("configured")
            or info.get("last_poll_ok") is False
            or age is None or age > stale_after
        )
        payload = {
            "status": "degraded" if degraded else "ok",
            "uptime_sec": int(time.time() - bot_state.START_TIME),
            "vulcan": info,
            "state": state_store.status(),
            "vulcan_poll_age_sec": age,
            "stale_after_sec": stale_after,
        }
        return web.json_response(payload)

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
    bot_state.set_bot_identity(me.id, me.username, me.full_name)  # type: ignore
    log.info(f"Бот авторизований як @{me.username} (id={me.id})")

    # ВАЖЛИВО: плагіни реєструємо ПЕРЕД основними хендлерами. aiogram бере
    # перший хендлер, що підійшов, а загальний @dp.message(F.text) ловить
    # будь-який текст (включно з командами) — тому всі плагінні команди
    # (/joke, /hello, /fill) мають бути зареєстровані раніше.
    background_tasks: set = set()
    loaded = load_plugins(dp, bot, background_tasks)
    log.info(f"Завантажено плагінів: {loaded}")

    register_handlers(dp, bot)

    # Durable-стан: закріплений JSON у приватному чаті (Koyeb Free: диск
    # ефемерний). Має стартувати ПІСЛЯ імпорту модулів (вони реєструють
    # секції) і ДО watcher'ів, щоб вони вже бачили відновлений стан.
    dp.shutdown.register(state_store.stop)
    await state_store.start(bot, STATE_CHAT_ID)

    await _start_health_server()

    await refresh_current_date()  # синхронний перший фетч ще до старту polling
    tasks.spawn_watch("date_sync", lambda: date_sync_watcher())
    log.info("Синхронізація дати з інтернетом запущена (кожні 8г)")

    tasks.spawn_watch("idle_chat", lambda: idle_chat_watcher(bot))
    idle_hours = float(os.getenv("IDLE_HOURS", "7"))
    log.info(f"Спостерігач за тишею в чаті запущено (поріг {idle_hours}г)")

    tasks.spawn_watch("reminders", lambda: reminders_watcher(bot))
    log.info("Воркер нагадувань запущено")

    # Background watcher tasks: тепер усі живуть у tasks.WATCHERS під
    # супервізором (фоновий цикл впав — сам задовбався, лог у Koyeb).
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
