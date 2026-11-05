"""Офлайн-тести user_memory (авто-факти) + user_profiles (/profile).
Запуск: python test_user_memory.py  (без мережі, без Gemini)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import user_memory as um
import user_profiles as up

# --- ізоляція стану між прогонами ---
um._state = {"last_run": "", "users": {}}
um._users = {}
up._profiles = {}


def test_profiles_override() -> None:
    """/profile: динамічний перекриває статичний; id-ключі для тих, хто
    писав у чат; legacy username-ключі читаються; снапшот roundtrip."""
    static_style = up.get_style(username="koserpa")
    assert static_style and up.source(username="koserpa") == "static"
    assert up.get_style(username="@KOSERPA") == static_style

    # динамічний під username (легасі-шлях)
    assert up.set_style(username="koserpa", text="Тестовий профіль")
    assert up.get_style(username="koserpa") == "Тестовий профіль"
    assert up.source(username="koserpa") == "dynamic"

    # той самий человек писав у чат -> id-ключ перекриває і прибирає легасі
    assert up.set_style(user_id=111, username="koserpa", text="Профіль за id")
    assert up.get_style(user_id=111, username="koserpa") == "Профіль за id"
    assert "koserpa" not in up._profiles, "легасі-запис мав бути прибраний"

    # людина БЕЗ @username: профіль за user_id
    assert up.set_style(user_id=777, text="Без юзернейма, але з профілем")
    assert up.get_style(user_id=777) == "Без юзернейма, але з профілем"
    assert up.source(user_id=777) == "dynamic"
    assert up.get_style(username="nevidomiy") == ""  # чужий — порожньо

    # снапшот: roundtrip (id-ключі виживають)
    snap = up._get_snapshot()
    up._profiles.clear()
    up._apply_snapshot(snap)
    assert up.get_style(user_id=777) == "Без юзернейма, але з профілем"
    assert up.get_style(user_id=111, username="koserpa") == "Профіль за id"

    # видалення: і id-, і username-варіанти
    assert up.delete_profile(username="nevidomiy") is False
    assert up.delete_profile(user_id=111, username="koserpa")
    assert up.get_style(user_id=111, username="koserpa") == static_style
    assert up.source(username="koserpa") == "static"
    assert up.delete_profile(user_id=777)
    assert up.get_style(user_id=777) == ""
    # guard-и
    assert not up.set_style(text="x")
    assert not up.set_style(user_id=None, username=None, text="x")
    assert not up.set_style(user_id="не число", text="x")
    print("  /profile: id-ключі, перекриття, снапшот, видалення ✓")


def test_profile_entries_listing() -> None:
    """Список для /profile: динамічні з іменами з реєстру, статичні окремо."""
    um._users = {"id:777": {"username": "", "name": "Без Юзера"}}
    up._profiles = {"id:777": "його профіль"}
    entries = up.profile_entries()
    labels = {e[0]: e for e in entries}
    assert "Без Юзера" in labels, "id-профіль підписується ім'ям з реєстру"
    assert labels["Без Юзера"][2] == "dynamic"
    assert labels["koserpa"][2] == "static"
    print("  /profile: список записів ✓")


def test_parse_facts_json() -> None:
    """Відповідь моделі: новий формат {"users":..., "chat":...}, старий
    плаский формат (сумісність), паркан ```json```, сміття."""
    good = '{"users": {"koserpa": ["купив клавіатуру"]}, "chat": ["грають разом"]}'
    users, chat = um._parse_facts_json(good)
    assert users == {"koserpa": ["купив клавіатуру"]} and chat == ["грають разом"]
    # старий формат без users/chat — все вважається юзерами
    users, chat = um._parse_facts_json('{"koserpa": ["скоро іспит"]}')
    assert users == {"koserpa": ["скоро іспит"]} and chat == []
    fenced = 'Ось факти:\n```json\n{"users": {}, "chat": ["малує 3D"]}\n```'
    users, chat = um._parse_facts_json(fenced)
    assert users == {} and chat == ["малує 3D"]
    assert um._parse_facts_json("не json взагалі") == ({}, [])
    assert um._parse_facts_json('{"users": {"koserpa": "не список"}}') == ({}, [])
    assert um._parse_facts_json('{"users": {"koserpa": ["", "   "]}}') == ({}, [])
    assert um._parse_facts_json("") == ({}, [])
    long = '{"users": {"koserpa": ["' + "х" * 500 + '"]}}'
    users, _ = um._parse_facts_json(long)
    assert max(len(f) for f in users["koserpa"]) <= 200
    print("  parse JSON відповіді моделі ✓")


def test_chat_facts() -> None:
    """Факти про компанію: окремий пул, без дублів, кап, інжект-текст."""
    um._state = {"last_run": "", "users": {}, "chat": []}
    assert um.get_chat_facts_text() == ""
    assert um._merge_chat(["домовились піти в п'ятницю",
                           "Домовились піти в п'ятницю "], "2026-11-05")
    assert len(um._state["chat"]) == 1
    text = um.get_chat_facts_text()
    assert "Пам'ять про компанію" in text and "(05.11)" in text
    # кап тримає найновіші
    old_max = __import__("config").USER_MEMORY_MAX_FACTS
    __import__("config").USER_MEMORY_MAX_FACTS = 2
    try:
        assert um._merge_chat(["другий факт", "третій факт"], "2026-11-05")
        assert [f["t"] for f in um._state["chat"]] == \
            ["другий факт", "третій факт"]   # кап тримає найновіші
    finally:
        __import__("config").USER_MEMORY_MAX_FACTS = old_max
    # снапшот roundtrip
    snap = um._get_snapshot()
    um._state = {"last_run": "", "users": {}, "chat": []}
    um._apply_snapshot(snap)
    assert len(um._state["chat"]) == 2
    print("  chat-факти компанії ✓")


def test_history_message_cap() -> None:
    """Довгі повідомлення (транскрипти голосових/документів) обрізаються
    в історії — головне джерело неконтрольованого росту снапшоту."""
    import bot_state
    import config
    bot_state.clear_history(1)
    long = "х" * 5000
    bot_state.append_history(1, {"role": "user", "parts": [{"text": long}]})
    h = bot_state._history[1]["recent"][-1]
    assert len(h["parts"][0]["text"]) == config.HISTORY_MSG_MAX_CHARS
    assert len(long) == 5000                    # вихідне повідомлення не змінене
    bot_state.clear_history(1)
    print("  кап довжини повідомлень історії ✓")


def test_merge_dedupe_cap_ttl() -> None:
    """Злиття фактів: без дублів, кап найновіших, TTL."""
    assert um._merge_facts("koserpa", ["Факт 1", "факт 1 ", "Факт 2"], "2026-11-03")
    users = um._state["users"]
    assert [f["t"] for f in users["koserpa"]] == ["Факт 1", "Факт 2"]
    assert not um._merge_facts("koserpa", ["Факт 1"], "2026-11-03")

    old_max = __import__("config").USER_MEMORY_MAX_FACTS
    __import__("config").USER_MEMORY_MAX_FACTS = 2
    try:
        assert um._merge_facts("koserpa", ["Факт 3"], "2026-11-04")
        assert [f["t"] for f in users["koserpa"]] == ["Факт 2", "Факт 3"]
    finally:
        __import__("config").USER_MEMORY_MAX_FACTS = old_max

    users["koserpa"].append({"t": "Давнє", "d": "2026-01-01"})
    users["ghost"] = [{"t": "ще давніше", "d": "2025-01-01"}]
    pruned = um._prune(users, datetime(2026, 11, 3))
    assert "ghost" not in pruned
    assert all(f["t"] != "Давнє" for f in pruned["koserpa"])
    print("  merge/cap/TTL фактів ✓")


def test_registry_and_migration() -> None:
    """Реєстр за user_id: працює без @username; легасі-факти мігрують
    під id-ключ при першій появі людини."""
    um._state = {"last_run": "", "users": {}}
    um._users = {}
    # людина з username
    um.note_user(111, "koserpa", "Костя Кравченко")
    assert um.id_key_for_username("koserpa") == "id:111"
    assert um.id_key_for_username("невідомий") == ""
    assert um.display_name("id:111") == "Костя Кравченко"
    # людина БЕЗ username — реєструється так само
    um.note_user(777, None, "Без Юзера")
    assert um.id_key_for_username("") == ""
    assert um._users["id:777"] == {"name": "Без Юзера"}

    # міграція легасі-фактів: старі записи під username -> id-ключ
    um._state["users"]["oldguy"] = [{"t": "старий факт", "d": "2026-11-01"}]
    um.note_user(888, "oldguy", "Old Guy")
    assert um._state["users"].get("oldguy") is None
    assert um._state["users"]["id:888"] == [{"t": "старий факт", "d": "2026-11-01"}]

    # резолв: username -> id, ім'я -> id, перший токен імені -> id
    assert um._resolve_key("koserpa") == "id:111"
    assert um._resolve_key("@koserpa") == "id:111"
    assert um._resolve_key("Костя Кравченко") == "id:111"
    assert um._resolve_key("Костя") == "id:111"
    assert um._resolve_key("Без Юзера") == "id:777"
    assert um._resolve_key("невідомий") == ""
    # легасі-ключ без реєстру знаходиться як є
    um._state["users"]["legacy_user"] = [{"t": "x", "d": "2026-11-01"}]
    assert um._resolve_key("legacy_user") == "legacy_user"
    print("  реєстр user_id + міграція легасі ✓")


def test_memory_text_and_no_username() -> None:
    """Пам'ять за user_id працює для людини БЕЗ @username; злиття
    фактів з кількох ключів без дублів."""
    um._merge_facts("id:777", ["Готує презентацію на п'ятницю"], "2026-11-05")
    text = um.get_memory_text(user_id=777)
    assert "Пам'ять про людину" in text and "(05.11)" in text
    # пошук за ім'ям без username знаходить ту саму людину
    assert um.get_memory_text(None, None, "Без Юзера") == text
    # злиття: той самий факт під легасі-ключем не задвоюється
    um._state["users"]["id:777"].append({"t": "Готує презентацію на п'ятницю",
                                         "d": "2026-11-04"})
    text2 = um.get_memory_text(user_id=777)
    assert text2.count("Готує презентацію на п'ятницю") == 1
    assert um.facts_count(user_id=777) == 1
    assert um.get_memory_text(user_id=999) == ""
    print("  пам'ять без @username + злиття ключів ✓")


def test_run_once_with_fake_gemini() -> None:
    """Повний цикл run_once з підміненим Gemini: факти пишуться під
    id-ключ, день фіксується, повторного виклику того ж дня немає."""
    import bot_state
    import gemini_client

    um._state = {"last_run": "", "users": {}}
    um._users = {"id:111": {"username": "koserpa", "name": "Костя Кравченко"}}

    bot_state.clear_history(1)
    bot_state.append_history(1, {"role": "user", "parts": [
        {"text": "Костя Кравченко: купив нову відеокарту, тепер CS піде"}]})
    bot_state.append_history(1, {"role": "model", "parts": [{"text": "норм, вітаю"}]})

    calls = []

    async def fake_ask(contents, current_date_str, **kwargs):
        calls.append(current_date_str)
        # службовий виклик мусить іти поза персоною
        assert kwargs.get("use_persona") is False
        return ('```json\n{"users": {"koserpa": ["купив нову відеокарту"]}, '
                '"chat": ["грають у CS разом по вечорах"]}\n```')

    saved_ask = gemini_client.ask_gemini
    gemini_client.ask_gemini = fake_ask
    try:
        today = datetime(2026, 11, 3, 21, 30)
        assert asyncio.run(um.run_once(today)) is True
        assert len(calls) == 1
        # модель відповіла username'ом -> факти під id-ключем
        assert um._state["last_run"] == "2026-11-03"
        assert [f["t"] for f in um._state["users"]["id:111"]] == \
            ["купив нову відеокарту"]
        # chat-факти з того ж виклику
        assert [f["t"] for f in um._state["chat"]] == \
            ["грають у CS разом по вечорах"]
        assert asyncio.run(um.run_once(today)) is True
        assert len(calls) == 1
        # снапшот roundtrip
        snap = um._get_snapshot()
        um._state = {"last_run": "", "users": {}}
        um._apply_snapshot(snap)
        assert um.facts_count(user_id=111) == 1
        # невідомі ключі від моделі не падають і не пишуться
        assert um._merge_facts("id:111", ["другий факт"], "2026-11-03")
        assert um.facts_count(user_id=111) == 2
    finally:
        gemini_client.ask_gemini = saved_ask
    print("  run_once: цикл із фейковим Gemini + снапшот ✓")


def test_snapshot_survives_garbage() -> None:
    """Битий/чужий снапшот не ламає стан; id-ключі проходять валідацію."""
    um._apply_snapshot(None)
    um._apply_snapshot("garbage")
    um._apply_snapshot({"users": {"koserpa": ["рядок замість списку",
                                               {"t": "ok", "d": "2026-11-03"}],
                                  "id:777": [{"t": "факт за id", "d": "2026-11-05"}]}})
    assert um.facts_count(username="koserpa") == 1
    assert um.facts_count(user_id=777) == 1
    up._apply_snapshot(None)
    up._apply_snapshot({"profiles": "не dict"})
    print("  битий снапшот не шкодить ✓")


def test_prompt_content() -> None:
    """Промпт: ростер з іменами з реєстру, заборона приватного, JSON-формат."""
    um._users = {"id:111": {"username": "koserpa", "name": "Костя Кравченко"},
                 "id:777": {"username": "", "name": "Без Юзера"}}
    prompt, roster_n = um._build_prompt(["Костя Кравченко: привіт"])
    assert "@koserpa = Костя Кравченко" in prompt
    assert "Без Юзера" in prompt          # людина без username теж у ростері
    assert roster_n >= 2
    assert "ЗАБОРОНЕНО" in prompt and "НЕ вигадуй" in prompt
    assert '"<username>"' in prompt
    print("  промпт витягування ✓")


if __name__ == "__main__":
    test_profiles_override()
    test_profile_entries_listing()
    test_parse_facts_json()
    test_chat_facts()
    test_history_message_cap()
    test_merge_dedupe_cap_ttl()
    test_registry_and_migration()
    test_memory_text_and_no_username()
    test_run_once_with_fake_gemini()
    test_snapshot_survives_garbage()
    test_prompt_content()
    print("ALL TESTS OK")
    sys.exit(0)
