"""Довготривале сховище стану бота в Telegram (Koyeb Free: диск ефемерний).

Проблема: на Koyeb Free немає persistent volumes, `data/*.json` зникає при
кожному редеплої та cold start. Через це губились baseline VULCAN (→ дублі
карток), нагадування і (в майбутньому) історія чату.

Рішення: один JSON-документ, закріплений у приватному чаті з ботом.
- `getChat(chat_id).pinned_message` дає покажчик на документ (message_id +
  file_id) — бот не мусить пам'ятати id між запусками.
- оновлення — `editMessageMedia` того ж повідомлення: без нових повідомлень
  і без пушів користувачу;
- якщо повідомлення видалили/відкріпили — створюємо новий документ і пінимо.

Модулі реєструють свої секції:

    state_store.register("vulcan_notify", getter, setter)

`start(bot, chat_id)` викликається на старті (після імпорту модулів),
`stop()` — на graceful shutdown (flush). Якщо `STATE_CHAT_ID` не заданий,
модуль працює як no-op: лишається тільки локальний `data/`.

Безпека: у снапшот не потрапляють токени/паролі/cookies — тільки те, що
модулі віддають через getter. Вміст у логи не пишеться.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InputMediaDocument

log = logging.getLogger("Bashma4ek_Bot.state")

FILENAME = "bashmak-state.json"
CAPTION = "Стан бота (оновлюється автоматично, не видаляти)"
STATE_VERSION = 1

# Мінімальний інтервал між аплоадами (сек). Оновлення дешеве, але навіщо
# смикати Telegram на кожну дрібницю.
SAVE_MIN_INTERVAL = float(os.getenv("STATE_SAVE_MIN_SEC", "10") or "10")
MAX_BYTES = 40 * 1024 * 1024          # ліміт Telegram на документ — 50 МБ

_bot: Any = None
_chat_id: int = 0
_pinned_id: int | None = None
_pinned_file_id: str | None = None

_started = False
_synced = False                       # чи вже прочитали/створили снапшот
_dirty = False
_last_save_ts = 0.0
_last_save_at: str | None = None
_last_error = ""
_restored_at: str | None = None
_task: asyncio.Task | None = None
_lock = asyncio.Lock()

# name -> (getter, setter); getter віддає JSON-серіалізовний об'єкт,
# setter приймає його назад і оновлює стан модуля.
_sections: dict[str, tuple[Callable[[], Any], Callable[[Any], None]]] = {}


def register(name: str, getter: Callable[[], Any],
             setter: Callable[[Any], None]) -> None:
    _sections[name] = (getter, setter)


def _collect() -> dict[str, Any]:
    sections: dict[str, Any] = {}
    for name, (getter, _setter) in _sections.items():
        try:
            sections[name] = getter()
        except Exception:
            log.exception(f"state: не вдалось зібрати секцію {name}")
    return {
        "v": STATE_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": sections,
    }


def _apply(payload: Any) -> int:
    """Розкладає снапшот по секціях. Повертає кількість застосованих."""
    if not isinstance(payload, dict):
        raise ValueError("снапшот не є об'єктом")
    if int(payload.get("v") or 0) > STATE_VERSION:
        log.warning("state: снапшот новішого формату — читаю відомі секції")
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        raise ValueError("у снапшоті немає 'sections'")
    applied = 0
    for name, data in sections.items():
        entry = _sections.get(name)
        if not entry:
            continue                     # секція з майбутнього/іншого модуля
        try:
            entry[1](data)
            applied += 1
        except Exception:
            log.exception(f"state: не вдалось застосувати секцію {name}")
    return applied


async def load() -> str:
    """'loaded' | 'empty' | 'error'. 'empty' = чат доступний, але піна немає."""
    global _pinned_id, _pinned_file_id, _restored_at, _last_error
    if not _bot or not _chat_id:
        return "error"
    try:
        chat = await _bot.get_chat(_chat_id)
        pinned = getattr(chat, "pinned_message", None)
        doc = getattr(pinned, "document", None) if pinned else None
        if not doc:
            return "empty"
        # якщо в чаті закріпили чужий документ — не намагаємось його читати,
        # а створюємо/пінимо свій (інакше стан ніколи не збережеться)
        name = getattr(doc, "file_name", None)
        if name and name != FILENAME:
            log.info(f"state: закріплено інший документ ({name}); створюю свій")
            return "empty"
        f = await _bot.get_file(doc.file_id)
        if not f.file_path:
            return "empty"
        buf = await _bot.download_file(f.file_path)
        raw = buf.read() if hasattr(buf, "read") else buf
        if not raw:
            return "empty"
        if len(raw) > MAX_BYTES:
            raise ValueError(f"снапшот завеликий: {len(raw)} байт")
        payload = json.loads(raw.decode("utf-8"))
        applied = _apply(payload)
        _pinned_id = pinned.message_id
        _pinned_file_id = doc.file_id
        _restored_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _last_error = ""
        log.info(
            f"state: відновлено з Telegram ({len(raw)} байт, "
            f"секцій: {applied}, saved_at={payload.get('saved_at')})"
        )
        return "loaded"
    except Exception as e:
        _last_error = f"{type(e).__name__}: {e}"
        log.warning(f"state: не вдалось прочитати снапшот ({_last_error})")
        return "error"


async def save_now() -> bool:
    """Серіалізує стан і оновлює закріплений документ."""
    global _pinned_id, _pinned_file_id, _dirty
    global _last_save_ts, _last_save_at, _last_error
    if not _started or not _synced or not _bot or not _chat_id:
        return False
    async with _lock:
        raw = json.dumps(_collect(), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            _last_error = f"снапшот завеликий ({len(raw)} байт)"
            log.error(f"state: {_last_error}")
            return False

        media = InputMediaDocument(
            media=BufferedInputFile(raw, filename=FILENAME), caption=CAPTION,
        )
        old_id: int | None = None
        try:
            if _pinned_id:
                try:
                    await _bot.edit_message_media(
                        chat_id=_chat_id, message_id=_pinned_id, media=media,
                    )
                except TelegramBadRequest as e:
                    # повідомлення видалили / не можна редагувати — перестворюємо
                    log.warning(f"state: pinned не редагується ({e}); створюю новий")
                    old_id, _pinned_id = _pinned_id, None
                    _pinned_file_id = None
            if not _pinned_id:
                msg = await _bot.send_document(
                    _chat_id,
                    BufferedInputFile(raw, filename=FILENAME),
                    caption=CAPTION,
                    disable_notification=True,
                )
                _pinned_id = msg.message_id
                doc = getattr(msg, "document", None)
                _pinned_file_id = getattr(doc, "file_id", None)
                await _bot.pin_chat_message(
                    _chat_id, msg.message_id, disable_notification=True,
                )
                if old_id:      # прибрати старий документ, щоб не смітив чат
                    try:
                        await _bot.delete_message(_chat_id, old_id)
                    except Exception:
                        pass
            _last_save_ts = time.time()
            _last_save_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _last_error = ""
            _dirty = False
            log.info(f"state: збережено в Telegram ({len(raw)} байт)")
            return True
        except Exception as e:
            _last_error = f"{type(e).__name__}: {e}"
            log.warning(f"state: не вдалось зберегти снапшот ({_last_error})")
            return False


def request_save() -> None:
    """Позначити стан брудним; реальний аплоад зробить фоновий цикл."""
    global _dirty
    if _started and _synced:
        _dirty = True


async def _flusher() -> None:
    global _synced
    while True:
        try:
            if not _synced:
                # старт не зміг прочитати снапшот (мережа/Telegram) — не
                # перезаписуємо добрий стан порожнім, пробуємо ще раз
                result = await load()
                if result in ("loaded", "empty"):
                    _synced = True
                    if result == "empty":
                        await save_now()
                    log.info("state: синхронізацію з Telegram відновлено")
            elif _dirty and (time.time() - _last_save_ts) >= SAVE_MIN_INTERVAL:
                await save_now()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("state: помилка фонового збереження")
        await asyncio.sleep(3)


async def start(bot: Any, chat_id: int) -> None:
    """Викликати один раз на старті, до запуску watcher'ів."""
    global _bot, _chat_id, _started, _synced, _task
    _bot, _chat_id = bot, int(chat_id or 0)
    if not _chat_id:
        log.info("state: STATE_CHAT_ID не заданий — снапшот у Telegram вимкнено")
        return
    _started = True
    result = await load()
    _synced = result in ("loaded", "empty")
    if result == "empty":
        await save_now()
    _task = asyncio.create_task(_flusher(), name="state_store")
    log.info(
        f"state: Telegram-сховище активне (chat_id={_chat_id}, "
        f"pinned={_pinned_id}, стан={'відновлено' if result == 'loaded' else result})"
    )


async def stop() -> None:
    """Flush на graceful shutdown (Koyeb SIGTERM / Ctrl+C)."""
    global _task, _started
    if _task:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
    if _started and _synced:
        await save_now()
    _started = False
    log.info("state: зупинено")


def status() -> dict[str, Any]:
    """Для /health та /status — без мережі."""
    return {
        "configured": bool(_chat_id),
        "chat_id": _chat_id or None,
        "pinned": bool(_pinned_id),
        "synced": _synced,
        "dirty": _dirty,
        "restored_at": _restored_at,
        "last_save_at": _last_save_at,
        "last_error": _last_error,
        "sections": sorted(_sections),
    }


def reset_for_tests() -> None:
    """Скидання модульного стану в тестах (не для продакшену)."""
    global _bot, _chat_id, _pinned_id, _pinned_file_id, _started, _synced
    global _dirty, _last_save_ts, _last_save_at, _last_error, _restored_at, _task
    _bot = None
    _chat_id = 0
    _pinned_id = None
    _pinned_file_id = None
    _started = False
    _synced = False
    _dirty = False
    _last_save_ts = 0.0
    _last_save_at = None
    _last_error = ""
    _restored_at = None
    _task = None
