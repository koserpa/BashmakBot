"""Спільний рантайм-стан + персист історії чатів у Telegram (state_store).

Історія зберігається як:
  chat_history = {
    chat_id: {
      "summary": "короткий підсумок старих повідомлень...",
      "recent": [{"role": "user", "parts": [{"text": "..."}]}, ...],
      "updated_at": "ISO timestamp"
    }
  }

При перевищенні HISTORY_SUMMARY_THRESHOLD старі повідомлення стискаються
через Gemini в один summary, і залишаються лише останні HISTORY_RECENT_KEEP.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from config import (
    HISTORY_SIZE,
    HISTORY_PERSIST_ENABLED,
    HISTORY_SUMMARY_THRESHOLD,
    HISTORY_RECENT_KEEP,
    HISTORY_SAVE_INTERVAL_SEC,
    STATE_CHAT_ID,
)

log = logging.getLogger("Bashma4ek_Bot.bot_state")

# Заповнюється один раз у main() під час старту — щоб не смикати get_me()
# на кожне повідомлення.
BOT_ID: int | None = None
BOT_USERNAME: str = ""
BOT_FULL_NAME: str = ""

START_TIME = time.time()

# --- Внутрішній формат історії ---
# chat_id -> {"summary": str, "recent": deque[dict], "dirty": bool}
_history: dict[int, dict[str, Any]] = defaultdict(
    lambda: {"summary": "", "recent": deque(maxlen=HISTORY_RECENT_KEEP), "dirty": False}
)

# --- Проактивні повідомлення в тихому чаті ---
last_human_activity: dict[int, float] = {}
idle_message_sent: dict[int, bool] = {}

# --- Детектор "срачу" ---
message_activity: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))
last_drama_intervention: dict[int, float] = {}

# --- Тиша конкретної людини ---
last_user_activity: dict[tuple[int, int], float] = {}
last_absence_poke: dict[tuple[int, int], float] = {}
known_chat_users: dict[int, dict] = defaultdict(dict)

# --- Персист історії ---
_history_save_task: asyncio.Task | None = None
_history_initialized = False


def set_bot_identity(bot_id: int, username: str, full_name: str) -> None:
    global BOT_ID, BOT_USERNAME, BOT_FULL_NAME
    BOT_ID = bot_id
    BOT_USERNAME = username
    BOT_FULL_NAME = full_name


# ---------- Публічний API для історії ----------

def get_history(chat_id: int) -> list[dict]:
    """Повертає повну історію (summary + recent) як список повідомлень для LLM."""
    h = _history[chat_id]
    result = []
    if h["summary"]:
        # Summary передаємо як system-подібне повідомлення
        result.append({"role": "user", "parts": [{"text": f"[Контекст попередньої розмови]: {h['summary']}"}]})
    result.extend(h["recent"])
    return result


def append_history(chat_id: int, message: dict) -> None:
    """Додає повідомлення в історію, позначає dirty, можливо тригерить сумаризацію."""
    h = _history[chat_id]
    h["recent"].append(message)
    h["dirty"] = True
    _maybe_trigger_summarization(chat_id)


def clear_history(chat_id: int) -> None:
    """Повністю очищає історію чату (команда /reset)."""
    h = _history[chat_id]
    h["summary"] = ""
    h["recent"].clear()
    h["dirty"] = True


def history_len(chat_id: int) -> int:
    """Кількість повідомлень у recent (summary не рахується)."""
    return len(_history[chat_id]["recent"])


# ---------- Сумаризація ----------

_summarization_lock = asyncio.Lock()
_summarization_in_progress: set[int] = set()


def _maybe_trigger_summarization(chat_id: int) -> None:
    """Якщо recent перевищив поріг — плануємо сумаризацію."""
    h = _history[chat_id]
    if len(h["recent"]) >= HISTORY_SUMMARY_THRESHOLD:
        # запускаємо в фоні, не блокуємо
        asyncio.create_task(_summarize_chat(chat_id))


async def _summarize_chat(chat_id: int) -> None:
    """Стискає старі повідомлення в summary через Gemini."""
    if chat_id in _summarization_in_progress:
        return
    async with _summarization_lock:
        if chat_id in _summarization_in_progress:
            return
        _summarization_in_progress.add(chat_id)

    try:
        h = _history[chat_id]
        if len(h["recent"]) < HISTORY_SUMMARY_THRESHOLD:
            return

        # Беремо повідомлення, які йдуть в summary (всі крім останніх KEEP)
        to_summarize = list(h["recent"])[:-HISTORY_RECENT_KEEP]
        if not to_summarize:
            return

        # Формуємо промпт для сумаризації
        dialog_text = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Bot'}: {' '.join(p.get('text', '') for p in m.get('parts', []))}"
            for m in to_summarize
        )

        prompt = (
            "Стисни діалог до 3-4 речень українською. "
            "Збережи ключові факти, імена, теми, домовленості. "
            "Не додавай власних коментарів, тільки суть.\n\n"
            f"ДІАЛОГ:\n{dialog_text}"
        )

        # Використовуємо Gemini для сумаризації
        from gemini_client import ask_gemini
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summary = await ask_gemini([{"role": "user", "parts": [{"text": prompt}]}], current_date)

        if summary and len(summary.strip()) > 10:
            # Мерджуємо з існуючим summary
            old_summary = h["summary"]
            if old_summary:
                # Просимо Gemini об'єднати старий summary з новим
                merge_prompt = (
                    "Об'єднай два підсумки в один (3-4 речення), без втрати фактів.\n\n"
                    f"СТАРИЙ:\n{old_summary}\n\nНОВИЙ:\n{summary}"
                )
                merged = await ask_gemini([{"role": "user", "parts": [{"text": merge_prompt}]}], current_date)
                if merged and len(merged.strip()) > 10:
                    h["summary"] = merged.strip()
                else:
                    h["summary"] = summary.strip()
            else:
                h["summary"] = summary.strip()

            # Залишаємо лише останні KEEP повідомлень
            h["recent"] = deque(list(h["recent"])[-HISTORY_RECENT_KEEP:], maxlen=HISTORY_RECENT_KEEP)
            h["dirty"] = True
            log.info(f"history: chat {chat_id} summarized (summary len={len(h['summary'])}, recent={len(h['recent'])})")

    except Exception as e:
        log.warning(f"history: summarization failed for chat {chat_id}: {e}")
    finally:
        _summarization_in_progress.discard(chat_id)


# ---------- State Store інтеграція ----------

def _collect_history_for_snapshot() -> dict[str, Any]:
    """Getter для state_store: серіалізуємо історію всіх чатів."""
    result = {}
    for chat_id, h in _history.items():
        if h["recent"] or h["summary"]:
            result[str(chat_id)] = {
                "summary": h["summary"],
                "recent": list(h["recent"]),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
    return result


def _apply_history_from_snapshot(data: Any) -> None:
    """Setter для state_store: відновлюємо історію зі снапшоту."""
    global _history_initialized
    if not isinstance(data, dict):
        return
    for chat_id_str, h in data.items():
        try:
            chat_id = int(chat_id_str)
            summary = h.get("summary", "") or ""
            recent_list = h.get("recent", []) or []
            _history[chat_id] = {
                "summary": summary,
                "recent": deque(recent_list, maxlen=HISTORY_RECENT_KEEP),
                "dirty": False,
            }
        except Exception:
            continue
    _history_initialized = True
    log.info(f"history: restored {len(_history)} chats from snapshot")


async def _history_flusher() -> None:
    """Фоновий цикл: періодично зберігає dirty історії в state_store."""
    import state_store
    while True:
        try:
            await asyncio.sleep(HISTORY_SAVE_INTERVAL_SEC)
            if not HISTORY_PERSIST_ENABLED or not STATE_CHAT_ID:
                continue
            dirty_chats = [cid for cid, h in _history.items() if h.get("dirty")]
            if dirty_chats:
                # state_store сам забере getter і збереже
                state_store.request_save()
                for cid in dirty_chats:
                    _history[cid]["dirty"] = False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"history: flusher error: {e}")


async def start_history_persistence() -> None:
    """Викликається в main() після state_store.start()."""
    global _history_save_task, _history_initialized
    if not HISTORY_PERSIST_ENABLED or not STATE_CHAT_ID:
        log.info("history: persistence disabled (no STATE_CHAT_ID or disabled in config)")
        return
    # Реєструємо секцію в state_store
    import state_store
    state_store.register("chat_history", _collect_history_for_snapshot, _apply_history_from_snapshot)
    # Запускаємо flusher
    _history_save_task = asyncio.create_task(_history_flusher(), name="history_flusher")
    log.info("history: persistence started")


async def stop_history_persistence() -> None:
    """Flush при вимкненні."""
    global _history_save_task
    if _history_save_task:
        _history_save_task.cancel()
        try:
            await _history_save_task
        except (asyncio.CancelledError, Exception):
            pass
        _history_save_task = None
    # Фінальний збереження
    import state_store
    if HISTORY_PERSIST_ENABLED and STATE_CHAT_ID:
        await state_store.save_now()
    log.info("history: persistence stopped")