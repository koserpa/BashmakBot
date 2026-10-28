"""Офлайн-тести інтеграції Vulkan (без мережа і без реальних cookie).

Запуск:
    python test_vulcan_utils.py

Перевіряємо:
- parse_cookies (формати: DevTools-рядок, JSON, порожнє, бруд)
- lesson_from_raw (звичайний урок, із заміною, брудні поля, відсутня 'data')
- format_day (порожній день/звичайний, сортування за часом)
- _week_of / _iso_range_* (границі тижня, UTC-конвертація, DST)
- VulcanClient: кешований GET + graceful stale_plan (мережа замокана)

LIVE-тест з реальным сайтом виконується ТОЛЬКІ якщо VULCAN_COOKIES
заданий у середовищі (python test_vulcan_utils.py --live).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone

import vulcan_utils as vu
from vulcan_utils import (
    VulcanBadData,
    VulcanClient,
    format_day,
    format_lesson_line,
    lesson_from_raw,
    parse_cookies,
    _hhmm,
    _iso_range_end,
    _iso_range_start,
    _week_of,
)

FAILED: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  ✅ {name}")
    except Exception as e:  # noqa: BLE001
        FAILED.append(name)
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------

def test_parse_cookies_header_line() -> None:
    got = parse_cookies("a=1; b=x=y; c=3")
    assert got == {"a": "1", "b": "x=y", "c": "3"}, got


def test_parse_cookies_json() -> None:
    got = parse_cookies('{"3S_ARR_EFEB": "tok.en"}')
    assert got == {"3S_ARR_EFEB": "tok.en"}, got


def test_parse_cookies_empty_and_garbage() -> None:
    assert parse_cookies("") == {}
    assert parse_cookies("   ") == {}
    assert parse_cookies("novalidchunk; ;;") == {}


def test_hhmm() -> None:
    assert _hhmm("2026-10-26T12:25:00+01:00") == "12:25"
    assert _hhmm("") == ""
    assert _hhmm(None) == ""
    assert _hhmm("короткий") == ""


RAW_PLAIN = {
    "data": "2026-10-26T00:00:00+01:00",
    "godzinaOd": "2026-10-26T08:00:00+01:00",
    "godzinaDo": "2026-10-26T08:45:00+01:00",
    "prowadzacy": "Примеровий Автор",
    "przedmiot": "Przykładowy przedmiot",
    "podzial": "t.program",
    "sala": "204",
    "zmiany": [],
    "zmianyUwagi": [],
    "zrealizowane": False,
}


def test_lesson_plain() -> None:
    l = lesson_from_raw(RAW_PLAIN)
    assert l["date"] == "2026-10-26"
    assert l["start"] == "08:00" and l["end"] == "08:45"
    assert l["subject"] == "Przykładowy przedmiot"
    assert l["teacher"] == "Примеровий Автор"
    assert l["room"] == "204"
    assert l["group"] == "t.program"      # без '|'
    assert l["changes"] == []
    assert l["realized"] is False


def test_lesson_with_change() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{"zmiana": 7, "prowadzacy": "Wakat WAKAT", "sala": "101"}]
    raw["zmianyUwagi"] = ["Lekcja przeniesiona"]
    l = lesson_from_raw(raw)
    # «було → стало»: старий вчитель/кабінет з самого уроку
    assert "🔄 Заміна вчителя → Wakat WAKAT" in l["changes"]
    assert "🔄 Заміна кабінету → 101" in l["changes"]
    assert "ℹ️ Lekcja przeniesiona" in l["changes"]
    assert l["canceled"] is False


def test_lesson_canceled_notice() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{
        "zmiana": 1, "prowadzacy": None, "sala": None,
        "informacjeNieobecnosc": "uczniowie zwolnieni do domu",
    }]
    l = lesson_from_raw(raw)
    assert l["canceled"] is True
    assert l["cancel_notice"] == "учнів відпустили додому"


def test_lesson_out_of_school_is_not_canceled() -> None:
    """zmiana=4 (вихід класу в кіно/музей) — це примітка, а НЕ відміна уроку."""
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{
        "zmiana": 4, "informacjeNieobecnosc": "Wyjście do kina ",
    }]
    l = lesson_from_raw(raw)
    assert l["canceled"] is False
    assert l["changes"] == ["📌 Wyjście do kina"]


def test_lesson_moved_from_and_to() -> None:
    raw_to = dict(RAW_PLAIN)
    raw_to["zmiany"] = [{
        "zmiana": 6, "godzinaOd": "2026-10-23T15:00:00+02:00",
        "zajecia": "Witryny i aplikacje internetowe",
    }]
    assert any(c.startswith("🕒 Перенесено на 15:00")
               for c in lesson_from_raw(raw_to)["changes"])

    raw_from = dict(RAW_PLAIN)
    raw_from["zmiany"] = [{
        "zmiana": 5, "godzinaOd": "2026-10-23T13:15:00+02:00",
    }]
    assert any(c.startswith("🕒 Перенесено з 13:15")
               for c in lesson_from_raw(raw_from)["changes"])

    # обидва коди на одному уроці -> один рядок «було → стало»
    raw_both = dict(RAW_PLAIN)
    raw_both["zmiany"] = [
        {"zmiana": 5, "godzinaOd": "2026-10-23T13:15:00+02:00"},
        {"zmiana": 6, "godzinaOd": "2026-10-23T15:00:00+02:00"},
    ]
    assert "🕒 Перенесено: 13:15 → 15:00" in lesson_from_raw(raw_both)["changes"]


def test_lesson_group_change() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{
        "zmiana": 15, "zajecia": "Chemia", "grupa": "4TIR",
    }]
    l = lesson_from_raw(raw)
    # показуємо ПЕРЕХІД, але без повтору назви уроку (вона вже в рядку)
    assert "🔀 Заміна на: Chemia (4TIR)" in l["changes"]
    # сирі дані для стабільного diff-підпису (не залежать від формулювання)
    assert l["raw_changes"] == [["grupa=4TIR", "zajecia=Chemia", "zmiana=15"]]


def test_raw_changes_keep_notes() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{"zmiana": 1, "informacjeNieobecnosc": "uczniowie zwolnieni"}]
    raw["zmianyUwagi"] = ["Przynieść zeszyt"]
    l = lesson_from_raw(raw)
    assert l["canceled"] is True
    # причина відміни — у сирому вигляді (переклад не впливає на підпис)
    assert l["raw_changes"] == [["informacjeNieobecnosc=uczniowie zwolnieni", "zmiana=1"]]
    assert l["raw_notes"] == ["Przynieść zeszyt"]


def test_lesson_same_group_lesson_note() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{"zmiana": 15, "zajecia": "Przykładowy przedmiot", "grupa": "4TIR"}]
    assert "🔀 Заняття разом з іншою групою (4TIR)" in lesson_from_raw(raw)["changes"]


def test_lesson_room_change_code8() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{"zmiana": 8, "sala": "101"}]
    assert "🔄 Заміна кабінету → 101" in lesson_from_raw(raw)["changes"]


def test_lesson_unknown_change_code_not_lost() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{"zmiana": 99, "sala": "7"}]
    l = lesson_from_raw(raw)
    assert l["canceled"] is False
    assert "🔄 Заміна кабінету → 7" in l["changes"]


def test_format_lesson_line_canceled_strikethrough() -> None:
    raw = dict(RAW_PLAIN)
    raw["zmiany"] = [{
        "zmiana": 1, "prowadzacy": None, "sala": None,
        "informacjeNieobecnosc": "uczniowie zwolnieni do domu",
    }]
    l = lesson_from_raw(raw)
    line = format_lesson_line(8, l)
    assert line.startswith("❌ <s>8.")
    assert "відмінено: учнів відпустили додому" in line


def test_format_lesson_line_escapes_html() -> None:
    """'&'/'<' у даних порталу не мають ламати HTML-парсинг Telegram."""
    raw = dict(RAW_PLAIN)
    raw["przedmiot"] = "Matematyka & Fizyka <b>"
    raw["prowadzacy"] = "Kowalski & Syn"
    line = format_lesson_line(1, lesson_from_raw(raw))
    assert "Matematyka &amp; Fizyka &lt;b&gt;" in line
    assert "Kowalski &amp; Syn" in line
    assert line.count("<b>") == 1 and line.count("</b>") == 1


def test_lesson_missing_day_raises() -> None:
    try:
        lesson_from_raw({"godzinaOd": "x"})
    except VulcanBadData:
        return
    raise AssertionError("VulcanBadData очікувався")


def test_lesson_group_pipe_stripped() -> None:
    raw = dict(RAW_PLAIN)
    raw["podzial"] = "|t.program"
    assert lesson_from_raw(raw)["group"] == "t.program"


def test_format_day_sorted_and_numbers() -> None:
    late = lesson_from_raw(dict(RAW_PLAIN))
    late["start"], late["end"] = "12:25", "13:10"
    early = lesson_from_raw(dict(RAW_PLAIN))
    text = format_day("Понеділок, 26.10.2026", [late, early])
    lines = text.splitlines()
    assert "пар немає" not in text
    assert lines[1].startswith("1. ") and "08:00-08:45" in lines[1]
    assert lines[2].startswith("2. ") and "12:25-13:10" in text


def test_format_day_empty() -> None:
    text = format_day("Неділя, 01.11.2026", [])
    assert "уроків немає" in text


def test_week_of() -> None:
    ws, we = _week_of(date(2026, 10, 26))   # понеділок
    assert ws == date(2026, 10, 26) and we == date(2026, 11, 1)
    ws, we = _week_of(date(2026, 11, 1))    # неділя того ж тижня
    assert ws == date(2026, 10, 26) and we == date(2026, 11, 1)
    ws, we = _week_of(date(2026, 10, 30))   # п'ятниця
    assert ws == date(2026, 10, 26)


def test_iso_range_winter_time() -> None:
    # наприкінці жовтня Warsaw = UTC+1: понеділок 00:00 місц -> 23:00Z від Нед
    assert _iso_range_start(date(2026, 10, 26)) == "2026-10-25T23:00:00.000Z"
    # local 23:59:59 (+01) -> 22:59:59 UTC (як у трафіку порталу)
    assert _iso_range_end(date(2026, 11, 1)) == "2026-11-01T22:59:59.999Z"
    # літо: UTC+2 (наприклад, вересень)
    assert _iso_range_start(date(2026, 9, 7)) == "2026-09-06T22:00:00.000Z"


# ------------- замокана мережа: кеш, cookie, stale fallback -------------

def _fake_session_factory(calls: dict):
    class FakeResp:
        def __init__(self, data, status=200):
            self._data = data
            self.status = status
            self.headers = {"Content-Type": "application/json; charset=utf-8"}

        async def json(self):
            return self._data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self):
            self.closed = False

        def get(self, url, params=None, allow_redirects=True):
            calls["n"] = calls.get("n", 0) + 1
            calls["last_url"] = url
            return FakeResp(calls["payloads"].get(url, []))

        async def close(self):
            self.closed = True

    return FakeSession()


def test_client_caches_plan() -> None:
    calls = {"payloads": {
        vu._BASE + "/api/Context": {"uczniowie": [{"key": "K1", "isUczen": True, "aktywny": True}]},
        vu._BASE + "/api/PlanZajec": [dict(RAW_PLAIN)],
        vu._BASE + "/api/DniWolne": [],
    }}
    client = VulcanClient(vu._BASE, {"cookie": "x"})
    client._ensure_session = lambda: asyncio.sleep(0, result=_fake_session_factory(calls))

    async def run():
        l1, h1, c1 = await client.day_schedule(date(2026, 10, 26))
        l2, h2, c2 = await client.day_schedule(date(2026, 10, 26))
        assert calls["n"] == 3, calls["n"]          # Context + Plan + DniWolne; вдруге — з кешу
        assert c2 is True and c1 is False
        assert len(l2) == 1 and l2[0]["subject"] == "Przykładowy przedmiot"
        assert h2 is None

    asyncio.run(run())


def test_client_stale_plan_fallback() -> None:
    # у кеші вже є план тижня -> stale_plan віддає уроки навіть "просрочний"
    client = VulcanClient(vu._BASE, {})
    ws, _ = _week_of(date(2026, 10, 26))
    ts_from = _iso_range_start(ws)
    ts_to = _iso_range_end(ws + __import__("datetime").timedelta(days=6))
    ck = "/api/PlanZajec?" + "&".join(
        f"{k}={v}" for k, v in sorted(
            {"key": "K1", "dataOd": ts_from, "dataDo": ts_to, "zakresDanych": "2"}.items()
        )
    )
    client._cache[ck] = (0.0, [dict(RAW_PLAIN)])   # when=0 -> максимально старий
    got = client.stale_plan(date(2026, 10, 26))
    assert len(got) == 1 and got[0]["date"] == "2026-10-26"


def test_client_stale_plan_wrong_week() -> None:
    client = VulcanClient(vu._BASE, {})
    ws, _ = _week_of(date(2026, 10, 26))
    ts_from = _iso_range_start(ws)
    ts_to = _iso_range_end(ws + __import__("datetime").timedelta(days=6))
    ck = "/api/PlanZajec?" + "&".join(
        f"{k}={v}" for k, v in sorted(
            {"key": "K1", "dataOd": ts_from, "dataDo": ts_to, "zakresDanych": "2"}.items()
        )
    )
    client._cache[ck] = (0.0, [dict(RAW_PLAIN)])
    # інший тиждень -> не підходить
    assert client.stale_plan(date(2026, 11, 9)) == []


def test_holiday_detection() -> None:
    calls = {"payloads": {
        vu._BASE + "/api/Context": {"uczniowie": [{"key": "K1", "isUczen": True, "aktywny": True}]},
        vu._BASE + "/api/PlanZajec": [],
        vu._BASE + "/api/DniWolne": [
            {"nazwa": "Wszystkich Świętych",
             "dataOd": "2026-11-01T00:00:00+01:00",
             "dataDo": "2026-11-01T00:00:00+01:00"},
        ],
    }}
    client = VulcanClient(vu._BASE, {"c": "x"})
    client._ensure_session = lambda: asyncio.sleep(0, result=_fake_session_factory(calls))

    async def run():
        lessons, holiday, _ = await client.day_schedule(date(2026, 11, 1))
        assert lessons == []
        assert holiday and holiday["nazwa"] == "Wszystkich Świętych"

    asyncio.run(run())


# ---------------------------------------------------------------------
# LIVE-тести (лише з VULCAN_COOKIES у середовищі)
# ---------------------------------------------------------------------

def run_live() -> None:
    assert vu.is_configured(), "VULCAN_COOKIES не задані у середовищі"
    target = date.today()

    async def run():
        client = await vu.get_client()
        key, _ = await client.account_key(force=True)
        assert isinstance(key, str) and key
        raw, _ = await client.plan_raw(*(
            [key, _iso_range_start(_week_of(target)[0]), _iso_range_end(_week_of(target)[1])]
        ))
        assert isinstance(raw, list)
        status, text = await vu.schedule_text(target)
        print(f"  LIVE ключ = довжини {len(key)}, уроків у тижні: {len(raw)}")
        print(f"  LIVE status = {status}")
        print(text[:600])
        assert status in ("ok", "error")

    try:
        asyncio.run(run())
    finally:
        # прибрати warning "Unclosed client session"
        client = vu._client  # noqa: SLF001
        if client is not None:
            asyncio.run(client.close())
            vu._client = None



def main() -> int:
    print("Офлайн-тести vulcan_utils:")
    for name, fn in sorted({k: v for k, v in globals().items()
                            if k.startswith("test_") and callable(v)}.items()):
        check(name, fn)
    if "--live" in sys.argv:
        print("LIVE-тести (реальний сайт):")
        check("live_schedule", run_live)
    if FAILED:
        print(f"\nПРОВАЛЕНО {len(FAILED)}: {', '.join(FAILED)}")
        return 1
    print("\nВсе ОК.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
