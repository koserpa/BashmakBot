"""Авто-логін у Dziennik VULCAN (вебверсія) чистим HTTP, без браузера.

Головне відкриття налагодженням (жовтень 2026): WAF/farm VULCAN на
WS-Fed endpoint'ах (Fs/Ls) віддає 404 не по заголовках, а по TLS-відбитку
клієнта — питонівський TLS (aiohttp) блокується, а таких же самих запитів
Chrome-TLS проходить. Тому ланцюг виконується через curl_cffi з
імперсацією Chrome (це все ще чистий HTTP, БЕЗ браузера — тільки
фінгерпрінт TLS/HTTP2, як у Chrome).

Перевірений ланцюг (Chrome trace):
  1. GET  {LOGIN_HOST}/{town}/Account/Logon?returnUrl=...        -> 200
  2. POST {LOGIN_HOST}/{town}/Account/QueryUserInfo
     (login+token+nonce+nonceSignature) -> 200 {ShowCaptcha: bool}
  3. CAPTCHA: proof-of-work SHA-256 (порт captcha.js) ТІЛЬКИ якщо
     ShowCaptcha=true; інакше порожній captcha-response (як у Chrome).
  4. POST {LOGIN_HOST}/{town}/Account/Logon (Login, Haslo, token,
     captcha-response) -> 302
  5. GET  .../bytom/Fs/Ls?wa=...        -> 200 (WS-Fed авто-submit форма
     з прихованими wresult/wa/wctx)
  6. POST dziennik-uczen/.../Account/Login?returnUrl=/bytom/App -> 302
  7. GET  .../bytom/App -> 200 — cookie порталу учня готові.

БЕЗПЕКА
-------
- Логін/пароль лише з env (VULCAN_LOGIN / VULCAN_PASSWORD).
- У логи НЕ пишуться: пароль, куки, captcha-response, токени, wresult.
  Тільки факт успіх/помилка з короткою, очищеною причиною.
- PoW розв'язується чесним перебором, без обхідних шляхів.
"""
from __future__ import annotations

import asyncio
import hashlib
import html as html_mod
import json
import logging
import os
import re
from urllib.parse import quote, urljoin, urlparse

from curl_cffi.requests import AsyncSession

from config import VULCAN_BASE

log = logging.getLogger("Bashma4ek_Bot.vulcan.autologin")

TOWN = (VULCAN_BASE.rstrip("/").rsplit("/", 1)[-1] or "bytom").lower()
LOGIN_HOST = "https://dziennik-logowanie.vulcan.net.pl"
PORTAL_HOST = "dziennik-uczen.vulcan.net.pl"
PORTAL_ROOT = f"https://{PORTAL_HOST}/"

_MAX_HOPS = 12
_IMPERSONATE = "chrome"


class AutologinError(Exception):
    """Авто-логін не вдався (повідомлення без секретів)."""


class AutologinDisabled(AutologinError):
    """VULCAN_LOGIN / VULCAN_PASSWORD не задані."""


# ----------------------------------------------------------------------
# URL входу (як у Chrome-ланцюзі — returnUrl з https-схемою.
# ----------------------------------------------------------------------

def build_login_url() -> str:
    """Посилання на сторінку входу з повним returnUrl-ланцюгом до /App."""
    inner = (
        f"{LOGIN_HOST}/{TOWN}/Fs/Ls"
        "?wa=wsignin1.0"
        f"&wtrealm={quote('https://' + PORTAL_HOST + '/' + TOWN + '/Account/Login?returnUrl=/' + TOWN + '/App', safe='')}"
        f"&wctx={quote('auth=student&nslo=1', safe='')}"
    )
    outer = f"{LOGIN_HOST}/{TOWN}/Account/Logon?returnUrl={quote(inner, safe='')}"
    return outer


# ----------------------------------------------------------------------
# PoW-CAPTCHA (точний порт captcha.js).
# ----------------------------------------------------------------------

def solve_pow(seed: str, difficulty: int, limit: int = 1_000_000_000) -> int:
    """Один раунд PoW: перший nonce, де перші 4 байти SHA-256 < difficulty."""
    base = hashlib.sha256(seed.encode("utf-8"))
    for nonce in range(1, limit):
        h = base.copy()
        h.update(str(nonce).encode("ascii"))
        if int.from_bytes(h.digest()[:4], "big") < difficulty:
            return nonce
    raise AutologinError(f"PoW: nonce не знайдено за {limit} спроб")


def solve_pow_response(challenge: str, difficulty: int, rounds: int) -> str:
    """`rounds` раундів: сідо наступного = challenge + попередні без роздільників."""
    if rounds <= 0:
        return ""
    nonces: list[int] = []
    for _ in range(rounds):
        nonces.append(solve_pow(challenge + "".join(str(n) for n in nonces), difficulty))
    return ";".join(str(n) for n in nonces)


# ----------------------------------------------------------------------
# Парсинг сторінки входу.
# ----------------------------------------------------------------------

