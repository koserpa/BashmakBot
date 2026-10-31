"""Проактивні сповіщення VULCAN для чату.

Дві фічі (обидві — фоновими watcher'ами, жодних запитів від користувача):

1. ЗМІНИ В РОЗКЛАДІ (заміни/відміни/примітки)
   Кожні VULCAN_CHANGES_POLL_MIN хвилин тягнемо розклад на найближчі 7 днів
   (force=True — свіжий), порівнюємо «підписи змін» кожного уроку з
   збереженим станом (data/vulcan_notify.json). Якби вчитель/сала змінили
   або урок відмінили — кидаємо картку в чат(и).

2. НАГАДУВАННЯ ЗА ДЕНЬ ДО ПОДІЇ
   Щодня після VULCAN_REMIND_HOUR год. (Варшава) перевіряємо
   /api/SprawdzianyZadaniaDomowe на завтра: sprawdzian (typ=1), kartkówka (typ=2),
   praca klasowa (typ=3), zadanie domowe (typ=4). Якщо є — картка «Завтра ...».

Кому слати: змінна VULCAN_NOTIFY_CHATS (chat_id через кому) АБО чати,
де хтось користувався /розклад (запам'ятовуються в data/vulcan_notify.json).

Безпека: у логах ніяких куків/ключів/вмісту — тільки факти і статуси.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from html import escape as _esc
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("Bashma4ek_Bot.vulcan.notify")

TZ = ZoneInfo("Europe/Warsaw")

import vulcan_utils as vu
import state_store

STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "vulcan_notify.json")

# Словник typ у /api/SprawdzianyZadaniaDomowe.
# Стандартний словарь VULCAN (порядок у дропдауні «Rodzaj» на порталі):
#   1=Sprawdzian, 2=Kartkówka, 3=Praca klasowa, 4=Zadanie domowe.
# Верифікація 27.10.2026: typ=1 → Sprawdzian (J.niemiecki 04.11, Matematyka 05.11
# сходяться зі скріншотів), typ=2 → Kartkówka (Chemia 12.11 — «Kartkówka» в UI).
# typ=3 цей рік ще не зустрічався, typ=4 без назви в API — фолбек «typ N».
_EVENT_TYPE_NAMES = {
    1: "🧪 Sprawdzian",
    2: "📝 Kartkówka",
    3: "📚 Praca klasowa",
    4: "📖 Zadanie domowe",
}


# ----------------------------------------------------------------------
# Стан (data/vulcan_notify.json)
# ----------------------------------------------------------------------

_state: dict[str, Any] = {}
_loaded = False


def _apply_defaults() -> None:
    _state.setdefault("chat_ids", [])
    _state.setdefault("lesson_changes", {})   # ключ уроку -> підпис змін (str)
    _state.setdefault("notified_events", {})  # id заходу -> true
    _state.setdefault("remind_done_day", "")  # останній день веч. нагадування
    _state.setdefault("morning_done_day", "") # останній день ранкового дайджеста
    _state.setdefault("muted_chats", [])      # чати, що запросили тишину
    _state.setdefault("fail_counts", {})      # chat_id -> поспіль невдалих відправок


def _ensure_loaded() -> None:
    global _loaded, _state
    if _loaded:
        return
    _loaded = True
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
        except Exception as e:
            log.error(f"не вдалось прочитати {STATE_FILE}: {e}")
            _state = {}
    _apply_defaults()


def _apply_external_state(data: Any) -> None:
    """Setter для state_store: стан із Telegram-снапшоту замінює локальний."""
    global _loaded
    if not isinstance(data, dict):
        raise ValueError("vulcan_notify: очікую об'єкт")
    _state.clear()
    _state.update(data)
    _apply_defaults()
    _loaded = True


def _get_state_for_snapshot() -> dict[str, Any]:
    """Getter для state_store: спершу гарантуємо, що локальний файл прочитано."""
    _ensure_loaded()
    return _state


state_store.register("vulcan_notify", _get_state_for_snapshot,
                     _apply_external_state)


def _save() -> None:
    # гігієна: події, сповіщені >35 днів тому, вже не «новачки» — викидаємо
    _state["notified_events"] = _prune_notified(
        _state.get("notified_events") or {}, datetime.now(TZ).date(),)
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"не вдалось зберегти {STATE_FILE}: {e}")
    # дублюємо в Telegram-снапшот (Koyeb Free: data/ ефемерний)
    state_store.request_save()


# Скільки днів тримати позначку «про цю подію вже сповіщались».
NOTIFIED_TTL_DAYS = 35


def _prune_notified(events: dict[str, Any], today: date) -> dict[str, Any]:
    """Чисті матчі з data/vulcan_notify.json -> зростає повільно, але рік
    за роком. Значення - дата сповіщення (ISO-строка); легаси `true` без
    дати лишаються (їх мало, а видаляти 'не піддається датуванню' ризиково)."""
    cutoff = (today - timedelta(days=NOTIFIED_TTL_DAYS)).isoformat()
    keep: dict[str, Any] = {}
    for k, v in (events or {}).items():
        try:
            d = date.fromisoformat(str(v)[:10])
            if d.isoformat() < cutoff:
                continue                       # протухло — чистимо
        except ValueError:
            pass                               # без дати (легаси) — лишаються
        keep[k] = v
    return keep


# ----------------------------------------------------------------------
# Телеметрія для /health (без мережі: останній цикл перевірки розкладу)
# ----------------------------------------------------------------------

LAST_POLL: dict[str, Any] = {"at": None, "ok": None, "error": "", "days": 0}


def _mark_poll(ok: bool, *, error: str = "", days: int = 0) -> None:
    LAST_POLL.update({
        "at": datetime.now(TZ).isoformat(timespec="seconds"),
        "ok": ok,
        "error": (error or "")[:200],
        "days": days,
    })


def health_info() -> dict[str, Any]:
    """Стан VULCAN-частини для /health. Мережу не чіпає (безпечно для
    health-check кожні кілька секунд)."""
    _ensure_loaded()
    import config
    return {
        "configured": vu.is_configured(),
        "baseline": _state.get("baseline"),
        "targets": len(_target_chats()),
        "tracked_lessons": len(_state.get("lesson_changes") or {}),
        "muted_chats": len(_state.get("muted_chats") or []),
        "last_poll_at": LAST_POLL["at"],
        "last_poll_ok": LAST_POLL["ok"],
        "last_poll_days": LAST_POLL["days"],
        "last_poll_error": LAST_POLL["error"],
        "poll_interval_min": int(config.VULCAN_CHANGES_POLL_MIN or 15),
    }


def remember_chat(chat_id: int) -> None:
    """Запам'ятати чат, де користувались розкладом (ціль для сповіщень).

    Групи/супергрупи в Telegram мають ВІД'ЄМНИЙ chat_id — їх теж реєструємо
    (раніше `cid <= 0` відкидав групи, тому картки йшли лише в приват).
    """
    _ensure_loaded()
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return
    if cid == 0:            # 0 — фейковий/тестовий чат, не реєструємо
        return
    if cid in (m for m in _state["muted_chats"]):
        return  # цей чат запросив тишину — не реєструємо
    if cid not in _state["chat_ids"]:
        _state["chat_ids"].append(cid)
        _save()
        log.info(f"Vulcan notify: додано ціль-чат (всього {len(_state['chat_ids'])})")


def toggle_mute(chat_id: int) -> bool:
    """Перемкнути тишину для чату. True = тепер муте, False = тепер активний."""
    _ensure_loaded()
    cid = int(chat_id)
    muted: list[int] = _state["muted_chats"]
    if cid in muted:
        muted.remove(cid)
        remember_chat(cid)  # повертаємо у цілі
        _save()
        return False
    muted.append(cid)
    _save()
    return True


def is_muted(chat_id: int) -> bool:
    _ensure_loaded()
    return int(chat_id) in _state["muted_chats"]


def _target_chats() -> list[int]:
    from config import VULCAN_NOTIFY_CHATS
    out: list[int] = []
    muted = _state.get("muted_chats") or []
    for s in VULCAN_NOTIFY_CHATS:
        try:
            out.append(int(s))
        except (TypeError, ValueError):
            continue
    for cid in _state.get("chat_ids") or []:
        if cid not in out:
            out.append(int(cid))
    return [c for c in out if c not in muted]


# ----------------------------------------------------------------------
# Чисті функції: підпис змін уроку / формати карток (тестуються офлайн).
# ----------------------------------------------------------------------

def lesson_key(l: dict) -> str:
    """Унікальний ключ уроку для порівнянь між опитуваннями."""
    return "|".join(str(x) for x in (
        l.get("date"), l.get("start"), l.get("end"),
        l.get("subject"), l.get("group", ""),
    ))


def lesson_signature(l: dict) -> str:
    """Канонічний підпис уроку для diff між опитуваннями.

    Рахується зі СТРУКТУРНИХ даних порталу (`raw_changes` з
    `lesson_from_raw`), а не з готового тексту картки. Інакше будь-яка
    правка формулювання («Заміна:» -> «Заміна на:») виглядає як нова
    зміна, і після деплою бот шле дубль усім чатам.

    `raw_changes` — список відсортованих `key=value` рядків на кожен
    елемент `zmiany`; порядок ключів стабільний, порядок елементів
    зберігається (портал іноді переставляє `zmiany`, тому сортуємо весь
    список перед підписом).
    """
    raw = l.get("raw_changes")
    if raw is None:      # урок не з lesson_from_raw (тести/старі кеші)
        raw = sorted(str(c) for c in (l.get("changes") or []))
    sig: dict[str, Any] = {
        "v": 2,
        "c": 1 if l.get("canceled") else 0,
        "raw": sorted(raw),
        "notes": sorted(str(n) for n in (l.get("raw_notes") or [])),
    }
    return json.dumps(sig, ensure_ascii=False, sort_keys=True)


def _legacy_signature(l: dict) -> str:
    """Старий (v1) формат підпису — тільки для міграції state.

    У state до цього оновлення лежать підписи, зроблені з тексту змін.
    Якщо порівнювати лише з новим підписом, перший же цикл після деплою
    вирішив би, що змінились усі уроки, і надіслав дублі карток. Тому
    «незміненим» вважаємо і збіг зі старим форматом; після першого
    збереження state там уже новий підпис, і фолбек стає непотрібним.
    """
    return json.dumps({
        "c": 1 if l.get("canceled") else 0,
        "n": (l.get("cancel_notice") or "")[:120],
        "z": sorted(str(c)[:160] for c in (l.get("changes") or [])),
    }, ensure_ascii=False, sort_keys=True)


def format_changes_card(day_name: str, ddmm: str, items: list[dict]) -> str:
    """Картка змін за один день. items = уроки, де зміни ЩОЙНО з'явились."""
    head = f"🔔 <b>Зміни в розкладі — {day_name}, {ddmm}</b>"
    lines: list[str] = []
    for l in sorted(items, key=lambda x: (x["start"], x["end"])):
        t = f"{l['start']}" if l.get("start") else "--:--"
        subj = "<b>" + _esc(l.get("subject") or "?") + "</b>"
        # вчитель (і кабінет) — щоб картка була самодостатньою: у самій
        # зміні ми вже не повторюємо старе значення, воно стоїть тут
        who = (l.get("teacher") or "").strip()
        room = (l.get("room") or "").strip()
        extra = ", ".join(x for x in (
            who, f"каб. {room}" if room else "",
        ) if x)
        if extra:
            subj += f" ({_esc(extra)})"
        # одна картка = один рядок на урок: назву не повторюємо, навіть якщо
        # змін кілька (вони йдуть через «;» в тому ж рядку)
        notes = [_esc(c) for c in (l.get("changes") or [])]
        if l.get("canceled"):
            reason = f" ({_esc(l['cancel_notice'])})" if l.get("cancel_notice") else ""
            notes.insert(0, f"відмінено{reason}")
        if not notes:
            continue
        bullet = "❌" if l.get("canceled") else "•"
        lines.append(f"{bullet} {t} {subj} — " + "; ".join(notes))
    if not lines:
        return ""
    return head + "\n" + "\n".join(lines)


