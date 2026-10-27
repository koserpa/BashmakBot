"""Офлайн-тести для text_utils (regex-матчери тригерів) та парсера
часу reminder_utils.parse_time_spec. Жодної мережі, жодного Gemini.

Запуск: python test_text_utils.py
"""
from __future__ import annotations

import os

# Детермінованість: на CI немає .env, а TRIGGER_NAMES читається в config.py
# при імпорті. Задаємо імена ДО імпорту text_utils/config, щоб тести не
# залежали від оточення (локального .env).
os.environ["TRIGGER_NAMES"] = "Башмак,Башмачок"

import sys
import time

from text_utils import build_matcher, strip_trigger, wants_forced_voice


def test_build_matcher() -> None:
    m = build_matcher(["курс", "долар*", "скільки коштує"])
    # без зірочки — ціле слово, не префікс
    assert m.search("який курс") and not m.search("курсор не лізь")
    assert not m.search("спарринг")          # «пара» всередині іншого слова
    # зірочка — це початок слова
    assert m.search("доларів і центів")
    assert m.search("долар")
    # фраза з пробілом
    assert m.search("скільки коштує PS5")
    print("  build_matcher ✓")


def test_strip_trigger() -> None:
    # тригер-ім'я на початку
    assert strip_trigger("Башмак, привіт", "bashmak_bot") == "привіт"
    # @згадка на початку (тріммінг прибирає і знак питання)
    assert strip_trigger("@bashmak_bot погода?", "bashmak_bot") == "погода"
    # згадка В КІНЦІ («привіт Башмак!») теж прибирається
    assert strip_trigger("привіт Башмак!", "bashmak_bot") == "привіт"
    # без тригера текст не змінюється (окрім тримінгу)
    assert strip_trigger("привіт", "bashmak_bot") == "привіт"
    print("  strip_trigger ✓")


def test_forced_voice() -> None:
    assert wants_forced_voice("скажи вголос")
    assert wants_forced_voice("озвуч як працює css")
    assert wants_forced_voice("скинь войс")
    assert not wants_forced_voice("привіт, шо як ти")
    print("  wants_forced_voice ✓")


def test_parse_time_spec() -> None:
    import reminder_utils as ru
    now = time.time()

    # відносний: 15m
    ts, rest = ru.parse_time_spec("15m піти їсти")
    assert ts is not None and abs(ts - now - 900) < 5
    assert rest == "піти їсти"

    # комбінація 1h30m (з нівельованими latin/кириличними одиницями)
    ts, rest = ru.parse_time_spec("1h30м здати проект")
    assert ts is not None and abs(ts - now - 5400) < 5
    assert rest == "здати проект"

    # укр/рос одиниці
    ts, _ = ru.parse_time_spec("2 хвилини воду")
    assert ts is not None and abs(ts - now - 120) < 5
    ts, _ = ru.parse_time_spec("45 секунд")
    assert ts is not None and abs(ts - now - 45) < 5
    ts, _ = ru.parse_time_spec("2 днів чекати")
    assert ts is not None and abs(ts - now - 2 * 86400) < 5

    # абсолютний час HH:MM — в майбутньому (сьогодні або завтра)
    ts, rest = ru.parse_time_spec("23:59 порахувати дз")
    assert ts is not None and ts > now
    assert rest == "порахувати дз"
    # формат «8.00» (крапка) теж валидний
    ts, _ = ru.parse_time_spec("23.59")
    assert ts is not None and ts > now

    # мусор -> None + підказка
    ts, msg = ru.parse_time_spec("хз коли")
    assert ts is None and "Не вдалося розпізнати" in (msg or "")
    ts, msg = ru.parse_time_spec("")
    assert ts is None and "Вкажи час" in (msg or "")
    print("  parse_time_spec ✓")


if __name__ == "__main__":
    test_build_matcher()
    test_strip_trigger()
    test_forced_voice()
    test_parse_time_spec()
    print("ALL TESTS OK")
    sys.exit(0)
