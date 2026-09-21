"""Все, що стосується зовнішнього пошуку і довідкових даних: Tavily,
курс валют (НБП), погода (Open-Meteo), синхронізація поточної дати."""
import asyncio
import logging
import os
import re
import string
import time

import requests

from config import TAVILY_API_KEY

log = logging.getLogger("Bashma4ek_Bot.search")

# --- Грубі тригери — перший бар'єр (без витрат квоти) ----------------------
# Якщо жодного з цих слів немає — LLM взагалі не кликається.
# Список свідомо вузький: тільки слова, що МАЙЖЕ завжди означають реальний
# пошук (не "коли підемо гуляти", не "курс лекцій").
SEARCH_TRIGGERS = {
    # укр — явні пошукові наміри
    "знайди", "пошукай", "погугли", "загугли", "новини", "погода", "прогноз",
    "долар", "євро", "гривня", "злотий",
    "актуальн", "останні новини", "свіжі новини",
    "скільки коштує", "де знаходиться", "що сталося", "що відбулось",
    "хто такий", "хто така", "розклад", "результат матч",
    # рос
    "найди", "поищи", "погугли", "новост", "прогноз погоды",
    "доллар", "евро", "гривны",
    "последние новости", "свежие новости",
    "сколько стоит", "где находится", "что случилось", "что произошло",
    "кто такой", "кто такая", "расписание",
    # en / universal
    "search", "google", "find me", "latest news", "current price",
    "who is", "what is", "when did", "how much",
    # польська — для запитів про Польщу
    "znajdź", "poszukaj", "aktualne", "cena", "kurs",
}

# Слова, що можуть входити до SEARCH_TRIGGERS але в даному контексті майже
# ніколи не означають реальний пошук — використовуємо як стоп-список
# щоб не кликати LLM на очевидно нерелевантні запити.
_COARSE_FALSE_POSITIVE_PATTERNS = [
    r"\bкурс\s+лекц",        # "курс лекцій"
    r"\bкурс\s+навч",        # "курс навчання"
    r"\bкурсов[иі]\s+роб",   # "курсова робота"
    r"\bкурс\s+програм",     # "курс програмування"
    r"\bрозклад\s+уроків",   # "розклад уроків" (часто жартівливий контекст)
]

MAX_SEARCH_RESULTS = 3          # топ-3 джерела в промпт
MAX_FETCH_CHARS = 1500          # символів на результат (було 6000)

# Маркер, який підмішується в промпт при невдалому пошуку
SEARCH_FAILED_MARKER = "__SEARCH_FAILED__"

IMAGE_SEARCH_TRIGGERS = {
    "покажи", "покажі", "як виглядає", "як виглядають", "фото", "фотку",
    "картинка", "картинку", "зображення",
    "покажи фото", "пришли фото",
    "как выглядит", "как выглядят", "фотка", "изображение",
}

# Ключові слова для новинного запиту
_NEWS_KEYWORDS = {
    "новини", "новость", "новости", "новину",
    "останні", "последние", "latest", "свіжі", "свеж",
    "що сталося", "що відбулось", "что случилось", "что произошло",
    "what happened", "breaking",
}


def is_image_query(text: str) -> bool:
    text_lower = (text or "").lower()
    return any(t in text_lower for t in IMAGE_SEARCH_TRIGGERS)


def _is_news_query(query: str) -> bool:
    """Чи схожий запит на новинний? Використовується для Tavily topic=news."""
    q = (query or "").lower()
    return any(kw in q for kw in _NEWS_KEYWORDS)


def _normalize_query(q: str) -> str:
    """Нормалізує запит для використання як кеш-ключа:
    нижній регістр, без пунктуації, зайвих пробілів."""
    q = q.lower()
    # прибираємо знаки пунктуації
    q = q.translate(str.maketrans("", "", string.punctuation + '«»„"'))
    # стискаємо пробіли
    return " ".join(q.split())


def needs_web_search(text: str) -> bool:
    """Грубий фільтр першого рівня: чи є в тексті хоча б один тригер?
    Якщо так — треба додатково перевірити через LLM."""
    text_lower = (text or "").lower()

    # Перевіряємо стоп-патерни — якщо збіг є, одразу False
    for pat in _COARSE_FALSE_POSITIVE_PATTERNS:
        if re.search(pat, text_lower):
            return False

    return any(trigger in text_lower for trigger in SEARCH_TRIGGERS)