def format_events_overview(events: list[dict], today: date) -> str:
    """Багатоденний огляд подій (для /sprawdziany): групування по датах."""
    if not events:
        return ""
    by_day: dict[str, list[dict]] = {}
    for e in (events or []):
        if not isinstance(e, dict):
            continue
        d = (str(e.get("data") or ""))[:10]
        by_day.setdefault(d, []).append(e)
    blocks: list[str] = []
    for iso in sorted(by_day):
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        if d < today:
            continue
        day_name = vu.DAY_NAMES_PL[d.weekday()]
        head = f"📅 <b>{day_name}, {d.strftime('%d.%m')}</b>"
        lines = []
        for e in by_day[iso]:
            typ = e.get("typ")
            subj = _esc((e.get("przedmiotNazwa") or "(без предмета)").strip())
            name = _EVENT_TYPE_NAMES.get(typ, f"Подія (typ {typ})")
            lines.append(f"  {name}: <b>{subj}</b>")
        blocks.append(head + "\n" + "\n".join(lines))
    return "\n".join(blocks)


def format_daybefore_card(d: date, events: list[dict]) -> str:
    """Картка «Завтра ...» зі sprawdzian/картківки/дз.

    events = [{'typ', 'przedmiotNazwa', ...}] (сирий формат порталу).
    """
    day_name = vu.DAY_NAMES_PL[d.weekday()] if vu else ""
    head = f"📌 <b>Завтра — {day_name}, {d.strftime('%d.%m')}</b>"
    lines: list[str] = []
    for e in (events or []):
        typ = e.get("typ")
        subj = _esc((e.get("przedmiotNazwa") or "(без предмета)").strip())
        name = _EVENT_TYPE_NAMES.get(typ, f"• Подія (typ {typ})")
        if typ in (1, 2, 3):             # sprawdzian / kartkówka / praca klasowa
            lines.append(f"{name}: <b>{subj}</b>")
        elif typ == 4:                   # zadanie domowe
            lines.append(f"{name} з <b>{subj}</b>")
        else:
            lines.append(f"• {subj}")
    if not lines:
        return ""
    return head + "\n" + "\n".join(lines)


