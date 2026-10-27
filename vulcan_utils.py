"""Інтеграція з вебпорталом Dziennik VULCAN (звичайна вебверсія).

Працює з ОНОВЛЕНОЮ вебверсією порталу учня (та сама, що відкривається у
браузері за адресою https://dziennik-uczen.vulcan.net.pl/<місто>/).
Мобільний API UONET+ та eduVULCAN НЕ використовуються.

Як це працює
------------
Авторизація (ручна, один раз):
    https://dziennik-logowanie.vulcan.net.pl/<місто>/Account/Logon
    - CAPTCHA на сторінці входу — це proof-of-work (SHA-256), її
      розв'язує браузер. Ми НЕ відтворюємо і НЕ обходимо цей механізм:
      користувач логінується сам, а модуль лише повторно використовує
      cookie-сесію порталу учня (VULCAN_COOKIES).

Після входу фронтенд порталу тягне дані з same-origin GET-запитів
(перевірено на живій версії порталу 26.7, нічого не вигадано):

    GET {VULCAN_BASE}/api/Context
        -> {"uczniowie":[{"key":"...", "uczen":"...", "oddzial":"2TC", ...}]}
    GET {VULCAN_BASE}/api/PlanZajec?key={key}&dataOd={ISO}&dataDo={ISO}&zakresDanych=2
        -> [ {"data", "godzinaOd", "godzinaDo", "prowadzacy", "przedmiot",
              "podzial", "sala", "zmiany": [...], "zmianyUwagi": [...], ...} ]
    GET {VULCAN_BASE}/api/DniWolne?key={key}&dataOd=&dataDo=
        -> [ {"nazwa":"Wszystkich Świętych", "dataOd", "dataDo"}, ... ]

`zakresDanych=2` — обов'язковий параметр (без нього сервер віддає 404).

Безпека
-------
- Cookie / ключі / параметри сесії НІКОЛИ не пишуться в логи (у логах
  тільки шлях ендпоінта без query-параметрів).
- VULCAN_COOKIES задаються через змінну середовища (локально у .env,
  на Koyeb — через Environment Variables).

Дати/час — у зоні Europe/Warsaw. Хелпер для ручного входу:
    get_vulcan_cookies.py  (локально: pip install playwright)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from yarl import URL

from config import (
    VULCAN_AUTOLOGIN_COOLDOWN_MIN,
    VULCAN_BASE,
    VULCAN_COOKIES,
    VULCAN_LOGIN,
    VULCAN_PASSWORD,
    VULCAN_TTL_MINUTES,
    VULCAN_UA,
)

log = logging.getLogger("Bashma4ek_Bot.vulcan")

TZ = ZoneInfo("Europe/Warsaw")

DAY_NAMES_PL = [
    "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя",
]

_BASE = VULCAN_BASE.rstrip("/")
_TTL = max(60, VULCAN_TTL_MINUTES * 60)
_KEY_TTL = 12 * 3600          # /api/Context кешується довше (легкий ендпоінт)
_AUTH_FAIL_COOLDOWN = 180     # "охолодження" після auth-помилок, щоб не молотити сайт
_STALE_GRACE = 7 * 86400      # скільки давності дозволено кешу при недоступності сайту


# ----------------------------------------------------------------------
# Типізовані помилки (обробляються в handlers.py).
# ----------------------------------------------------------------------

class VulcanError(Exception):
    """Базова помилка інтеграції."""


class VulcanDisabled(VulcanError):
    """VULCAN_COOKIES не задані — фіча вимкнена."""


class VulcanAuthError(VulcanError):
    """Сесію втрачено (cookie протухли або невалідні) — потрібен ручний вхід."""


class VulcanUnavailable(VulcanError):
    """Сайт не відповідає / тайм-аут / 5xx."""


class VulcanBadData(VulcanError):
    """Відповідь не відповідає очікуваній схемі (портал оновили?)."""


# ----------------------------------------------------------------------
# VULCAN_COOKIES: "name=value; name2=value2" або {"name": "value"} JSON.
# ----------------------------------------------------------------------

def parse_cookies(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw[0] in ("{", "["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise VulcanBadData(f"VULCAN_COOKIES — неочікуваний JSON: {e}") from None
        if not data:
            return {}
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        raise VulcanBadData("VULCAN_COOKIES (JSON) має бути об'єктом {\"name\": \"value\"}")
    out: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


# ----------------------------------------------------------------------
# Чисті функції: елемент /api/PlanZajec -> словник уроку; форматтер дня.
# Покриті юніт-тестами в test_vulcan_utils.py.
# ----------------------------------------------------------------------

def _hhmm(iso: str | None) -> str:
    """\"2026-10-26T12:25:00+01:00\" -> \"12:25\"."""
    if not iso or len(iso) < 16:
        return ""
    return iso[11:16]