# --- Кеш пошукових запитів ---------------------------------------------------
SEARCH_CACHE_TTL_SEC = 5 * 60
_search_cache: dict[str, tuple[float, str]] = {}


def _cache_get(key: str) -> str | None:
    entry = _search_cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > SEARCH_CACHE_TTL_SEC:
        _search_cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: str) -> None:
    _search_cache[key] = (time.time(), value)
    if len(_search_cache) > 200:
        oldest_key = min(_search_cache, key=lambda k: _search_cache[k][0])
        _search_cache.pop(oldest_key, None)


# --- Курс валют (НБП — Народний банк Польщі) --------------------------------
CURRENCY_TRIGGER_WORDS = {
    "курс", "курси", "курсы", "долар", "доллар", "євро", "евро",
    "гривня", "гривны", "гривень", "злотий", "злотых", "злотого", "фунт",
}

CURRENCY_KEYWORDS = {
    "USD": ["долар", "доллар", "usd", "$"],
    "EUR": ["євро", "евро", "eur", "€"],
    "UAH": ["гривня", "гривны", "гривень", "грн", "uah", "₴"],
    "GBP": ["фунт", "gbp", "£"],
}


def is_currency_query(text: str) -> bool:
    text_lower = text.lower()
    return any(w in text_lower for w in CURRENCY_TRIGGER_WORDS)


def detect_currency_codes(text: str) -> list[str]:
    text_lower = text.lower()
    return [code for code, keywords in CURRENCY_KEYWORDS.items()
            if any(kw in text_lower for kw in keywords)]


def _currency_sync(codes: list[str]) -> str:
    codes = codes or ["USD", "EUR"]
    lines = []
    for code in codes:
        try:
            resp = requests.get(
                f"https://api.nbp.pl/api/exchangerates/rates/A/{code}/?format=json",
                timeout=6,
            )
            resp.raise_for_status()
            data = resp.json()
            rate = data["rates"][0]["mid"]
            date = data["rates"][0]["effectiveDate"]
            lines.append(f"- 1 {code} = {rate} PLN (курс НБП станом на {date})")
        except Exception as e:
            log.error(f"Помилка отримання курсу {code} з НБП: {e}")

    if not lines:
        return ""
    return "\n\n[Актуальний курс валют]:\n" + "\n".join(lines)


# --- Погода (Open-Meteo) -----------------------------------------------------
WEATHER_TRIGGER_WORDS = {"погода", "погоду", "погоди", "прогноз погоды", "прогноз погоди"}
DEFAULT_WEATHER_CITY = os.getenv("DEFAULT_WEATHER_CITY", "Bytom")
_WEATHER_STOPWORDS = {
    "сьогодні", "зараз", "завтра", "яка", "буде", "у",
    "сегодня", "сейчас", "завтра", "какая", "будет",
}


def is_weather_query(text: str) -> bool:
    text_lower = text.lower()
    return any(w in text_lower for w in WEATHER_TRIGGER_WORDS)


def extract_weather_city(text: str) -> str:
    match = re.search(
        r"погод[аиу]?\s*(?:в|у|на)?\s*([A-Za-zА-Яа-яЇїІіЄєҐґ\-]{3,30})",
        text,
        re.IGNORECASE,
    )
    if match:
        candidate = match.group(1).strip()
        if candidate.lower() not in _WEATHER_STOPWORDS:
            return candidate
    return DEFAULT_WEATHER_CITY


def _weather_sync(city: str) -> str:
    try:
        geo_resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "uk"},
            timeout=6,
        )
        geo_resp.raise_for_status()
        results = geo_resp.json().get("results")
        if not results:
            return ""
        loc = results[0]
        found_name = loc.get("name", city)
        country = loc.get("country", "")

        forecast_resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "current": "temperature_2m,apparent_temperature,precipitation,wind_speed_10m",
                "timezone": "auto",
            },
            timeout=6,
        )
        forecast_resp.raise_for_status()
        current = forecast_resp.json().get("current", {})
        if not current:
            return ""

        return (
            f"\n\n[Поточна погода — {found_name}, {country}]:\n"
            f"Температура: {current.get('temperature_2m')}°C "
            f"(відчувається як {current.get('apparent_temperature')}°C)\n"
            f"Вітер: {current.get('wind_speed_10m')} км/год, "
            f"опади: {current.get('precipitation')} мм"
        )
    except Exception as e:
        log.error(f"Помилка отримання погоди для {city!r}: {e}")
        return ""


