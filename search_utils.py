"""Все, що стосується зовнішнього пошуку і довідкових даних: Tavily,
курс валют (НБП), погода (Open-Meteo), синхронізація поточної дати."""
import asyncio
import logging
import os
import re
import time

import requests

from config import TAVILY_API_KEY

log = logging.getLogger("Bashma4ek_Bot.search")

# --- Тригери для веб-пошуку -------------------------------------------------
# Рішення "шукати чи ні" приймається в коді напряму (детерміновано), а не
# моделлю через function calling — той підхід виявився нестабільним з
# gemini-3.1-flash-lite.
SEARCH_TRIGGERS = {
    # укр
    "знайди", "гугл", "пошукай", "новини", "погода", "прогноз",
    "курс", "курси", "долар", "євро", "зарплат", "ціна", "ціни",
    "сьогодні", "зараз", "актуальн", "останні", "свіж",
    "інтернет", "найди", "хто такий", "хто така",
    "коли", "скільки коштує", "де знаходиться", "що сталося", "що відбулось",
    # рос (Влад і Саша частіше пишуть/отримують відповіді російською)
    "найди", "погугли", "поищи", "новост", "прогноз погоды",
    "курс", "доллар", "евро", "зарплат", "цена", "цены",
    "сегодня", "сейчас", "актуальн", "последние", "свеж",
    "интернет", "кто такой", "кто такая",
    "когда", "сколько стоит", "где находится", "что случилось", "что произошло",
    # універсальні / інші мови
    "search", "google",
}

MAX_SEARCH_RESULTS = 5
MAX_FETCH_CHARS = 6000

IMAGE_SEARCH_TRIGGERS = {
    "покажи", "покажі", "як виглядає", "як виглядають", "фото", "фотку",
    "картинка", "картинку", "зображення",
    "покажи фото", "пришли фото",
    "как выглядит", "как выглядят", "фотка", "изображение",
}


def is_image_query(text: str) -> bool:
    text_lower = (text or "").lower()
    return any(t in text_lower for t in IMAGE_SEARCH_TRIGGERS)


def needs_web_search(text: str) -> bool:
    """Перевіряє, чи варто автоматично зробити пошук в інтернеті."""
    text_lower = (text or "").lower()
    return any(trigger in text_lower for trigger in SEARCH_TRIGGERS)


# --- Кеш пошукових запитів ---------------------------------------------------
# Якщо кілька людей підряд запитують те саме (наприклад "яка погода?"),
# не варто бити по зовнішньому API двічі — тримаємо результат кілька
# хвилин в пам'яті.
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
    # Проста самоочистка, щоб словник не ріс нескінченно в довгоживучому процесі.
    if len(_search_cache) > 200:
        oldest_key = min(_search_cache, key=lambda k: _search_cache[k][0])
        _search_cache.pop(oldest_key, None)


# --- Курс валют (НБП — Народний банк Польщі) --------------------------------
# Бот у Польщі, тож база — PLN. Швидше й точніше за пошук по інтернету для
# цієї конкретної, дуже частої категорії запитів.
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
# Безкоштовний API без ключа, точніший і швидший за скрейпінг для цієї
# категорії запитів.
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
    """Намагається витягти назву міста після слова 'погода' (напр. 'погода
    у Варшаві'). Якщо не вдалось — використовує місто за замовчуванням."""
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
# Tavily заточений під LLM-агентів: одразу повертає очищений релевантний
# контент по кожному результату (не треба окремо парсити HTML сторінки, як
# із сирими сніпетами DuckDuckGo).
def _tavily_search_sync(query: str, want_images: bool = False) -> tuple[str, list[str]]:
    if not TAVILY_API_KEY:
        log.error("TAVILY_API_KEY не задано в .env — пошук в інтернеті вимкнено")
        return "", []

    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "basic",
        "max_results": MAX_SEARCH_RESULTS,
        "include_answer": True,
    }
    if want_images:
        payload["include_images"] = True
        payload["include_image_descriptions"] = True

    try:
        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error(f"Помилка пошуку Tavily: {e}")
        return "", []

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

    image_urls: list[str] = []
    for img in data.get("images", [])[:3]:
        # З include_image_descriptions=True кожен елемент — dict {"url": ..., "description": ...}
        url = img.get("url") if isinstance(img, dict) else img
        if url:
            image_urls.append(url)

    return text_result, image_urls


async def get_web_context(query: str) -> tuple[str, list[str]]:
    """Головна точка входу: визначає які джерела опитати (курс/погода/
    Tavily), склеює результати і повертає (текст, картинки)."""
    cache_key = query.strip().lower()
    want_images = is_image_query(query)

    cached = _cache_get(cache_key)
    if cached is not None and not want_images:
        log.info(f"Пошук: кеш-хіт для запиту {query!r}")
        return cached, []

    parts: list[str] = []
    image_urls: list[str] = []

    is_curr = is_currency_query(query)
    is_weath = is_weather_query(query)

    if is_curr:
        currency_info = await asyncio.to_thread(_currency_sync, detect_currency_codes(query))
        if currency_info:
            parts.append(currency_info)

    if is_weath:
        weather_info = await asyncio.to_thread(_weather_sync, extract_weather_city(query))
        if weather_info:
            parts.append(weather_info)

    # Tavily виконуємо додатково, якщо в запиті є звичайні пошукові тригери
    # (не тільки курс/погода) — щоб не губити другу частину змішаного
    # запиту типу "яка погода і хто виграв матч".
    should_search_tavily = needs_web_search(query) and not (is_curr or is_weath)
    if not should_search_tavily and (is_curr or is_weath):
        remaining_triggers = SEARCH_TRIGGERS - CURRENCY_TRIGGER_WORDS - WEATHER_TRIGGER_WORDS
        should_search_tavily = any(t in query.lower() for t in remaining_triggers)

    if should_search_tavily:
        tavily_text, image_urls = await asyncio.to_thread(_tavily_search_sync, query, want_images)
        if tavily_text:
            parts.append(tavily_text)

    text_result = "\n".join(parts)

    if text_result and not want_images:
        _cache_set(cache_key, text_result)

    return text_result, image_urls


# --- Синхронізація поточної дати з інтернету ---------------------------------
# System time на хостингу зазвичай і так вірний, але тримаємо це окремо
# від локального часу процесу: якщо хостинг "засне"/зависне на довго
# (наприклад free-план), процес міг не помітити, що час зсунувся.
DATE_SYNC_INTERVAL_SEC = 8 * 3600

# Рядок, що підмішується моделі як "сьогоднішня дата" — оновлюється фоновою
# таскою. Стартове значення — локальний час, щоб бот не був "без дати" до
# першого успішного запиту.
current_date_str: str = time.strftime("%Y-%m-%d (%A)")


def _fetch_current_date_sync() -> str | None:
    """Тягне поточну дату з публічного time-API (без ключа). При невдачі
    повертає None — виклик просто залишить попереднє значення."""
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