_ATTR_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"'
    r'|value="([^"]+)"[^>]*name="__RequestVerificationToken"'
)
# input-тег, у якого ">" МОЖЕ бути всередині значення атрибута
# (html-кейс VULCAN: &lt; ескейпили, а '>' залишали сируим)
_INPUT_TAG_RE = re.compile(
    r'<input\b(?:[^>"\']|"[^"]*"|\'[^\']*\')*>', re.IGNORECASE | re.DOTALL
)
_ATTR_NAME_RE = re.compile(r'\bname=["\']([^"\']+)["\']')
_ATTR_VALUE_RE = re.compile(r'\bvalue=["\']([^"\']*)["\']')
_CHALLENGE_RE = re.compile(r'data-challenge="([^"]+)"')
_DIFFICULTY_RE = re.compile(r'data-difficulty="(\d+)"')
_ROUNDS_RE = re.compile(r'data-rounds="(\d+)"')
_JS_NONCE_RE = re.compile(r"nonce:\s*'([^']+)'")
_JS_SIGNATURE_RE = re.compile(r"signature:\s*'([^']+)'")
# Сторінка порталу учня: /App/<ключ> (та insight SPA-роутингу) або /App
_APP_PATH_RE = re.compile(rf"/{re.escape(TOWN)}/App(/\S+)?$", re.IGNORECASE)


def parse_login_page(html_text: str) -> dict[str, object]:
    """Отримання всіх не-секретних полів з HTML сторінки входу."""
    tok = _ATTR_TOKEN_RE.search(html_text)
    token = (tok.group(1) or tok.group(2)) if tok else None
    m_d = _DIFFICULTY_RE.search(html_text)
    m_r = _ROUNDS_RE.search(html_text)
    m_c = _CHALLENGE_RE.search(html_text)
    m_n = _JS_NONCE_RE.search(html_text)
    m_s = _JS_SIGNATURE_RE.search(html_text)
    return {
        "token": token,
        "challenge": m_c.group(1) if m_c else None,
        "difficulty": int(m_d.group(1)) if m_d else 0,
        "rounds": int(m_r.group(1)) if m_r else 0,
        "js_nonce": m_n.group(1) if m_n else "",
        "js_signature": m_s.group(1) if m_s else "",
    }


def _login_error_message(html_text: str) -> str | None:
    """Очищений текст помилки форми входу (без значень полів)."""
    for pattern in (
        r'<span class="[^"]*field-validation-error[^"]*"[^>]*>([^<]{3,160})<',
        r'<div class="[^"]*message-error[^"]*"[^>]*>([^<]{3,160})<',
        r'<div class="[^"]*validation-summary-errors[^"]*"[^>]*>\s*<ul>\s*<li>([^<]{3,160})<',
        r'<span class="message-[^"]*"[^>]*>([^<]{3,160})<',
    ):
        m = re.search(pattern, html_text)
        if m:
            txt = re.sub(r"\s+", " ", m.group(1)).strip()
            return txt[:160]
    if 'id="Haslo"' in html_text and "/App/" not in html_text:
        return "логін/пароль не прийнято порталом"
    return None


def _parse_autosubmit_form(html_text: str) -> dict | None:
    """WS-Fed авто-submit сторінка: <form action> з прихованими wresult/wa/wctx.

    Головну форму входу (з Haslo / Account/Logon) НІКОЛИ не пере-сабмітимо.
    Значення декодується від HTML-entities (браузер теж декодує перед POST).
    """
    for m_form in re.finditer(
        r'<form\b[^>]*action=["\']([^"\']+)["\'][^>]*>(.*?)</form>',
        html_text, re.IGNORECASE | re.DOTALL,
    ):
        action, inner = m_form.group(1), m_form.group(2)
        if not action or "Account/Logon" in action or 'id="Haslo"' in inner:
            continue
        fields: dict[str, str] = {}
        for tag in _INPUT_TAG_RE.finditer(inner):
            tag_text = tag.group(0)
            if not re.search(r'type=["\']hidden["\']', tag_text, re.IGNORECASE):
                continue
            nm = _ATTR_NAME_RE.search(tag_text)
            if not nm:
                continue
            vl = _ATTR_VALUE_RE.search(tag_text)
            fields[nm.group(1)] = html_mod.unescape(vl.group(1)) if vl else ""
        if fields:
            return {"action": action, "fields": fields}
    return None


def _portal_cookie_string(session: AsyncSession) -> str:
    """Cookie порталу учня з curl-сесії -> 'name=value; name2=value2'."""
    out: list[str] = []
    try:
        d = session.cookies.get_dict(domain=PORTAL_HOST)
        out = [f"{k}={v}" for k, v in d.items()]
    except Exception:
        pass
    if not out:
        try:
            jar = session.cookies.jar
            for c in jar:
                cd = (c.domain or "").lstrip(".")
                if PORTAL_HOST == cd or PORTAL_HOST.endswith("." + cd) or cd == PORTAL_HOST:
                    out.append(f"{c.name}={c.value}")
        except Exception:
            out = [f"{k}={v}" for k, v in session.cookies.get_dict().items()]
    return "; ".join(out)


