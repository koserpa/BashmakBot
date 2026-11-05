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

# Ключі сховища: "id:<user_id>" (нові записи) АБО username (легасі зі
# старих снапшотів; ледача міграція при першій появі людини в чаті).
# user_id стабільний і є завжди — на відміну від @username, який можна
# змінити або взагалі не мати.
_state: dict[str, Any] = {"last_run": "", "users": {}}
# "id:<user_id>" -> {"username": str, "name": str} (наповнюється middleware)
_users: dict[str, dict[str, str]] = {}


def _norm(username: str | None) -> str:
    return (username or "").lstrip("@").strip().lower()


def _id_key(user_id: Any) -> str:
    """user_id -> ключ сховища 'id:<uid>' ('' якщо немає/не числовий)."""
    if user_id is None:
        return ""
    if isinstance(user_id, int):
        return f"id:{user_id}"
    s = str(user_id).strip()
    return f"id:{s}" if s.isdigit() else ""


# ---------------- реєстр користувачів ----------------

def note_user(user_id: Any, username: str | None = None,
              full_name: str | None = None) -> None:
    """Запам'ятати користувача за user_id (з middleware). Працює і для
    людей БЕЗ @username — тоді ключ лише id + ім'я. Ледача міграція:
    факти, записані раніше під username-ключем, переїжджають під id."""
    uid = _id_key(user_id)
    name = (full_name or "").strip()
    uname = _norm(username)
    if not uid or not (uname or name):
        return
    entry = _users.setdefault(uid, {})
    if uname:
        entry["username"] = uname
    if name:
        entry["name"] = name
    if uname:
        users = _state.get("users", {})
        if uname in users and uid not in users:
            users[uid] = users.pop(uname)
            state_store.request_save()
            log.info(f"user_memory: факти '{uname}' -> {uid}")


def id_key_for_username(username: str | None) -> str:
    """'koserpa' -> 'id:...' з реєстру ('' якщо невідомий)."""
    uname = _norm(username)
    if not uname:
        return ""
    for uid, entry in _users.items():
        if entry.get("username") == uname:
            return uid
    return ""


def display_name(uid_key: str) -> str:
    """Людське ім'я для ключа сховища (для /profile-списку)."""
    entry = _users.get(uid_key) or {}
    if entry.get("name"):
        return entry["name"]
    if entry.get("username"):
        return "@" + entry["username"]
    return uid_key


def _resolve_key(key_or_name: str) -> str:
    """'@koserpa' / 'Костя' -> ключ сховища ('id:...' або легасі-username)."""
    raw = _norm(key_or_name)
    if not raw:
        return ""
    for uid, entry in _users.items():          # 1) username з реєстру
        if entry.get("username") == raw:
            return uid
    for uid, entry in _users.items():          # 2) ім'я (повне або перший токен)
        name = (entry.get("name") or "").lower()
        if name and (name == raw or name.split()[:1] == [raw]):
            return uid
    if raw in _state.get("users", {}):         # 3) вже існуючий ключ (легасі)
        return raw
    return ""


# ---------------- читання пам'яті ----------------

def _candidate_keys(user_id: Any, username: str | None) -> list[str]:
    """Усі можливі ключі людини (id + username + легасі), без дублів."""
    keys: list[str] = []
    k = _id_key(user_id)
    if k:
        keys.append(k)
    u = _norm(username)
    if u:
        keys.append(u)
        rk = id_key_for_username(u)
        if rk and rk not in keys:
            keys.append(rk)
    return [k for k in keys if k]


def _facts_for_keys(keys: list[str]) -> list[dict]:
    """Факти з усіх ключів людини, злиті і без дублів (від старих до свіжих)."""
    users = _state.get("users", {})
    merged: dict[str, dict] = {}
    for k in keys:
        for f in users.get(k) or []:
            t = str(f.get("t") or "").strip().lower()
            if t and t not in merged:
                merged[t] = f
    return sorted(merged.values(), key=lambda f: str(f.get("d") or ""))


def facts_count(user_id: Any = None, username: str | None = None) -> int:
    return len(_facts_for_keys(_candidate_keys(user_id, username)))


def get_memory_text(user_id: Any = None, username: str | None = None,
                    full_name: str | None = None) -> str:
    """Пам'ять про людину для інжекту в LLM-контекст (або '').
    Ключі: user_id -> username -> повне ім'я (через реєстр)."""
    keys = _candidate_keys(user_id, username)
    if full_name:
        rk = _resolve_key(full_name)
        if rk and rk not in keys:
            keys.append(rk)
    facts = _facts_for_keys(keys)
    if not facts:
        return ""
    lines = []
    for f in facts:
        d = str(f.get("d") or "")
        when = f"({d[8:10]}.{d[5:7]}) " if len(d) >= 10 else ""
        lines.append(f"• {when}{f.get('t', '')}".rstrip())
    return "Пам'ять про людину (з чату, може застаріти):\n" + "\n".join(lines)


def get_chat_facts_text() -> str:
    """Пам'ять про КОМПАНІЮ (домовленості/жарти/плани) для інжекту, або ''."""
    facts = _state.get("chat") or []
    if not facts:
        return ""
    lines = []
    for f in facts:
        d = str(f.get("d") or "")
        when = f"({d[8:10]}.{d[5:7]}) " if len(d) >= 10 else ""
        lines.append(f"• {when}{f.get('t', '')}".rstrip())
    return "Пам'ять про компанію (з чату, може застаріти):\n" + "\n".join(lines)


def _merge_chat(new_facts: list[str], today_iso: str) -> bool:
    """Дописати факти про компанію (без дублів), кап. True = змінилось."""
    cur = list(_state.get("chat") or [])
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
        cur = cur[-max_n:]
        changed = True
    _state["chat"] = cur
    return changed


