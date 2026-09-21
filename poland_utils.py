"""Утиліти для отримання інформації про свята, неробочі дні та торгові
неділі в Польщі без використання платних API або квот Gemini.

- Niedziele handlowe (Торгові неділі): за польським законодавством торгівля
  дозволена у 7 визначених неділь на рік. Розраховується автоматично.
- Święta państwowe (Державні свята): 13 офіційних неробочих днів. Запитуються
  через публічне безкоштовне Nager.Date API (з кешуванням на 24г) та локальним
  fallback-списком.
"""
import datetime
import json
import logging
import time
import urllib.request
import urllib.error
from zoneinfo import ZoneInfo

log = logging.getLogger("Bashma4ek_Bot.poland")

TZ = ZoneInfo("Europe/Warsaw")

# Кеш для свят: {year: (timestamp, [holidays])}
_HOLIDAYS_CACHE: dict[int, tuple[float, list[dict]]] = {}
_CACHE_TTL_SEC = 24 * 3600


def _today() -> datetime.date:
    """Сьогоднішня дата у Варшаві (на Koyeb системний пояс — UTC)."""
    return datetime.datetime.now(TZ).date()


def _get_easter_date(year: int) -> datetime.date:
    """Обчислення дати Великодня (Григоріанський календар) за алгоритмом Meeus/Jones/Butcher."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def get_shopping_sundays(year: int) -> list[datetime.date]:
    """Повертає список 7 офіційних торгових неділь (niedziele handlowe) у Польщі на вказаний рік:
    1. Остання неділя січня
    2. Неділя перед Великоднем (Niedziela Palmowa)
    3. Остання неділя квітня
    4. Остання неділя червня
    5. Остання неділя серпня
    6. Неділя за два тижні до Різдва (перша з двох)
    7. Неділя безпосередньо перед Різдвом
    """
    def _last_sunday_of_month(y: int, m: int) -> datetime.date:
        if m == 12:
            next_month = datetime.date(y + 1, 1, 1)
        else:
            next_month = datetime.date(y, m + 1, 1)
        last_day = next_month - datetime.timedelta(days=1)
        offset = (last_day.weekday() - 6) % 7
        return last_day - datetime.timedelta(days=offset)

    easter = _get_easter_date(year)
    palm_sunday = easter - datetime.timedelta(days=7)

    jan_sunday = _last_sunday_of_month(year, 1)
    apr_sunday = _last_sunday_of_month(year, 4)
    jun_sunday = _last_sunday_of_month(year, 6)
    aug_sunday = _last_sunday_of_month(year, 8)

    christmas = datetime.date(year, 12, 25)
    offset_to_prev_sunday = (christmas.weekday() - 6) % 7
    if offset_to_prev_sunday == 0:
        offset_to_prev_sunday = 7
    dec_sunday_1 = christmas - datetime.timedelta(days=offset_to_prev_sunday)
    dec_sunday_2 = dec_sunday_1 - datetime.timedelta(days=7)

    sundays = sorted({
        jan_sunday,
        palm_sunday,
        apr_sunday,
        jun_sunday,
        aug_sunday,
        dec_sunday_2,
        dec_sunday_1,
    })
    return sundays


def get_static_holidays(year: int) -> list[dict]:
    """13 офіційних державних свят Польщі (fallback без інтернету)."""
    easter = _get_easter_date(year)
    easter_mon = easter + datetime.timedelta(days=1)
    pentecost = easter + datetime.timedelta(days=49)  # Zielone Świątki
    corpus_christi = easter + datetime.timedelta(days=60)  # Boże Ciało

    items = [
        (datetime.date(year, 1, 1), "Nowy Rok", "Новий рік"),
        (datetime.date(year, 1, 6), "Trzech Króli", "Трьох Королів (Богоявлення)"),
        (easter, "Wielkanoc", "Великдень"),
        (easter_mon, "Poniedziałek Wielkanocny", "Великодній понеділок"),
        (datetime.date(year, 5, 1), "Święto Pracy", "Свято Праці"),
        (datetime.date(year, 5, 3), "Święto Konstytucji 3 Maja", "День Конституції 3 Травня"),
        (pentecost, "Zielone Świątki", "Зелені Свята"),
        (corpus_christi, "Boże Ciało", "Боже Тіло"),
        (datetime.date(year, 8, 15), "Wniebowzięcie NMP / Święto Wojska Polskiego", "Внебовзяття / День Війська Польського"),
        (datetime.date(year, 11, 1), "Wszystkich Świętych", "Всіх Святих"),
        (datetime.date(year, 11, 11), "Święto Niepodległości", "День Незалежності Польщі"),
        (datetime.date(year, 12, 25), "Boże Narodzenie (1. dzień)", "Різдво (перший день)"),
        (datetime.date(year, 12, 26), "Boże Narodzenie (2. dzień)", "Різдво (другий день)"),
    ]
    items.sort(key=lambda x: x[0])
    return [
        {"date": d.strftime("%Y-%m-%d"), "localName": pl, "name": ua}
        for d, pl, ua in items
    ]


def fetch_holidays(year: int) -> list[dict]:
    """Отримує офіційні свята Польщі з Nager.Date API або fallback-списку."""
    now = time.time()
    if year in _HOLIDAYS_CACHE:
        cached_time, data = _HOLIDAYS_CACHE[year]
        if now - cached_time < _CACHE_TTL_SEC:
            return data

    try:
        url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/PL"
        req = urllib.request.Request(url, headers={"User-Agent": "BashmakBot/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            holidays = json.loads(resp.read().decode("utf-8"))
            _HOLIDAYS_CACHE[year] = (now, holidays)
            return holidays
    except Exception as e:
        log.warning(f"Не вдалося отримати свята Польщі з Nager.Date ({e}), використовуємо локальний розрахунок")
        fallback = get_static_holidays(year)
        _HOLIDAYS_CACHE[year] = (now, fallback)
        return fallback


def get_sunday_shopping_info(target_date: datetime.date | None = None) -> str:
    """Повертає деталі про торгові неділі відносно поточної або переданої дати."""
    if target_date is None:
        target_date = _today()

    # Знаходимо найближчу неділю
    days_until_sunday = (6 - target_date.weekday()) % 7
    nearest_sunday = target_date + datetime.timedelta(days=days_until_sunday)

    shopping_sundays = get_shopping_sundays(nearest_sunday.year)
    # Якщо кінець року — додаємо неділі наступного
    if nearest_sunday.month == 12:
        shopping_sundays += get_shopping_sundays(nearest_sunday.year + 1)

    is_shopping = nearest_sunday in shopping_sundays

    next_shopping = [s for s in shopping_sundays if s >= target_date and (s != nearest_sunday or not is_shopping)]
    next_shopping_str = next_shopping[0].strftime("%d.%m.%Y") if next_shopping else "не визначено"

    sunday_str = nearest_sunday.strftime("%d.%m.%Y")
    status = "ТОРГОВА (niedziela handlowa) — великі магазини (Biedronka, Lidl, галереї) ВІДКРИТІ" if is_shopping else (
        "НЕторгова (niedziela niehandlowa) — ТРЦ і супермаркети ЗАКРИТІ (працюють тільки Żabka, АЗС, чергові аптеки)"
    )

    lines = [
        f"Найближча неділя ({sunday_str}): {status}.",
    ]
    if not is_shopping:
        lines.append(f"Наступна відкрита торгова неділя: {next_shopping_str}.")

    return "\n".join(lines)


def get_upcoming_holidays(limit: int = 3, target_date: datetime.date | None = None) -> str:
    """Повертає найближчі державні свята та вихідні дні в Польщі."""
    if target_date is None:
        target_date = _today()

    holidays = fetch_holidays(target_date.year)
    if target_date.month >= 11:
        holidays = holidays + fetch_holidays(target_date.year + 1)

    upcoming = []
    for h in holidays:
        try:
            h_date = datetime.datetime.strptime(h["date"], "%Y-%m-%d").date()
            if h_date >= target_date:
                upcoming.append((h_date, h.get("localName", ""), h.get("name", "")))
        except Exception:
            continue

    upcoming.sort(key=lambda x: x[0])
    upcoming = upcoming[:limit]

    if not upcoming:
        return "Найближчих свят не знайдено."

    lines = ["Найближчі державні свята/вихідні в Польщі (dni wolne):"]
    for d, pl_name, ua_name in upcoming:
        diff_days = (d - target_date).days
        day_word = "сьогодні" if diff_days == 0 else ("завтра" if diff_days == 1 else f"через {diff_days} дн.")
        lines.append(f"- {d.strftime('%d.%m.%Y')} ({day_word}): {pl_name} ({ua_name})")

    return "\n".join(lines)


# Ключові слова для детекції запитів про неділі, магазини та свята в Польщі
_POLAND_CALENDAR_KEYWORDS = {
    # неділі і магазини
    "неділя", "неділі", "неділю", "воскресенье", "niedziela", "niedziele", "niedzieli",
    "торгова", "торгові", "торговая", "handlowa", "handlowe", "niehandlowa",
    "бедронка", "бєдронка", "бедронку", "бєдронку", "biedronka", "biedronki",
    "лідл", "лидл", "lidl", "магазин", "магазини", "магазины", "żabka", "жабка",
    # свята і вихідні
    "свято", "свята", "святкові", "праздник", "праздники", "święto", "święta",
    "дні вольне", "dni wolne", "вихідний в польщі", "вихідні в польщі", "выходные в польше",
}


def is_poland_calendar_query(text: str) -> bool:
    """Чи стосується запит торгових неділь, відкритих магазинів або свят у Польщі."""
    t = (text or "").lower()
    return any(w in t for w in _POLAND_CALENDAR_KEYWORDS)


def get_poland_context(query: str) -> str:
    """Генерує контекст для LLM про польські неділі чи свята без використання Gemini або Tavily."""
    today = _today()
    q = (query or "").lower()

    parts = []
    # Якщо питання про неділю або магазини
    if any(w in q for w in ["неділ", "воскресен", "niedziel", "handlow", "бедрон", "biedron", "лідл", "lidl", "магазин"]):
        parts.append("[Інформація про неділі та торгівлю в Польщі]:\n" + get_sunday_shopping_info(today))

    # Якщо питання про свята або вихідні дні
    if any(w in q for w in ["свят", "праздник", "święt", "woln", "вихідн", "выходн"]):
        parts.append("[Найближчі свята та неробочі дні в Польщі]:\n" + get_upcoming_holidays(limit=4, target_date=today))

    # Якщо запит загальний польський — додаємо і неділю, і свята
    if not parts:
        parts.append("[Довідка по Польщі (торгові неділі та свята)]:\n" + get_sunday_shopping_info(today) + "\n\n" + get_upcoming_holidays(limit=3, target_date=today))

    return "\n\n" + "\n\n".join(parts)