def lesson_from_raw(item: Any) -> dict[str, Any]:
    """Один елемент з /api/PlanZajec -> словник уроку. Всі поля опційні."""
    if not isinstance(item, dict):
        raise VulcanBadData(f"урок не є об'єктом ({type(item).__name__})")
    day = (item.get("data") or "")[:10]
    if not day:
        raise VulcanBadData("урок без поля 'data'")

    # --- розшифровка замін (тип 'zmiana', за живим трафіком портала) ---
    #   1 = відмінено (informacjeNieobecnosc = причина, напр.
    #       "uczniowie zwolnieni do domu" -> учнів відпустили додому)
    #   7 = заміна вчителя (prowadzacy = хто веде)
    canceled_notice = ""
    canceled = False
    changes: list[str] = []
    for z in item.get("zmiany") or []:
        if not isinstance(z, dict):
            continue
        code = z.get("zmiana")
        absent = (z.get("informacjeNieobecnosc") or "").strip()
        if code == 1 or absent:
            canceled = True
            if absent and not canceled_notice:
                canceled_notice = _translate_notice(absent)
        if code == 7:
            who = (z.get("prowadzacy") or "").strip()
            if who:
                changes.append(f"🔄 Заміна вчителя: {who}")
            sala = (z.get("sala") or "").strip()
            if sala:
                changes.append(f"🔄 Заміна кабінету: {sala}")
    for n in item.get("zmianyUwagi") or []:
        if isinstance(n, str) and n.strip():
            changes.append(f"ℹ️ {n.strip()}")

    group = (item.get("podzial") or "").strip().lstrip("|").strip()

    return {
        "date": day,
        "start": _hhmm(item.get("godzinaOd")),
        "end": _hhmm(item.get("godzinaDo")),
        "subject": (item.get("przedmiot") or "").strip() or "(без назви)",
        "teacher": (item.get("prowadzacy") or "").strip(),
        "room": (item.get("sala") or "").strip(),
        "group": group,
        "changes": changes,
        "canceled": canceled,
        "cancel_notice": canceled_notice,
        "realized": bool(item.get("zrealizowane")),
    }


# Часті польські формулювання причин/rозпису -> українською (для зрозумілості)
_NOTICE_TRANSLATIONS = {
    "uczniowie zwolnieni do domu": "учнів відпустили додому",
    "zajęcia odwołane": "заняття відмінено",
    "lekcja odwołana": "урок відмінено",
}


def _translate_notice(notice: str) -> str:
    """"uczniowie zwolnieni do domu" -> "учнів відпустили додому" (позасхемні — як є)."""
    n = notice.strip().lower()
    for pl, ua in _NOTICE_TRANSLATIONS.items():
        if pl in n:
            return ua if ua else ""
    return notice


def format_lesson_line(idx: int, l: dict) -> str:
    t = f"<code>{l['start']}-{l['end']}</code>" if l["start"] and l["end"] else "<code>--:--</code>"
    canceled = bool(l.get("canceled"))

    parts = [f"<b>{l['subject']}</b>"]
    if l["group"]:
        parts.append(f"({l['group']})")
    if l["teacher"]:
        parts.append(f"— {l['teacher']}")
    if l["room"]:
        parts.append(f"[каб. {l['room']}]")
    main = " ".join(parts)
    line = f"{idx}. {t} {main}"

    if canceled:
        line = f"❌ <s>{idx}. {t} {main}</s>"
        if l.get("cancel_notice"):
            line += f"\n      ↳ відмінено: {l['cancel_notice']}"
        else:
            line += "\n      ↳ заняття відмінено"
    for c in l["changes"]:
        line += f"\n      {c}"
    return line