# ---------------- витягування фактів ----------------

def _build_prompt(dialogs: list[str]) -> tuple[str, int]:
    """Промпт для витягування. Повертає (текст, кількість учасників у ростері)."""
    import user_profiles
    roster: list[str] = []
    for uid, entry in _users.items():
        uname = entry.get("username") or ""
        name = entry.get("name") or ""
        label = f"@{uname}" if uname else (name or uid)
        if name and uname:
            roster.append(f"{label} = {name}")
        else:
            roster.append(label)
    for u in user_profiles.known_usernames():
        if not any(u == e.get("username") for e in _users.values()):
            roster.append(f"@{u}")
    return (
        "Ось фрагменти чату та список учасників (username = ім'я в Telegram).\n"
        "Витягни два типи СТІЙКИХ фактів:\n"
        "1) про учасників: плани, події, навчання, хобі, покупки, вподобання — "
        "у полі \"users\";\n"
        "2) про КОМПАНІЮ загалом: домовленості, спільні плани, внутрішні жарти "
        "й реалії («збираємось у п'ятницю», «грають у CS разом») — у полі "
        "\"chat\", без прив'язки до конкретної людини.\n"
        "ЗАБОРОНЕНО записувати: здоров'я, гроші, стосунки, сім'ю, релігію — "
        "такі речі ігноруй. НЕ вигадуй: тільки те, що прямо випливає з чату. "
        "Формулюй коротко (до 100 символів), українською. "
        f"Максимум {int(config.USER_MEMORY_MAX_FACTS or 12)} фактів на людину "
        "і стільки ж у полі \"chat\". Якщо нових фактів немає — порожні поля.\n"
        "Відповідь — ТІЛЬКИ JSON без пояснень, формат:\n"
        '{"users": {"<username>": ["факт 1", "факт 2"]}, "chat": ["факт про компанію"]}\n\n'
        "УЧАСНИКИ:\n" + ("\n".join(roster) or "(невідомі)") +
        "\n\nЧАТ:\n" + "\n---\n".join(dialogs)
    ), len(roster)


def _parse_facts_json(raw: str) -> tuple[dict[str, list[str]], list[str]]:
    """Розібрати відповідь моделі (JSON, можливо у ```...```).

    Формат: {"users": {...}, "chat": [...]}. Старий формат без "users"
    ({"<username>": [...]}) теж приймається — тоді chat = [].
    Повертає (users, chat_facts)."""
    if not raw:
        return {}, []
    text = raw.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}, []
    try:
        data = json.loads(m.group(0))
    except (TypeError, ValueError):
        return {}, []
    if not isinstance(data, dict):
        return {}, []

    raw_chat = data.pop("chat", None)
    chat: list[str] = []
    if isinstance(raw_chat, list):
        chat = [str(f).strip()[:200] for f in raw_chat if str(f).strip()]

    # сумісність: і {"users": {...}}, і старий плаский формат
    users_raw = data.get("users") if isinstance(data.get("users"), dict) else data
    users: dict[str, list[str]] = {}
    for key, facts in (users_raw or {}).items():
        if not isinstance(facts, list):
            continue
        clean = [str(f).strip()[:200] for f in facts if str(f).strip()]
        if clean:
            users[str(key)] = clean
    return users, chat


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
        # use_persona=False: витягування — службовий виклик, факти мають
        # бути нейтральними, без акценту Гоги/Рози
        raw = await ask_gemini(
            [{"role": "user", "parts": [{"text": prompt}]}], today_iso,
            use_persona=False)
    except Exception as e:
        log.warning(f"user_memory: запит до Gemini не вдався ({e})")
        return False                 # повтор наступного циклу

    # День закрито незалежно від парсингу: інакше крива відповідь моделі
    # молотитиме запитами до кінця доби (квота не гумові).
    _state["last_run"] = today_iso

    parsed_users, parsed_chat = _parse_facts_json(raw or "")
    if not parsed_users and not parsed_chat:
        log.info("user_memory: нових фактів не знайдено (або не парситься)")
        state_store.request_save()
        return True

    users = _state.setdefault("users", {})
    users = _prune(users, today)
    _state["users"] = users
    # chat-факти під тим самим TTL
    kept_chat = [f for f in (_state.get("chat") or [])
                 if str(f.get("d") or "9999") >=
                 (today - timedelta(days=FACT_TTL_DAYS)).date().isoformat()]
    _state["chat"] = kept_chat
    total_new = 0
    for key, facts in parsed_users.items():
        store_key = _resolve_key(key)
        if not store_key:
            log.info(f"user_memory: факт для невідомого '{key[:40]}' пропущено")
            continue
        if _merge_facts(store_key, facts, today_iso):
            total_new += len(facts)
    if parsed_chat and _merge_chat(parsed_chat, today_iso):
        total_new += len(parsed_chat)
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
    return {"last_run": _state.get("last_run", ""),
            "users": _state.get("users", {}),
            "chat": _state.get("chat", [])}


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
    chat = data.get("chat")
    if isinstance(chat, list):
        _state["chat"] = [
            {"t": str(f.get("t") or "").strip()[:200],
             "d": str(f.get("d") or "")[:10]}
            for f in chat
            if isinstance(f, dict) and str(f.get("t") or "").strip()
        ]
    log.info(f"user_memory: відновлено ({len(_state.get('users', {}))} людей, "
             f"{len(_state.get('chat', []))} фактів про компанію, "
             f"last_run={_state.get('last_run') or '—'})")


state_store.register("user_memory", _get_snapshot, _apply_snapshot)