# ----------------------------------------------------------------------
# Дані з порталу (з авто-відновленням сесії через авто-логін).
# ----------------------------------------------------------------------

async def _week_lessons_fresh(client) -> tuple[list[dict], date, date]:
    """Свіжий розклад на today..today+6 -> список уроків (оскільки raw)."""
    today = datetime.now(TZ).date()
    key, _ = await client.account_key()
    raw, _ = await client.plan_raw(
        key,
        vu._iso_range_start(today),
        vu._iso_range_end(today + timedelta(days=6)),
        force=True,
    )
    lessons = []
    for item in raw:
        try:
            lessons.append(vu.lesson_from_raw(item))
        except vu.VulcanBadData:
            continue
    return lessons, today, today + timedelta(days=6)


async def _tomorrow_events_fresh(client) -> tuple[date, list[dict]]:
    """/api/SprawdzianyZadaniaDomowe на завтра (fresh)."""
    key, _ = await client.account_key()
    tomorrow = datetime.now(TZ).date() + timedelta(days=1)
    data, _ = await client.events_raw(
        key,
        vu._iso_range_start(tomorrow),
        vu._iso_range_end(tomorrow),
        force=True,
    )
    return tomorrow, data if isinstance(data, list) else []


async def _on_auth_error() -> bool:
    """Відновлення сесії при VulcanAuthError (публічний API-еквівалент
   控制和 schedule_text): авто-логін, якщо є credentials."""
    return await vu.try_autologin()