# --- Загальний пошук в інтернеті (Tavily) ------------------------------------
def _tavily_search_sync(
    query: str,
    want_images: bool = False,
    is_news: bool = False,
) -> tuple[str, list[str]]:
    """Виконує пошук через Tavily.

    - is_news=True: topic="news", days=3, search_depth="advanced"
    - is_news=False: topic="general", search_depth="advanced" (після LLM-класифікації
      запит вже підтверджений — варто шукати якісно)
    - Повертає SEARCH_FAILED_MARKER замість порожнього рядка при помилці,
      щоб у промпт можна було підмішати явне попередження моделі.
    """
    if not TAVILY_API_KEY:
        log.error("TAVILY_API_KEY не задано в .env — пошук в інтернеті вимкнено")
        return SEARCH_FAILED_MARKER, []

    payload: dict = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",   # завжди advanced — запит вже перевірений LLM
        "max_results": MAX_SEARCH_RESULTS,
        "include_answer": True,
    }

    if is_news:
        payload["topic"] = "news"
        payload["days"] = 3
    else:
        payload["topic"] = "general"

    if want_images:
        payload["include_images"] = True
        payload["include_image_descriptions"] = True

    try:
        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=12)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Помилка пошуку Tavily: {e}")
        return SEARCH_FAILED_MARKER, []

    lines = ["\n\n[Знайдена актуальна інформація з інтернету]:"]

    answer = data.get("answer")
    if answer:
        lines.append(f"Коротка відповідь: {answer}")

    for r in data.get("results", []):
        title = r.get("title", "")
        content = r.get("content", "")
        url = r.get("url", "")
        if len(content) > MAX_FETCH_CHARS:
            content = content[:MAX_FETCH_CHARS] + "...[текст обрізано]"
        lines.append(f"- {title}: {content} ({url})")

    text_result = "" if len(lines) == 1 else "\n".join(lines)
    if not text_result:
        return SEARCH_FAILED_MARKER, []

    image_urls: list[str] = []
    for img in data.get("images", [])[:3]:
        url = img.get("url") if isinstance(img, dict) else img
        if url:
            image_urls.append(url)

    return text_result, image_urls


def classify_query(query: str) -> set[str]:
    """Визначає, які спеціалізовані джерела (currency, weather, poland, timetable) потрібні.
    Tavily тепер підключається не тут, а після LLM-класифікації в get_web_context."""
    import poland_utils
    import timetable_utils

    categories: set[str] = set()
    if is_currency_query(query):
        categories.add("currency")
    if is_weather_query(query):
        categories.add("weather")
    if poland_utils.is_poland_calendar_query(query):
        categories.add("poland")
    if timetable_utils.is_timetable_query(query):
        categories.add("timetable")
    return categories


