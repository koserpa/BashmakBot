"""Smoke-тест: реальні команди /rozklad, /rozklad_jutro, /rozklad_status.

Симулює aiogram Message достатньо, щоб викликалися плагінові хендлери.
Виконує LIVE запити до VULCAN (потрібні VULCAN_COOKIES у .env).
"""
from __future__ import annotations

import asyncio
import sys


class FakeMessage:
    def __init__(self, text: str):
        self.text = text
        # для vulcan_notify.remember_chat (тест-чат, не впливає на бота)
        from types import SimpleNamespace
        self.chat = SimpleNamespace(id=0)

    async def reply(self, txt: str, **kwargs):
        print("----------reply----------")
        print(txt)
        print("-------------------------")


async def main() -> int:
    import plugins.vulcan as pv

    await pv.cmd_rozklad(FakeMessage("/rozklad"))
    await pv.cmd_rozklad_jutro(FakeMessage("/rozklad_jutro"))
    await pv.cmd_rozklad_status(FakeMessage("/rozklad_status"))
    # текстовий тригер без слеша + рос/польськ варіанти
    await pv.cmd_rozklad_text(FakeMessage("розклад післязавтра"))
    await pv.cmd_rozklad_text(FakeMessage("расписание на среду"))
    await pv.cmd_rozklad_text(FakeMessage("план на п'ятницю"))
    await pv.cmd_rozklad_text(FakeMessage("Бот розклад на цю середу"))
    # дата в аргументі
    await pv.cmd_rozklad(FakeMessage("/rozklad 2 листопада"))
    # реальний тижневий огляд
    await pv.cmd_rozklad(FakeMessage("/rozklad тиждень"))

    # парсери: без виклику мережі — лише transform
    now = __import__("datetime").datetime.now(pv.TZ).date()
    cases = {
        "2.11": (11, 2), "02.11.2026": (11, 2), "2-11": (11, 2),
        "2 листопада": (11, 2), "2 listopada": (11, 2), "2го грудня": (12, 2),
        "31.02": None, "грібен": None,
    }
    for arg, expect in cases.items():
        got = pv._parse_date_arg(arg)
        if expect is None:
            assert got is None, f"{arg} -> {got}"
            print(f"  date '{arg}' -> None ✅")
        else:
            mo, dy = expect
            assert got is not None and got.month == mo and got.day == dy, f"{arg} -> {got}"
            if got < now:
                got = got.replace(year=got.year + 1)
            print(f"  date '{arg}' -> {got} ✅")
    from datetime import timedelta
    rel_cases = {
        "післязавтра": 2, "послезавтра": 2, "pojutrze": 2,
        "через 3 днів": 3, "через 5 днів": 5, "za 3 dni": 3,
    }
    for arg, off in rel_cases.items():
        got = pv._parse_relative_arg(arg)
        exp = now + timedelta(days=off)
        assert got == exp, f"{arg} -> {got}, expected {exp}"
        print(f"  relative '{arg}' -> {got} ✅")
    for wd in ("вівторок", "ср", "п'ятниця", "niedziela", "вторник"):
        got = pv._parse_relative_arg(wd)
        exp = pv._next_weekday(pv._WEEKDAYS[wd])
        assert got == exp, f"{wd} -> {got}, expected {exp}"
        assert got >= now, wd
        print(f"  weekday '{wd}' -> {got} ✅")
    assert pv._parse_relative_arg("тюлюлюм") is None
    assert pv._parse_relative_arg("") is None
    print("  relative garbage -> None ✅")
    # прийменник на початку
    assert pv._parse_relative_arg("на післязавтра") == now + timedelta(days=2)
    got = pv._parse_date_arg("на 2.11")
    assert got is not None and got.month == 11 and got.day == 2, got
    print("  prep-stripped 'на післязавтра' / 'на 2.11' ✅")
    # обгортки «на цю середу», «наступну п'ятницю» і рос/польськ ключові слова
    assert pv._parse_relative_arg("на цю середу") == pv._next_weekday(2)
    assert pv._parse_relative_arg("наступну п'ятницю") == pv._next_weekday(4)
    assert pv._target_from_arg("розклад на цю середу") == pv._next_weekday(2)
    assert pv._target_from_arg("расписание на среду") == pv._next_weekday(2)
    assert pv._target_from_arg("план на п'ятницю") == pv._next_weekday(4)
    # як у реальному ході: хендлер відрізає "Бот, " → аргумент починається з "розклад"
    assert pv._target_from_arg("розклад післязавтра") == now + timedelta(days=2)
    print("  'на цю середу' / 'наступну п'ятницю' / рос+польськ ключові слова ✅")

    # чисте закриття (без "Unclosed client session")
    import vulcan_utils as vu
    if vu._client is not None:
        await vu._client.close()
        vu._client = None
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