async def upcoming_events_text(days: int = 30) -> tuple[str, str]:
    """Публічний API для хендлера /sprawdziany: список подій на N днів.

    Повертає (status, html) — той самий контракт, що schedule_text.
    """
    try:
        client = await vu.get_client()
        try:
            key, _ = await client.account_key()
            d_from = datetime.now(TZ).date()
            d_to = d_from + timedelta(days=int(days or 30))
            data, cached = await client.events_raw(
                key, vu._iso_range_start(d_from), vu._iso_range_end(d_to),
            )
        except vu.VulcanAuthError:
            if not await _on_auth_error():
                raise
            client = await vu.get_client()
            key, _ = await client.account_key()
            data, cached = await client.events_raw(
                key, vu._iso_range_start(datetime.now(TZ).date()),
                vu._iso_range_end(datetime.now(TZ).date() + timedelta(days=int(days or 30))),
            )
    except vu.VulcanDisabled:
        raise
    except (vu.VulcanAuthError, vu.VulcanUnavailable, vu.VulcanBadData) as e:
        return "error", vu._error_text(e)

    today = datetime.now(TZ).date()
    header = (f"🧪 <b>Sprawdziany i zadania domowe</b> "
              f"({today.strftime('%d.%m')} – {(today + timedelta(days=int(days or 30))).strftime('%d.%m')}"
              f") (<b>Dziennik VULCAN</b>)")
    if cached:
        header += " • з кешу"
    body = format_events_overview(data if isinstance(data, list) else [], today)
    if not body:
        return "ok", header + "\n\nЗаписів немає 🎉"
    return "ok", header + "\n\n" + body


