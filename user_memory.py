"""Авто-пам'ять про користувачів (LLM-факти з чату).

Раз на добу (USER_MEMORY_HOUR, Варшава) ОДИН дешевий Gemini-виклик
витягує з останніх повідомлень чатів стійкі ПУБЛІЧНО-БЕЗПЕЧНІ факти
про відомих користувачів: плани, події, навчання, хобі, покупки.
Факти зберігаються в снапшоті (секція user_memory), при інжекті
додаються до профілю з датами — модель бачить, наскільки факт свіжий.

Приватність: у промпті пряма заборона на здоров'я/гроші/стосунки/сім'ю;
поле sensitive з профілів видалено (рішення власника 03.11.2026).
Кап: USER_MEMORY_MAX_FACTS на людину (старі витісняються), TTL 60 днів.

Секція реєструється на рівні імпорту — state_store.start() відновлює
тільки зареєстровані секції.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import config
import state_store

log = logging.getLogger("Bashma4ek_Bot.user_memory")

TZ = ZoneInfo("Europe/Warsaw")

# Факти старше цього (днів) викидаються при кожному запуску.
FACT_TTL_DAYS = 60

# username (lowercase) -> [{"t": факт, "d": "YYYY-MM-DD"}] (новіші в кінці)
_state: dict[str, Any] = {"last_run": "", "users": {}}
# username (lowercase) -> Telegram full_name (наповнюється з middleware)
_names: dict[str, str] = {}


def _norm(username: str | None) -> str:
    return (username or "").lstrip("@").strip().lower()


# ---------------- реєстр імен ----------------

def note_user(username: str | None, full_name: str | None) -> None:
    """Запам'ятати відповідність username <-> Telegram-ім'я (з middleware).
    Потрібно, щоб витягнуті з чату факти («Костя сказав...») мапились
    на username, а для людей без @username — знаходитись за ім'ям."""
    key = _norm(username)
    name = (full_name or "").strip()
    if not key or not name:
        return
    if _names.get(key) != name:
        _names[key] = name


def _resolve_username(key_or_name: str) -> str:
    """'koserpa' / 'Костя Кравченко' -> canonical username або ''."""
    key = _norm(key_or_name)
    if not key:
        return ""
    import user_profiles
    if key in user_profiles.known_usernames() or key in _names:
        return key
    for u, name in _names.items():
        if name.lower() == key or name.lower().split()[0:1] == [key]:
            return u
    return ""


# ---------------- читання пам'яті ----------------

def facts_count(username: str | None) -> int:
    key = _norm(username)
    return len(_state.get("users", {}).get(key) or [])


def get_memory_text(username: str | None, full_name: str | None = None) -> str:
    """Пам'ять про людину для інжекту в LLM-контекст (або '').
    Ключ: спершу username, при промаху — Telegram full_name."""
    users = _state.get("users", {})
    key = _norm(username)
    if key not in users and full_name:
        key = _norm(_resolve_username(full_name))
    facts = users.get(key) or []
    if not facts:
        return ""
    lines = []
    for f in facts:
        d = str(f.get("d") or "")
        when = f"({d[8:10]}.{d[5:7]}) " if len(d) >= 10 else ""
        lines.append(f"• {when}{f.get('t', '')}".rstrip())
    return "Пам'ять про людину (з чату, може застаріти):\n" + "\n".join(lines)


# ---------------- витягування фактів ----------------

def _build_prompt(dialogs: list[str]) -> tuple[str, int]:
    """Промпт для витягування. Повертає (текст, кількість учасників у ростері)."""
    import user_profiles
    roster = []
    for u in user_profiles.known_usernames():
        name = _names.get(u, "")
        roster.append(f"@{u}" + (f" = {name}" if name else ""))
    return (
        "Ось фрагменти чату та список учасників (username = ім'я в Telegram).\n"
        "Витягни СТІЙКІ факти про учасників, що стануть у пригоді в майбутніх "
        "розмовах: плани, події, навчання/школа, хобі, покупки, вподобання.\n"
        "ЗАБОРОНЕНО записувати: здоров'я, гроші, стосунки, сім'ю, релігію — "
        "такі речі ігноруй. НЕ вигадуй: тільки те, що прямо випливає з чату. "
        "Формулюй коротко (до 100 символів), українською. "
        f"Максимум {int(config.USER_MEMORY_MAX_FACTS or 12)} фактів на людину. "
        "Якщо нових фактів немає — поверни порожній об'єкт.\n"
        "Відповідь — ТІЛЬКИ JSON без пояснень, формат:\n"
        '{"<username>": ["факт 1", "факт 2"]}\n\n'
        "УЧАСНИКИ:\n" + ("\n".join(roster) or "(невідомі)") +
        "\n\nЧАТ:\n" + "\n---\n".join(dialogs)
    ), len(roster)