async def get_web_context(
    query: str,
    chat_history: list[dict] | None = None,
) -> tuple[str, list[str]]:
    """Головна точка входу: двоступеневий фільтр + пошук.

    Крок 1: грубий фільтр (needs_web_search) — без витрат квоти.
    Крок 2: LLM-класифікація (classify_search_query) — дешевий запит,
            повертає очищений запит або None.
    Кешування по нормалізованому очищеному запиту.
    При невдалому Tavily — підмішується явне попередження моделі.
    """
    # Lazy import — уникаємо циклу на рівні модулів
    from gemini_client import classify_search_query
    import poland_utils
    import timetable_utils

    want_images = is_image_query(query)
    chat_history = chat_history or []

    # --- Спеціалізовані джерела (валюта, погода, польський календар, розклад) ---
    categories = classify_query(query)
    parts: list[str] = []
    image_urls: list[str] = []

    if "currency" in categories:
        currency_info = await asyncio.to_thread(_currency_sync, detect_currency_codes(query))
        if currency_info:
            parts.append(currency_info)

    if "weather" in categories:
        weather_info = await asyncio.to_thread(_weather_sync, extract_weather_city(query))
        if weather_info:
            parts.append(weather_info)

    if "poland" in categories:
        poland_info = poland_utils.get_poland_context(query)
        if poland_info:
            parts.append(poland_info)

    if "timetable" in categories:
        timetable_info = timetable_utils.get_schedule_context(query)
        if timetable_info:
            parts.append(timetable_info)

    # --- Tavily: двоступеневий фільтр ---
    # Для картинок LLM-класифікацію пропускаємо — тригер вже спрацював
    # надійно (is_image_query), запит передаємо як є.
    # Якщо запит вже повністю покрито локальними джерелами (валюта/погода/Польща/розклад)
    # без явного запиту на свіжі новини/веб — економимо запит до Tavily!
    local_covered = bool(categories) and not want_images
    has_news_intent = _is_news_query(query)

    should_search_tavily = want_images or (
        needs_web_search(query)
        and (not local_covered or has_news_intent)
    )
    refined_query: str | None = None

    if should_search_tavily:

        if want_images:
            # Зображення — одразу в Tavily, без класифікатора
            refined_query = query
        else:
            # Крок 2: LLM вирішує остаточно і нормалізує запит
            refined_query = await classify_search_query(query, chat_history)

            if refined_query is None:
                log.info(f"Пошук: LLM вирішив НЕ шукати для {query!r}")
                # Якщо є вже результати (валюта/погода) — повертаємо їх
                if parts:
                    text_result = "\n".join(parts)
                    _cache_set(_normalize_query(query), text_result)
                    return text_result, []
                return "", []

    # --- Перевірка кешу по нормалізованому запиту ---
    cache_key = _normalize_query(refined_query or query)
    cached = _cache_get(cache_key)
    if cached is not None and not want_images:
        log.info(f"Пошук: кеш-хіт для {refined_query or query!r}")
        if parts:
            return "\n".join(parts) + cached, []
        return cached, []

    # --- Формуємо фінальний запит з датою для новин ---
    effective_query = refined_query or query
    is_news = _is_news_query(effective_query) or _is_news_query(query)

    if is_news:
        date_str = get_current_date_str()
        if date_str and date_str[:10] not in effective_query:
            effective_query = f"{effective_query} {date_str[:10]}"
        log.info(f"Пошук (новини): {effective_query!r}")
    else:
        log.info(f"Пошук: {effective_query!r} (оригінал: {query!r})")

    # --- Виклик Tavily ---
    if refined_query is not None:
        tavily_text, image_urls = await asyncio.to_thread(
            _tavily_search_sync, effective_query, want_images, is_news
        )

        if tavily_text == SEARCH_FAILED_MARKER:
            # Tavily впав — додаємо явне попередження моделі
            parts.append(
                "\n\n[⚠ Пошук в інтернеті не вдався — "
                "не вигадуй актуальні факти, відповідай лише на основі "
                "загальних знань і чітко скажи, що не маєш свіжих даних]"
            )
            log.warning(f"Tavily не відповів для запиту {effective_query!r}")
        elif tavily_text:
            parts.append(tavily_text)

    text_result = "\n".join(parts)

    if text_result and not want_images and SEARCH_FAILED_MARKER not in text_result:
        _cache_set(cache_key, text_result)

    return text_result, image_urls


# --- Синхронізація поточної дати з інтернету ---------------------------------
DATE_SYNC_INTERVAL_SEC = 8 * 3600
current_date_str: str = time.strftime("%Y-%m-%d (%A)")


def _fetch_current_date_sync() -> str | None:
    try:
        resp = requests.get(
            "https://timeapi.io/api/time/current/zone",
            params={"timeZone": "Europe/Warsaw"},
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json()
        return f"{data['year']:04d}-{data['month']:02d}-{data['day']:02d} ({data['dayOfWeek']})"
    except Exception as e:
        log.error(f"Не вдалось отримати поточну дату з інтернету: {e}")
        return None


async def refresh_current_date() -> None:
    global current_date_str
    fetched = await asyncio.to_thread(_fetch_current_date_sync)
    if fetched:
        current_date_str = fetched
        log.info(f"Поточна дата оновлена: {current_date_str}")


async def date_sync_watcher():
    """Раз на DATE_SYNC_INTERVAL_SEC оновлює current_date_str з інтернету."""
    while True:
        await refresh_current_date()
        await asyncio.sleep(DATE_SYNC_INTERVAL_SEC)


def get_current_date_str() -> str:
    return current_date_str


_WEEKEND_DAY_NAMES = {"saturday", "sunday"}


def is_weekend() -> bool:
    """Чи зараз вихідний (субота/неділя) — за назвою дня тижня."""
    match = re.search(r"\(([A-Za-z]+)\)", current_date_str)
    if not match:
        return False
    return match.group(1).strip().lower() in _WEEKEND_DAY_NAMES