async def _morning_cycle(bot) -> bool:
    """Ранковий дайджест: «розклад на сьогодні» + події на сьогодні."""
    _ensure_loaded()
    today = datetime.now(TZ).date()
    status, sched = await vu.schedule_text(today)
    if status != "ok":
        log.warning("Vulcan notify: ранковий дайджест — розклад не дістався")
        return False
    # події сьогодні (справджан/дз на сьогодні)
    today_events: list[dict] = []
    try:
        client = await vu.get_client()
        key, _ = await client.account_key()
        data, _ = await client.events_raw(
            key, vu._iso_range_start(today), vu._iso_range_end(today),
        )
        today_events = [e for e in (data if isinstance(data, list) else [])
                        if isinstance(e, dict) and str((str(e.get("data") or ""))[:10]) == today.isoformat()]
    except vu.VulcanError as e:
        log.warning(f"Vulcan notify: події сьогодні недоступні ({type(e).__name__}: {e})")
    text = "🌅 <b>Доброго ранку!</b>\n\n" + sched
    if today_events:
        events_card = format_daybefore_card(today, today_events).replace(
            "Завтра", "Сьогодні", 1)
        text += "\n\n" + events_card
    ok = await _notify(bot, text)
    if ok:
        log.info("Vulcan notify: надіслано ранковий дайджест")
    return ok


# ----------------------------------------------------------------------
# Фоновий watcher: зміни + нагадування за день.
# ----------------------------------------------------------------------

async def _notify(bot, text: str) -> bool:
    """Розсилка в чат(и). True = хоч один чат отримав.

    Чат, у який 10 разів поспіль не вдалось написати (бот видалений з
    нього), авто-виключається зі списку цілей.
    """
    sent = False
    for cid in _target_chats():
        try:
            await bot.send_message(cid, text)
            sent = True
            if _state.get("fail_counts", {}).get(str(cid), 0):
                _state["fail_counts"][str(cid)] = 0
        except Exception as e:
            fails = int(_state.get("fail_counts", {}).get(str(cid), 0)) + 1
            _state.setdefault("fail_counts", {})[str(cid)] = fails
            if fails >= 10:
                # закономерно мертвий чат: прибираємо, щоб не молотити
                if cid in _state.get("chat_ids", []):
                    _state["chat_ids"].remove(cid)
                _state["fail_counts"][str(cid)] = 0
                _save()
                log.warning(f"Vulcan notify: чат {cid} прибрано зі списку "
                            f"(10 послідовних невдач)")
            else:
                log.warning(f"Vulcan notify: не зміг написати чат {cid}: "
                            f"{type(e).__name__}: {str(e)[:80]} ({fails}/10)")
    if not sent:
        _save()
    return sent


def _lesson_date(l: dict) -> date | None:
    try:
        return date.fromisoformat(str(l.get("date") or "")[:10])
    except ValueError:
        return None