def format_day(title: str, lessons: list[dict], *, extra: str = "") -> str:
    """HTML-текст розкладу на день."""
    head = f"📅 <b>{title}</b>"
    if extra:
        head += f" {extra}"
    if not lessons:
        return f"{head}: уроків немає 🎉"
    lines = [head + ":"]
    ordered = sorted(lessons, key=lambda x: (x["start"], x["end"]))
    for i, l in enumerate(ordered, start=1):
        lines.append(format_lesson_line(i, l))
    canceled_n = sum(1 for l in ordered if l.get("canceled"))
    summary = f"🧮Уроків: {len(ordered)}"
    if canceled_n:
        summary += f" • відмінено: {canceled_n}❌"
    lines.append(summary)
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Клієнт порталу (aiohttp).
# ----------------------------------------------------------------------

def _iso_range_start(d: date) -> str:
    """День (Warsaw) 00:00 -> UTC ISO '2026-10-25T23:00:00.000Z'."""
    dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=TZ).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _iso_range_end(d: date) -> str:
    dt = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=TZ).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.999Z")


def _week_of(d: date) -> tuple[date, date]:
    start = d - timedelta(days=d.weekday())   # Понеділок цього тижня
    return start, start + timedelta(days=6)


class VulcanClient:
    """Асинхронний клієнт порталу учня, авторизований через cookie."""

    def __init__(self, base_url: str, cookies: dict[str, str]):
        self._base = base_url.rstrip("/")
        self._cookies = cookies
        self._session: aiohttp.ClientSession | None = None
        self._key: str | None = None
        self._key_expires = 0.0
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_auth_fail = 0.0
        self._lock = asyncio.Lock()

    # ---------------- HTTP ----------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar()
            jar.update_cookies(self._cookies, URL(self._base + "/"))
            self._session = aiohttp.ClientSession(
                cookie_jar=jar,
                timeout=aiohttp.ClientTimeout(total=20),
                headers={
                    "User-Agent": VULCAN_UA,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": self._base + "/App",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, path: str, params: dict[str, str] | None) -> Any:
        """GET основного API порталу, з типізованими помилками.

        У лог пишемо ТІЛЬКИ path (без ?query) — параметри містять ключ
        учня, у логи його не складаємо.
        """
        if self._last_auth_fail and time.monotonic() - self._last_auth_fail < _AUTH_FAIL_COOLDOWN:
            raise VulcanAuthError("щойно була невдала спроба авторизації (cooldown)")

        session = await self._ensure_session()
        url = self._base + path
        try:
            async with session.get(url, params=params, allow_redirects=False) as resp:
                status = resp.status
                if status in (301, 302, 303, 307, 308):
                    self._mark_auth_fail()
                    raise VulcanAuthError(f"сесія невалідна (redirect HTTP {status})")
                if status in (401, 403):
                    self._mark_auth_fail()
                    raise VulcanAuthError(f"сесія невалідна (HTTP {status})")
                if status == 409:
                    # 409 "Brak uprawnień" — HttpOnly-сесія відсутня/протухла
                    self._mark_auth_fail()
                    raise VulcanAuthError("сесія невалідна (HTTP 409)")
                if status >= 500:
                    raise VulcanUnavailable(f"сервер VULCAN повернув HTTP {status}")
                if status == 404:
                    raise VulcanBadData("ендпоінт не знайдено (HTTP 404) — портал міг змінитися")
                if status != 200:
                    raise VulcanUnavailable(f"HTTP {status}")
                ctype = resp.headers.get("Content-Type", "")
                if "json" not in ctype:
                    # 200 + HTML — це зазвичай знову сторінка входу
                    self._mark_auth_fail()
                    raise VulcanAuthError(f"очікувався JSON, отримано {ctype or 'не-JSON'}")
                try:
                    return await resp.json()
                except Exception as e:
                    raise VulcanBadData(f"JSON не розбирається: {e}") from None
        except VulcanError:
            raise
        except asyncio.TimeoutError:
            raise VulcanUnavailable("тайм-аут запиту до VULCAN") from None
        except aiohttp.ClientError as e:
            raise VulcanUnavailable(f"мережева помилка: {type(e).__name__}") from None

    def _mark_auth_fail(self) -> None:
        self._last_auth_fail = time.monotonic()

    async def _get_cached(self, path: str, params: dict[str, str], ttl: int,
                          *, force: bool = False) -> tuple[Any, bool]:
        """GET із кешем у пам'яті. Повертає (data, from_cache)."""
        cache_key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        now = time.monotonic()
        hit = self._cache.get(cache_key)
        if hit and not force and (now - hit[0]) < ttl:
            return hit[1], True
        data = await self._request(path, params)
        self._cache[cache_key] = (now, data)
        return data, False

    def purge_cache(self) -> None:
        self._cache.clear()

    # ---------------- API порталу ----------------

    async def account_key(self, *, force: bool = False) -> tuple[str, bool]:
        """Ключ учня (params 'key' для всіх API) через /api/Context."""
        now = time.monotonic()
        if self._key and not force and now < self._key_expires:
            return self._key, True
        ctx = await self._request("/api/Context", None)
        try:
            students = ctx["uczniowie"]
            if not isinstance(students, list) or not students:
                raise KeyError("uczniowie порожній")
            student = next(
                (s for s in students
                 if isinstance(s, dict) and s.get("isUczen") and s.get("aktywny")),
                students[0],
            )
            key = student["key"]
        except (KeyError, TypeError, IndexError) as e:
            raise VulcanBadData(f"/api/Context — несподівана структура: {e}") from None
        if not isinstance(key, str) or not key:
            raise VulcanBadData("/api/Context — порожній 'key' учня")
        self._key = key
        self._key_expires = time.monotonic() + _KEY_TTL
        return key, False

    async def free_days(self, key: str, ts_from: str, ts_to: str,
                        *, force: bool = False) -> tuple[list[dict], bool]:
        data, cached = await self._get_cached(
            "/api/DniWolne", {"key": key, "dataOd": ts_from, "dataDo": ts_to},
            _TTL, force=force,
        )
        if not isinstance(data, list):
            raise VulcanBadData("/api/DniWolne — очікувався масив")
        return data, cached

    async def plan_raw(self, key: str, ts_from: str, ts_to: str,
                       *, force: bool = False) -> tuple[list[dict], bool]:
        """/api/PlanZajec на довільний діапазон. Обов'язково zakresDanych=2."""
        data, cached = await self._get_cached(
            "/api/PlanZajec",
            {"key": key, "dataOd": ts_from, "dataDo": ts_to, "zakresDanych": "2"},
            _TTL, force=force,
        )
        if not isinstance(data, list):
            raise VulcanBadData("/api/PlanZajec — очікувався масив")
        return data, cached

    async def events_raw(self, key: str, ts_from: str, ts_to: str,
                         *, force: bool = False) -> tuple[list[dict], bool]:
        """/api/SprawdzianyZadaniaDomowe: sprawdziany/kartkówki + zadania domowe.

        Елемент: {typ: int (1 = sprawdzian/картківка, 2 = zadanie domowe),
        przedmiotNazwa, data (ISO), hasAttachment, id}.
        """
        data, cached = await self._get_cached(
            "/api/SprawdzianyZadaniaDomowe",
            {"key": key, "dataOd": ts_from, "dataDo": ts_to},
            _TTL, force=force,
        )
        if not isinstance(data, list):
            raise VulcanBadData("/api/SprawdzianyZadaniaDomowe — очікувався масив")
        return data, cached

    async def day_schedule(self, d: date, *, force: bool = False) -> tuple[list[dict], dict | None, bool]:
        """Розклад на день: (уроки, свято|None, from_cache).

        Запит покриває календарний тиждень, що містить d, — один запит
        обслуговує і '/rozklad', і '/rozklad_jutro'.
        """
        ws, we = _week_of(d)
        ts_from, ts_to = _iso_range_start(ws), _iso_range_end(we)

        key, _ = await self.account_key()
        raw, cached = await self.plan_raw(key, ts_from, ts_to, force=force)
        lessons: list[dict] = []
        for item in raw:
            try:
                lessons.append(lesson_from_raw(item))
            except VulcanBadData as e:
                # один кривий елемент не повинен валити весь розклад;
                # структура даних все одно перевіряється тестами
                log.warning(f"пропускаю некоректний елемент PlanZajec: {e}")
        target = d.isoformat()
        lessons = [l for l in lessons if l["date"] == target]

        holiday: dict | None = None
        try:
            days_raw, _ = await self.free_days(key, ts_from, ts_to, force=force)
        except VulcanError as e:
            # DniWolne некритичні: тільки прикраса для свята
            log.warning(f"DniWolne недоступні ({e}); показую день без перевірки свята")
            days_raw = []
        for fd in days_raw:
            od = (fd.get("dataOd") or "")[:10]
            do = (fd.get("dataDo") or "")[:10]
            if od and od <= target <= do:
                holiday = fd
                break
        return lessons, holiday, cached

    # ---------------- graceful fallback на просрочний кеш ----------------

    def stale_plan(self, d: date) -> list[dict]:
        """Якщо сайт лежить — використай навіть просрочний кеш тижня d."""
        if not self._cache:
            return []
        ws, we = _week_of(d)
        prefix = "/api/PlanZajec?"
        ts_from = _iso_range_start(ws)
        for ck, (when, data) in self._cache.items():
            if not ck.startswith(prefix):
                continue
            if f"dataOd={ts_from}" not in ck.split("?")[1]:
                continue
            if time.monotonic() - when > _STALE_GRACE:
                continue
            try:
                parsed: list[dict] = []
                for x in data:
                    try:
                        parsed.append(lesson_from_raw(x))
                    except VulcanBadData:
                        continue
                if not parsed:
                    continue
                return parsed
            except VulcanError:
                continue
        return []


# ----------------------------------------------------------------------
# Синглтон + зручний публічний API для хендлерів.
# ----------------------------------------------------------------------

_client: VulcanClient | None = None
_client_lock = asyncio.Lock()


def is_configured() -> bool:
    """VULCAN доступний, якщо є cookie АБО логін/пароль для авто-логіну."""
    return bool((VULCAN_COOKIES or "").strip()) or autologin_configured()


def autologin_configured() -> bool:
    return bool(VULCAN_LOGIN and VULCAN_PASSWORD)


def set_cookies(raw: str) -> None:
    """Живе оновлення cookie-сесії (з авто-логіну) без перезапуску бота.

    Оновлюємо module-global (імпортований з config на старті значення
    для внутрішнього використання), старий клієнт скидається.
    """
    global VULCAN_COOKIES
    VULCAN_COOKIES = (raw or "").strip()
    reset_client()


# ----------------------------------------------------------------------
# Авто-логін: коли сесію протухло, входимо заново власними credentials
# (env VULCAN_LOGIN / VULCAN_PASSWORD) затуршуючись не більше ніж раз
# на VULCAN_AUTOLOGIN_COOLDOWN_MIN хвилин, щоб не молотити портал.
# ----------------------------------------------------------------------

_autologin_lock = asyncio.Lock()
_last_autologin = 0.0


async def try_autologin() -> bool:
    """Спробувати оновити сесію авто-логіном. False = не змогли/нечас."""
    global _last_autologin
    if not autologin_configured():
        return False
    async with _autologin_lock:
        since = time.monotonic() - _last_autologin
        if _last_autologin and since < VULCAN_AUTOLOGIN_COOLDOWN_MIN * 60:
            log.info(
                f"Vulcan авто-логін: cooldown "
                f"({VULCAN_AUTOLOGIN_COOLDOWN_MIN * 60 - since:.0f}с до наступної спроби)"
            )
            return False
        _last_autologin = time.monotonic()
        import vulcan_autologin
        try:
            raw = await vulcan_autologin.login_and_get_cookies()
        except vulcan_autologin.AutologinDisabled:
            return False
        except vulcan_autologin.AutologinError as e:
            # повідомлення уже очищене (без паролів/куків)
            log.warning(f"Vulcan авто-логін не вдався: {e}")
            return False
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            log.warning(f"Vulcan авто-логін: мережева помилка ({type(e).__name__}: {e})")
            return False
        set_cookies(raw)
        log.info("Vulcan авто-логін: успішно, сесію оновлено")
        return True


def _decode_cookies() -> dict[str, str]:
    try:
        return parse_cookies(VULCAN_COOKIES)
    except VulcanBadData as e:
        raise VulcanAuthError(f"VULCAN_COOKIES задані некоректно: {e}") from None


async def get_client() -> VulcanClient:
    global _client
    if not is_configured():
        raise VulcanDisabled("VULCAN_COOKIES не задані (і VULCAN_LOGIN теж)")
    if not (VULCAN_COOKIES or "").strip():
        # cookie ще немає взагалі — пробуємо авто-логін з нуля
        if not await try_autologin():
            raise VulcanDisabled(
                "авто-логін не вдався: перевір VULCAN_LOGIN / VULCAN_PASSWORD"
            )
    async with _client_lock:
        if _client is None:
            _client = VulcanClient(_BASE, _decode_cookies())
    return _client


def reset_client() -> None:
    """Скинути клієнт (наприклад, після оновлення VULCAN_COOKIES)."""
    global _client
    _client = None


def _auth_error_text() -> str:
    return (
        "🔒 <b>VULCAN</b>: сесію протухло або вона невалідна.\n"
        "Залогінься знову у браузері та онови <code>VULCAN_COOKIES</code>\n"
        "(локально допоможе <code>python get_vulcan_cookies.py</code>)."
    )


def _unavailable_error_text() -> str:
    return (
        "🌐 <b>VULCAN</b>: сайт недоступний або надто довго відповідає.\n"
        "Спробуй пізніше."
    )


def _bad_data_error_text(e: Exception) -> str:
    return (
        "🧩 <b>VULCAN</b>: несподівана структура відповіді (портал міг оновитися).\n"
        f"<code>{str(e)[:200]}</code>"
    )


def _error_text(e: Exception) -> str:
    if isinstance(e, VulcanAuthError):
        return _auth_error_text()
    if isinstance(e, VulcanUnavailable):
        return _unavailable_error_text()
    return _bad_data_error_text(e)


async def _recover_on_auth_error() -> bool:
    """Реакція на VulcanAuthError: чистимо кеш і пробуємо авто-логін.

    True -> виклику треба перестворити клієнта (get_client) і повторити
    запит; False -> підняти помилку далі (старих шляхів не порушили).
    """
    if _client is not None:
        _client.purge_cache()
    return await try_autologin()


async def schedule_text(d: date, *, force: bool = False) -> tuple[str, str]:
    """Публічний API для хендлерів.

    Повертає (status, text):
      'ok'    — готовий HTML текст розкладу;
      'error' — читабельне для людини повідомлення про проблему
                (статичного фолбеку більше немає).
    VulcanDisabled викидається НАРУЖУ — викликач сам вирішує, що показати.
    """
    stale = False
    try:
        client = await get_client()
        try:
            lessons, holiday, cached = await client.day_schedule(d, force=force)
        except VulcanAuthError:
            # сесію протухло -> авто-логін (якщо є credentials) -> повтор
            if not await _recover_on_auth_error():
                raise
            client = await get_client()
            lessons, holiday, cached = await client.day_schedule(d, force=force)
    except (VulcanAuthError, VulcanUnavailable, VulcanBadData) as e:
        if _client is not None:
            stale_lessons = _client.stale_plan(d)
            if stale_lessons:
                stale = True
                lessons = stale_lessons
                holiday = None
                cached = True
            else:
                return "error", _error_text(e)
        else:
            return "error", _error_text(e)

    title = f"{DAY_NAMES_PL[d.weekday()]}, {d.strftime('%d.%m.%Y')}"
    extra = "(<b>Dziennik VULCAN</b>)"
    if cached:
        extra += " • з кешу"
    if stale:
        extra += " • ⚠️ сайт недоступний, показую останні збережені дані"
    if holiday:
        name = holiday.get("nazwa") or "вихідний"
        return "ok", f"📅 <b>{title}</b> — {name} 🎉\n{extra}"
    return "ok", format_day(title, lessons, extra=extra)


async def week_text() -> tuple[str, str]:
    """Реальний тижневий огляд (пн-нд поточного тижня): (status, html).

    Дані за один запит /api/PlanZajec на весь тиждень + кеш, тому повторі
    виклики не навантажують портал повторно.
    """
    today = datetime.now(TZ).date()
    monday = today - timedelta(days=today.weekday())
    friday = monday + timedelta(days=4)

    try:
        client = await get_client()
        try:
            key, _ = await account_key_cached(client)
            raw, _ = await client.plan_raw(
                key, _iso_range_start(monday), _iso_range_end(friday + timedelta(days=2)),
            )
        except VulcanAuthError:
            # сесію протухло -> авто-логін (якщо є credentials) -> повтор
            if not await _recover_on_auth_error():
                raise
            client = await get_client()
            key, _ = await account_key_cached(client)
            raw, _ = await client.plan_raw(
                key, _iso_range_start(monday), _iso_range_end(friday + timedelta(days=2)),
            )
    except VulcanDisabled:
        raise
    except (VulcanAuthError, VulcanUnavailable, VulcanBadData, VulcanError) as e:
        return "error", _error_text(e)

    by_day: dict[str, list[dict]] = {}
    for item in raw:
        try:
            l = lesson_from_raw(item)
        except VulcanBadData:
            continue
        by_day.setdefault(l["date"], []).append(l)

    days_raw = []
    try:
        days_raw, _ = await client.free_days(
            key, _iso_range_start(monday), _iso_range_end(friday + timedelta(days=2)),
        )
    except VulcanError as e:
        log.warning(f"DniWolne недоступні ({e}); тиждень без найменувань свят")

    blocks: list[str] = []
    for i in range(7):
        d = monday + timedelta(days=i)
        title = f"{DAY_NAMES_PL[d.weekday()]}, {d.strftime('%d.%m')}"
        iso = d.isoformat()
        holiday = None
        for fd in days_raw:
            od = (fd.get("dataOd") or "")[:10]
            do = (fd.get("dataDo") or "")[:10]
            if od and od <= iso <= do:
                holiday = fd
                break
        if holiday and not by_day.get(iso):
            name = holiday.get("nazwa") or "вихідний"
            blocks.append(f"📅 <b>{title}</b> — {name} 🎉")
            continue
        if iso not in by_day:
            blocks.append(f"📅 <b>{title}</b> — уроків немає")
            continue
        blocks.append(format_day(title, by_day[iso]))

    header = (
        f"📚 <b>Розклад на тиждень {monday.strftime('%d.%m')} – "
        f"{friday.strftime('%d.%m')}</b> (<b>Dziennik VULCAN</b>)"
    )
    return "ok", header + "\n\n" + "\n\n".join(blocks)


async def account_key_cached(client: VulcanClient) -> tuple[str, bool]:
    """Дрібний helper: ключ учня з client-кешем, шоб week_guess не писав логіку двічі."""
    return await client.account_key()


async def status_text() -> str:
    """Діагностика розкладу-VULCAN /rozklad_status."""
    if not is_configured():
        return "❌ VULCAN: <code>VULCAN_COOKIES</code> не задано (і VULCAN_LOGIN теж) — інтеграція вимкнена."
    autologin_note = (
        " • авто-логін: <b>увімк</b>" if autologin_configured()
        else " • авто-логін: вимк (немає VULCAN_LOGIN/VULCAN_PASSWORD)"
    )
    try:
        client = await get_client()
        _, cached = await client.account_key(force=True)
        n = len(client._cache)
        return (
            f"✅ <b>VULCAN</b> сесія жива. Контекст "
            f"{'з кешу' if cached else 'оновлено'}, записів у кеші: {n}{autologin_note}."
        )
    except VulcanError as e:
        return f"⚠️ VULCAN проблема: {e}{autologin_note}"


# ----------------------------------------------------------------------
# Keepalive: поки бот живий, раз на N хвилин мовчки оновлюємо сесію,
# щоб cookie не протухли за час простою (як тримається відкрита вкладка
# порталу). Це мінімальне навантаження на сайт: 1 GET за цикл.
# ----------------------------------------------------------------------

_keepalive_alive = True


async def keepalive_watcher() -> None:
    from config import VULCAN_KEEPALIVE_MINUTES  # читання свіже на старті watcher'а
    minutes = int(VULCAN_KEEPALIVE_MINUTES or 0)
    if minutes <= 0:
        log.info("Vulcan keepalive вимкнено (VULCAN_KEEPALIVE_MINUTES=0)")
        return
    log.info(f"Vulcan keepalive запущено (кожні {minutes}хв)")
    global _keepalive_alive
    while True:
        await asyncio.sleep(minutes * 60)
        if not is_configured():
            continue
        try:
            client = await get_client()
            await client._request("/api/Context", None)
            if not _keepalive_alive:
                log.info("Vulcan keepalive: сесію відновлено")
            _keepalive_alive = True
        except VulcanAuthError:
            if _keepalive_alive:
                log.warning("Vulcan keepalive: сесію протухло — пробую авто-логін")
            _keepalive_alive = False
            if _client is not None:
                _client.purge_cache()
            if await try_autologin():
                log.info("Vulcan keepalive: сесію відновлено авто-логіном")
                _keepalive_alive = True
        except VulcanError as e:
            log.warning(f"Vulcan keepalive: {e}")
