"""Офлайн-тести vulcan_autologin (без мережі) + жива перевірка (--live-fake).

    python test_vulcan_autologin.py              # офлайн
    python test_vulcan_autologin.py --live-fake  # ланцюг входу з ФЕЙКОВИМИ даними
"""
from __future__ import annotations

import asyncio
import hashlib
import sys

import vulcan_autologin as va


# ---- зразок реальної сторінки входу (анонімізовано, структура 1:1) ----
SAMPLE_LOGIN_HTML = """
    <script>
        var querySignature = {
            nonce: '639286444536762364',
            signature: 'F3JE37UZDASIDTOQ3ZKF7SZD7A3LKPXDHHIXGC2Z6UZX43KRBMPCWGBNQW5YCJVJNIQGKWJAAF724VPW7V44DPU5RB2BBXBNG24XMFUJJ2R2U'
        };
    </script>
    <div class="captcha-wrapper input-components" data-challenge="9230546f-65e7-428b-93c4-673367e04f9a" data-difficulty="100000" data-rounds="15">
    <input id="captcha-response" name="captcha-response" type="hidden"></input>
    <input name="__RequestVerificationToken" type="hidden" value="TOKEN123abc" />
    <input id="Login" name="Login" type="text" value="" />
    <input id="Haslo" name="Haslo" type="password" />
"""

SAMPLE_AUTOFORM_HTML = """
<html><body><form action="https://dziennik-uczen.vulcan.net.pl/bytom/Account/Login"
 method="post">
<input type="hidden" name="wa" value="wsignin1.0" />
<input type="hidden" name="wresult" value="&lt;token&gt;" />
<input type="hidden" name="wctx" value="auth=student" />
<noscript><input type="submit"/></noscript></form></body></html>
"""


def test_pow_round_small() -> None:
    """Контрольний малий difficulty: nonce знайдений і підпорядковується умові."""
    n = va.solve_pow("abc-challenge", 50000)
    h = hashlib.sha256(f"abc-challenge{n}".encode()).digest()
    assert int.from_bytes(h[:4], "big") < 50000
    # умова не тримається для попереднього nonce (перший придатний)
    if n > 1:
        h = hashlib.sha256(f"abc-challenge{n-1}".encode()).digest()
        assert int.from_bytes(h[:4], "big") >= 50000
    print(f"  PoW small: challenge='abc-challenge' -> nonce={n} ✓")


def test_pow_offline() -> None:
    """Порт captcha.js: правильний формат відповіді 'n1;n2;...' та сідо-ланцюг."""
    challenge = "9230546f-65e7-428b-93c4-673367e04f9a"
    resp = va.solve_pow_response(challenge, 200000, 4)
    parts = resp.split(";")
    assert len(parts) == 4 and all(p.isdigit() for p in parts), resp
    # перевіряємо сідо кожного раунду точно як у captcha.js:
    seed = challenge
    acc = ""
    for p in parts:
        n = int(p)
        h = hashlib.sha256(f"{seed}{n}".encode()).digest()
        assert int.from_bytes(h[:4], "big") < 200000, (seed, n)
        acc += p
        seed = challenge + acc
    print(f"  PoW rounds: {resp[:60]}... ✓ (ідентично captcha.js)")


def test_build_url() -> None:
    url = va.build_login_url()
    assert url.startswith("https://dziennik-logowanie.vulcan.net.pl/bytom/Account/Logon?returnUrl=")
    assert url.count("?") == 1, "returnUrl має бути закодований одним ?"
    import urllib.parse
    q = urllib.parse.parse_qs(url.split("?", 1)[1])
    assert "returnUrl" in q and "wa=wsignin1.0" in q["returnUrl"][0]
    assert "Fs/Ls" in q["returnUrl"][0]
    print(f"  login URL: закодований, один '?', returnUrl цілий ✓")


def test_parse_login_page() -> None:
    page = va.parse_login_page(SAMPLE_LOGIN_HTML)
    assert page["token"] == "TOKEN123abc", page["token"]
    assert page["challenge"] == "9230546f-65e7-428b-93c4-673367e04f9a"
    assert page["difficulty"] == 100000
    assert page["rounds"] == 15
    assert page["js_nonce"] == "639286444536762364"
    assert page["js_signature"].startswith("F3JE37UZDA")
    # порожня сторінка -> нічого, без крашів
    empty = va.parse_login_page("<html></html>")
    assert empty["token"] is None and empty["challenge"] is None
    print("  parse_login_page: token/challenge/difficulty/rounds/qs ✓")


def test_parse_autosubmit() -> None:
    form = va._parse_autosubmit_form(SAMPLE_AUTOFORM_HTML)
    assert form is not None and form["action"].endswith("/Account/Login")
    assert form["fields"]["wctx"] == "auth=student" and "wresult" in form["fields"]
    # лова головна форма входу НЕ сабмітиться
    login_page_form = va._parse_autosubmit_form(
        SAMPLE_LOGIN_HTML + '<form action="/bytom/Account/Logon?returnUrl=x" method="post">'
        '<input type="hidden" name="__RequestVerificationToken" value="t"/></form>'
    )
    assert login_page_form is None, "форма входу не має авто-сабмітитись"
    print("  parse_autosubmit: wsfed-форма парситься, форма входу ігнорується ✓")


def test_error_message() -> None:
    html_err = (
        '<span class="field-validation-error" data-valmsg-for="Login">'
        "Nieprawidłowy login lub hasło"
        "</span>"
    )
    assert va._login_error_message(html_err) == "Nieprawidłowy login lub hasło"
    assert va._login_error_message("<html>ok</html>") is None
    print("  error_message: витягує текст помилки ✓")


def test_app_url() -> None:
    assert va._APP_PATH_RE.search("/bytom/App/aGVsbG8xMjM0NTY3/tablica")
    assert va._APP_PATH_RE.search("/bytom/App")
    assert not va._APP_PATH_RE.search("/bytom/Account/Login")  # query не рахується
    assert va._at_app("https://dziennik-uczen.vulcan.net.pl/bytom/App")
    assert va._at_app("https://dziennik-uczen.vulcan.net.pl/bytom/App/TXg4/tablica")
    assert not va._at_app("https://dziennik-uczen.vulcan.net.pl/bytom/Account/Login?returnUrl=/bytom/App")
    print("  APP-перевірки ✓")


async def _live_fake() -> None:
    """Ланцюг входу з навмисно ФЕЙКОВИМИ даними — перевіряє, що PoW і
    вся мережева частина працюють (сервер віддає текст-помилку входу,
    а НЕ помилку captcha-токена)."""
    import os
    os.environ.setdefault("VULCAN_LOGIN", "smoketest-999999")
    os.environ.setdefault("VULCAN_PASSWORD", "definitely-wrong-password")
    try:
        await va.login_and_get_cookies()
    except va.AutologinDisabled:
        print("  (--live-fake: credentials не задані, пропускаю)")
        return
    except va.AutologinError as e:
        msg = str(e)
        print(f"  fake вхід відхилено сервером: {msg[:120]}")
        assert "captcha" not in msg.lower(), "PoW не прийнято!"
        print("  --live-fake ✓ (PoW прийнятий, відхилені дані — очікувано)")
        return
    raise AssertionError("фейкові дані не могли залогінитись!")


def main() -> int:
    print("офлайн:")
    test_pow_round_small()
    test_pow_offline()
    test_build_url()
    test_parse_login_page()
    test_parse_autosubmit()
    test_error_message()
    test_app_url()
    if "--live-fake" in sys.argv:
        print("live (fake credentials):")
        asyncio.run(_live_fake())
    print("\nALL TESTS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