def _compute_changes(lessons: list[dict], stored: dict[str, str],
                     *, seed: bool = False,
                     today: date | None = None) -> tuple[list[dict], dict[str, str]]:
    """Diff поточного стану проти збереженого.

    Повертає (уроки з НОВИМИ змінами, повний новий map).

    seed=True -> перший запуск/файл загублений (Koyeb ephemeral). Раніше
    тут усе тихо «з'їдалось» — і заміни, зроблені до старту бота, не
    приходили НІКОЛИ. Тепер базу зберігаємо, але зміни на сьогодні й далі
    показуємо: краще один раз нагадати, ніж промовчати про заміну.
    today=None -> показати всі зміни (для тестів/ручних викликів).
    """
    now_map: dict[str, str] = {}
    new_changed: list[dict] = []
    for l in lessons:
        has_changes = bool(l.get("canceled") or l.get("changes"))
        k = lesson_key(l)
        sig = lesson_signature(l)
        now_map[k] = sig
        if seed:
            # минулі дні не турбуємо, решту — показуємо як «поточні зміни»
            d = _lesson_date(l)
            if has_changes and (today is None or (d is not None and d >= today)):
                new_changed.append(l)
            continue
        prev = stored.get(k)
        if prev is not None and prev != sig and has_changes:
            # стара версія підпису (текст замість сирих полів) — не зміна,
            # а просто міграція state; інакше деплой = дубль усіх карток
            if prev != _legacy_signature(l):
                new_changed.append(l)
        elif prev is None and has_changes:
            # новий урок ІЗ змінами (напр., вчора вписали заміну на нове заняття)
            new_changed.append(l)
    # підчищення застарілих ключів (уроки минули)
    keep = {k: v for k, v in stored.items() if k in now_map}
    keep.update(now_map)
    return new_changed, keep