def _parse_facts_json(raw: str) -> dict[str, list[str]]:
    """Розібрати відповідь моделі (JSON, можливо у ```...```)."""
    if not raw:
        return {}
    text = raw.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, facts in data.items():
        if not isinstance(facts, list):
            continue
        clean = [str(f).strip()[:200] for f in facts if str(f).strip()]
        if clean:
            out[str(key)] = clean
    return out


def _prune(users: dict[str, list[dict]], today: datetime) -> dict[str, list[dict]]:
    """TTL: факти старше FACT_TTL_DAYS викидаються."""
    cutoff = (today - timedelta(days=FACT_TTL_DAYS)).date().isoformat()
    out: dict[str, list[dict]] = {}
    for u, facts in users.items():
        kept = [f for f in facts if str(f.get("d") or "9999") >= cutoff]
        if kept:
            out[u] = kept
    return out


def _merge_facts(username: str, new_facts: list[str], today_iso: str) -> bool:
    """Дописати нові факти (без дублів), кап на кількість. True = змінилось."""
    users = _state.setdefault("users", {})
    cur = list(users.get(username) or [])
    seen = {str(f.get("t") or "").strip().lower() for f in cur}
    changed = False
    for t in new_facts:
        t = str(t or "").strip()[:200]
        if not t or t.lower() in seen:
            continue
        cur.append({"t": t, "d": today_iso})
        seen.add(t.lower())
        changed = True
    max_n = max(1, int(config.USER_MEMORY_MAX_FACTS or 12))
    if len(cur) > max_n:
        cur = cur[-max_n:]          # тримай найновіші
        changed = True
    users[username] = cur
    return changed


async def run_once(today: datetime | None = None) -> bool:
    """Один щоденний цикл витягування. True = день закрито."""
    today = today or datetime.now(TZ)
    today_iso = today.date().isoformat()
    if _state.get("last_run") == today_iso:
        return True
    import bot_state
    dialogs = bot_state.get_recent_dialog_texts()
    if not dialogs:
        return True                  # ще нічого аналізувати
    prompt, roster_n = _build_prompt(dialogs)

    from gemini_client import ask_gemini
    try:
        raw = await ask_gemini(
            [{"role": "user", "parts": [{"text": prompt}]}], today_iso)
    except Exception as e:
        log.warning(f"user_memory: запит до Gemini не вдався ({e})")
        return False                 # повтор наступного циклу

    # День закрито незалежно від парсингу: інакше крива відповідь моделі
    # молотитиме запитами до кінця доби (квота не гумові).
    _state["last_run"] = today_iso

    parsed = _parse_facts_json(raw or "")
    if not parsed:
        log.info("user_memory: нових фактів не знайдено (або не парситься)")
        state_store.request_save()
        return True

    users = _state.setdefault("users", {})
    users = _prune(users, today)
    _state["users"] = users
    total_new = 0
    for key, facts in parsed.items():
        username = _resolve_username(key)
        if not username:
            log.info(f"user_memory: факт для невідомого '{key[:40]}' пропущено")
            continue
        if _merge_facts(username, facts, today_iso):
            total_new += len(facts)
    state_store.request_save()
    log.info(f"user_memory: день {today_iso} закрито, нових фактів ~{total_new} "
             f"(ростер: {roster_n}, чатів: {len(dialogs)})")
    return True


async def memory_watcher() -> None:
    """Фоновий цикл: раз на добу після USER_MEMORY_HOUR (Варшава)."""
    log.info(f"user_memory: запущено (щодня після {config.USER_MEMORY_HOUR}:00, "
             f"кап {config.USER_MEMORY_MAX_FACTS} фактів/людину, "
             f"enabled={config.USER_MEMORY_ENABLED})")
    while True:
        try:
            now = datetime.now(TZ)
            if config.USER_MEMORY_ENABLED and now.hour >= config.USER_MEMORY_HOUR:
                await run_once(now)
        except Exception as e:
            log.warning(f"user_memory: помилка циклу: {e}")
        await asyncio.sleep(20 * 60)


# ---------------- state_store (снапшот) ----------------

def _get_snapshot() -> dict[str, Any]:
    return {"last_run": _state.get("last_run", ""), "users": _state.get("users", {})}


def _apply_snapshot(data: Any) -> None:
    if not isinstance(data, dict):
        return
    last_run = data.get("last_run")
    if isinstance(last_run, str):
        _state["last_run"] = last_run
    users = data.get("users")
    if isinstance(users, dict):
        clean: dict[str, list[dict]] = {}
        for user, facts in users.items():
            key = _norm(user)
            if not key or not isinstance(facts, list):
                continue
            kept = []
            for f in facts:
                if isinstance(f, dict) and str(f.get("t") or "").strip():
                    kept.append({"t": str(f["t"]).strip()[:200],
                                 "d": str(f.get("d") or "")[:10]})
            if kept:
                clean[key] = kept
        _state["users"] = clean
    log.info(f"user_memory: відновлено ({len(_state.get('users', {}))} людей, "
             f"last_run={_state.get('last_run') or '—'})")


state_store.register("user_memory", _get_snapshot, _apply_snapshot)