def credentials_configured() -> bool:
    return bool(os.getenv("VULCAN_LOGIN", "").strip()
                and os.getenv("VULCAN_PASSWORD", ""))


def _at_app(url: str) -> bool:
    return bool(_APP_PATH_RE.search(urlparse(url).path))


# ----------------------------------------------------------------------
# Сам акт входу.
# ----------------------------------------------------------------------

async def login_and_get_cookies() -> str:
    """Повний авто-логін. Повертає рядок VULCAN_COOKIES порталу учня.

    Викидає AutologinDisabled (credentials не запаравлені) або
    AutologinError (нічого з етапів не пройшло; повідомлення без секретів).
    """
    login = os.getenv("VULCAN_LOGIN", "").strip()
    password = os.getenv("VULCAN_PASSWORD", "")
    if not login or not password:
        raise AutologinDisabled("VULCAN_LOGIN / VULCAN_PASSWORD не задані")

    url = build_login_url()

    async with AsyncSession(impersonate=_IMPERSONATE, timeout=120) as session:
        # --- 1. GET сторінки входу ------------------------------------------------------------
        resp = await session.get(url)
        if resp.status_code != 200:
            raise AutologinError(f"сторінка входу: HTTP {resp.status_code}")
        page = parse_login_page(resp.text)
        if not page["token"]:
            raise AutologinError("немає __RequestVerificationToken на сторінці входу")

        # --- 2. QueryUserInfo (returns ShowCaptcha) ----------------------------------------------
        show_captcha = True  # fallback: розв'язуємо PoW чесно, якщо крок не вдався
        try:
            q = await session.post(
                f"{LOGIN_HOST}/{TOWN}/Account/QueryUserInfo",
                data={
                    "login": login,
                    "__RequestVerificationToken": page["token"],
                    "nonce": page["js_nonce"],
                    "nonceSignature": page["js_signature"],
                },
            )
            data = json.loads(q.text or "{}")
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                show_captcha = bool(data["data"].get("ShowCaptcha"))
        except Exception:
            pass  # ShowCaptcha, який поки True -> розв'язуємо PoW

        # --- 3. PoW або пустий captcha-response (як у Chrome) ---
        if show_captcha:
            if not page["challenge"]:
                raise AutologinError("сайт вимагає CAPTCHA, але не віддав challenge")
            log.info("Vulcan авто-логін: CAPTCHA увімкнена — розв'язую PoW")
            captcha_response = await asyncio.to_thread(
                solve_pow_response,
                str(page["challenge"]), int(page["difficulty"]), int(page["rounds"]),
            )
        else:
            captcha_response = ""  # Chrome шле empty, коли PoW вимкнено

        # --- 4. POST форми входу ---
        resp = await session.post(
            url,
            data={
                "Login": login,
                "Haslo": password,
                "__RequestVerificationToken": page["token"],
                "captcha-response": captcha_response,
            },
            allow_redirects=False,
        )
        log.info(f"Vulcan авто-логін: POST Logon -> HTTP {resp.status_code}")
        html = resp.text
        current = str(resp.url)
        if resp.status_code == 200:
            err = _login_error_message(html) or "вхід не прийнято порталом"
            raise AutologinError(f"невірні облікові дані: {err}")
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise AutologinError(f"форма входу: HTTP {resp.status_code}")

        # --- 5. Ланцюг редиректів + авто-submit WS-Fed форм ---
        hops = 0
        while hops < _MAX_HOPS:
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if not loc:
                    raise AutologinError("redirect без Location")
                nxt = urljoin(current, loc)
                resp = await session.get(nxt, allow_redirects=False)
                current = str(resp.url)
                html = resp.text
                log.info(f"Vulcan авто-логін: GET {urlparse(nxt).path} -> {resp.status_code}")
                hops += 1
                continue
            if resp.status_code != 200:
                raise AutologinError(f"ланцюг входу: HTTP {resp.status_code} "
                                     f"({urlparse(current).path})")
            if _at_app(current):
                break
            form = _parse_autosubmit_form(html)
            if form is None:
                raise AutologinError(
                    f"ланцюг входу не досяг порталу учня "
                    f"(зупинились на {urlparse(current).path})"
                )
            nxt = urljoin(current, form["action"])
            resp = await session.post(nxt, data=form["fields"], allow_redirects=False)
            log.info(
                f"Vulcan авто-логін: WS-Fed POST {urlparse(nxt).path} -> {resp.status_code}"
            )
            html = resp.text
            current = str(resp.url)
            hops += 1

        if not _at_app(current):
            raise AutologinError("не потрапили на портал учня")

        # --- 6. Cookie порталу учня ---
        cookie_string = _portal_cookie_string(session)
        if not cookie_string:
            raise AutologinError("вхід пройшов, але cookie порталу не отримано")
        return cookie_string