async def _changes_cycle(bot) -> None:
    _ensure_loaded()
    stored = _state.setdefault("lesson_changes", {})
    try:
        client = await vu.get_client()
        try:
            lessons, day_from, day_to = await _week_lessons_fresh(client)
        except vu.VulcanAuthError:
            if not await _on_auth_error():
                _mark_poll(False, error="сесія мертва, автологін не вдався")
                log.warning("Vulcan notify: сесія мертва, авто-логін не вдався")
                return
            client = await vu.get_client()
            lessons, day_from, day_to = await _week_lessons_fresh(client)
    except vu.VulcanDisabled:
        _mark_poll(False, error="VULCAN не налаштований")
        return
    except vu.VulcanError as e:
        _mark_poll(False, error=f"{type(e).__name__}: {e}")
        log.warning(f"Vulcan notify: план недоступний ({type(e).__name__}: {e})")
        return

    _mark_poll(True, days=len(lessons))
    seed = "baseline" not in _state
    today = datetime.now(TZ).date()
    changed, new_map = _compute_changes(
        lessons, dict(stored), seed=seed, today=today,
    )
    if seed:
        _state["baseline"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    if not changed:
        _state["lesson_changes"] = new_map
        _save()
        if seed:
            log.info("Vulcan notify: базова снапшот розкладу збережено")
        return

    # групуємо за датою і надсилаємо по днях
    by_day: dict[str, list[dict]] = {}
    for l in changed:
        by_day.setdefault(l["date"], []).append(l)
    delivered: set[str] = set()
    for iso in sorted(by_day):
        items = by_day[iso]
        try:
            d = date.fromisoformat(iso)
        except ValueError:
            continue
        card = format_changes_card(
            vu.DAY_NAMES_PL[d.weekday()], d.strftime("%d.%m"), items,
        )
        if card and await _notify(bot, card):
            delivered.add(iso)
        else:
            log.warning("Vulcan notify: картку змін не доставлено (чатів 0?)")

    # НЕ фіксуємо підписи змін, які не дійшли: інакше вони загубляться
    # назавжди (типово — перший запуск без зареєстрованих чатів).
    if len(delivered) != len(by_day):
        for iso, items in by_day.items():
            if iso in delivered:
                continue
            for l in items:
                k = lesson_key(l)
                if k in stored:
                    new_map[k] = stored[k]      # старий підпис -> повторимо
                else:
                    new_map.pop(k, None)        # новий ключ -> «новий» знову
    _state["lesson_changes"] = new_map
    _save()
    if delivered:
        log.info(f"Vulcan notify: надіслано змін на {len(delivered)} день(ів)")
    if seed:
        log.info("Vulcan notify: базову снапшот збережено; поточні зміни надіслано")


async def _tomorrow_cycle(bot) -> bool:
    """Картка «за завтра»: sprawdzian/картківка/дз.

    True = день закрито (слати більше не треба). False = варто повторити
    наступного циклу: не дістали події з порталу або не змогли доставити.
    """
    _ensure_loaded()
    try:
        client = await vu.get_client()
        try:
            tomorrow, raw_events = await _tomorrow_events_fresh(client)
        except vu.VulcanAuthError:
            if not await _on_auth_error():
                return False
            client = await vu.get_client()
            tomorrow, raw_events = await _tomorrow_events_fresh(client)
    except vu.VulcanDisabled:
        return True
    except vu.VulcanError as e:
        log.warning(f"Vulcan notify: події недоступні ({type(e).__name__}: {e})")
        return False

    notified = _state.setdefault("notified_events", {})
    fresh = [
        e for e in raw_events
        if isinstance(e, dict) and e.get("id") is not None
        and str(e.get("id")) not in notified
    ]
    if not fresh:
        _save()
        return True
    card = format_daybefore_card(tomorrow, fresh)
    if not card:
        _save()
        return True
    ok = await _notify(bot, card)
    if not ok:
        # НЕ позначаємо як надіслане: інакше нагадування губиться назавжди
        # (типово — свіжий деплой, коли в чаті ще ніхто не користувався
        # розкладом, тобто цілей для розсилки немає).
        _save()
        return False
    for e in fresh:
        notified[str(e.get("id"))] = datetime.now(TZ).date().isoformat()
    _save()
    log.info(f"Vulcan notify: надіслано нагадування про завтра ({len(fresh)} подій)")
    return True


async def notify_watcher(bot) -> None:
    """Єдина фаза: keepalive сесії + зміни + нагадування. Запускається з плагіна.

    Об'єднав cycle-полінги: сам цикл підтримує сесію живою (кожне звернення
    до API = keepalive), тому окремий keepalive_watcher більше не потрібен,
    коли цей watcher активний.
    """
    import config
    poll_min = max(5, int(config.VULCAN_CHANGES_POLL_MIN or 15))
    remind_hour = int(config.VULCAN_REMIND_HOUR or 18)
    m_hour = int(config.VULCAN_MORNING_HOUR or 6)
    m_minute = int(config.VULCAN_MORNING_MINUTE or 50)
    log.info(f"Vulcan notify запущено (зміни: кожні {poll_min}хв, "
             f"нагадування: {remind_hour}:00, ранок: {m_hour}:{m_minute:02d}, "
             f"тиша: 00:00–06:00)")
    await asyncio.sleep(20)  # дати боту вставати

    while True:
        now = datetime.now(TZ)

        # --- нічна пауза 00:00–06:00: заміни вночі вносять рідко ---
        if now.hour < 6:
            await asyncio.sleep(30 * 60)
            continue

        _ensure_loaded()

        # --- 1. зміни розкладу (кожне звернення до API тримає сесію живою) ---
        try:
            await _changes_cycle(bot)
        except Exception as e:
            log.exception(f"Vulcan notify: помилка циклу змін: {e}")

        # --- 2. ранковий дайджест (тільки вранці: після рестарту о 22:00
        #        «Доброго ранку» з розкладом уже недоречне) ---
        today = now.date().isoformat()
        morning_due = (now.hour < 12
                       and (now.hour > m_hour
                            or (now.hour == m_hour and now.minute >= m_minute)))
        if morning_due and _state.get("morning_done_day") != today:
            ok = False
            try:
                ok = await _morning_cycle(bot)
            except Exception as e:
                log.exception(f"Vulcan notify: помилка ранкового дайджеста: {e}")
            # прапорець — лише якщо реально закрили день: інакше одна
            # помилка API о 6:50 вбивала дайджест до наступної доби
            if ok:
                _state["morning_done_day"] = today
                _save()

        # --- 3. нагадування за день ---
        if now.hour >= remind_hour and _state.get("remind_done_day") != today:
            ok = False
            try:
                ok = await _tomorrow_cycle(bot)
            except Exception as e:
                log.exception(f"Vulcan notify: помилка циклу нагадувань: {e}")
            if ok:
                _state["remind_done_day"] = today
                _save()

        await asyncio.sleep(poll_min * 60)


def register_watchers(bot, background_tasks) -> None:
    _ensure_loaded()
    if not _target_chats():
        log.info("Vulcan notify: чатів ще немає — активується після першого /rozklad")
        # все одно запускаємо: чат запам'ятається при першому використанні
    import tasks
    if tasks.spawn_watch("vulcan_notify", lambda: notify_watcher(bot),
                         tasks_set=background_tasks) is None:
        return
    log.info("Vulcan notify: watcher зареєстровано (keepalive всередині)")
