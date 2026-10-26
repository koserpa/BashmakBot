"""Локальний хелпер: ручний вхід у Dziennik VULCAN -> cookie-сесія в .env.

ЩО РОБИТЬ
---------
1. Відкриває звичайне вікно браузера (Playwright, НЕ headless) на
   сторінці входу https://dziennik-logowanie.vulcan.net.pl/bytom/.
2. Ви САМІ вводите логін і пароль (CAPTCHA проходить у
   браузері автоматично, як і для звичайних користувачів).
3. Після успішного входу (потрапили на портал учня) скрипт забирає
   cookie-сесію порталу учня і записує рядок VULCAN_COOKIES=... у .env
   (чи background друкує куди його вставити, якщо --no-env).
4. Значення cookie НЕ друкується в консоль — тільки назви.

ЯК ЗАПУСТИТИ
------------
    pip install playwright
    python -m playwright install chromium
    python get_vulcan_cookies.py            # записує в .env
    python get_vulcan_cookies.py --no-env   # просто показати назви cookie

ВСТАНОВЛЕННЯ НА КОYEB
---------------------
Скопіруйте значення VULCAN_COOKIES з .env у змінні середовища сервісу
Koyeb (Dashboard -> Settings -> Environment Variables). ЦЕ СЕКРЕТ:
ніколи не комітьте ..env у репозиторій і не надсилайте його нікому.

Коли cookie протухне, бот "/rozklad" повідомить це — тоді повторно
запустіть цей скрипт.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Повний ланцюг входу: після Logon VULCAN передаст особу порталу учня
# (dziennik-uczen) через Fs/Ls і поставить сесію портала, а потім
# перекине на /bytom/App — саме те, що чекає скрипт.
LOGIN_URL = (
    "https://dziennik-logowanie.vulcan.net.pl/bytom/Account/Logon"
    "?returnUrl=http%3a%2f%2fdziennik-logowanie.vulcan.net.pl%2fbytom%2fFs%2fLs"
    "%3fwa%3dwsignin1.0%26wtrealm%3Dhttps%253A%252F%252Fdziennik-uczen."
    "vulcan.net.pl%252Fbytom%252FAccount%252FLogin%253FreturnUrl%253D%252Fbytom"
    "%252FApp%26wctx%3dauth%253Dstudent%2526nslo%253D1"
)
PORTAL_HOST = "dziennik-uczen.vulcan.net.pl"
PROFILE_DIR = str(Path(__file__).parent / "data" / ".vulcan_pw_profile")

INCLUDE_COOKIE_NAMES = re.compile(
    r"^(3S_ARR_EFEB)$", re.IGNORECASE
)
# Ці службові cookie не потрібні для API.
IGNORE_COOKIE_NAMES = re.compile(r"^(contrast|font)$", re.IGNORECASE)
# Справжній портал учня має URL виду /bytom/App/<base64key>/tablica.
APP_URL_RE = re.compile(r"vulcan\.net\.pl/bytom/App/[A-Za-z0-9=_-]{8,}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ручний вхід VULCAN -> VULCAN_COOKIES")
    parser.add_argument("--env", default=".env", help="шлях до .env (за замовчуванням ./.env)")
    parser.add_argument("--no-env", action="store_true", help="не змінювати .env")
    parser.add_argument("--legacy-filter", action="store_true",
                        help="брати тільки 3S_ARR_EFEB (заразом НЕ працює — API потребує "
                             "HttpOnly сесійні cookie; за замовчуванням беремо всі)")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Потрібен playwright:  pip install playwright  "
              "а потім:  python -m playwright install chromium")
        return 1

    with sync_playwright() as p:
        # постійний профіль: наступні запуски хелпера - вже залогінені
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=False,
        )
        page = context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        print("У відкритому вікні браузера залогіньтеся (логін/пароль, CAPTCHA пройде автоматично).")
        print("Скрипт чекає, поки вас перекине на портал учня (до 5 хвилин)...")
        try:
            # чекаємо САМЕ сторінку порталу з ключем учня в URL
            # ("/App/<base64key>/..."), а не проміжні редиректи з/App у query
            page.wait_for_url(APP_URL_RE, timeout=300_000)
            # додаємо час на завершення SPA-роутингу
            page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:
            print("❌ Час очікування вичерпано — вхід не завершився.")
            context.close()
            return 1
        print(f"Потрапили на портал учня: {page.url.split('?')[0][:90]}")

        cookies = [c for c in context.cookies() if PORTAL_HOST in (c.get("domain") or "")]
        if not cookies:
            print("❌ Cookie порталу учня не знайдені — схоже, вхід не завершився.")
            context.close()
            return 1

        if args.legacy_filter:
            selected = [c for c in cookies if INCLUDE_COOKIE_NAMES.match(c.get("name", ""))]
        else:
            # API потребує HttpOnly сесійні cookie (ASP.NET_SessionId,
            # X-V-RequestVerificationToken#...), тож беремо ВСІ, крім косметичних
            selected = [c for c in cookies if not IGNORE_COOKIE_NAMES.match(c.get("name", ""))]
        if not selected:
            print(f"❌ Авторизаційних cookie не знайдено. Наявні імена: "
                  f"{[c['name'] for c in cookies]}")
            context.close()
            return 1

        cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in selected)
        print(f"✅ Отримано cookie: {[c['name'] for c in selected]} "
              f"(значення не друкується).")

        if not args.no_env:
            env_path = Path(args.env)
            line = f"VULCAN_COOKIES={cookie_header}\n"
            if env_path.exists():
                content = env_path.read_text(encoding="utf-8")
                if re.search(r"^VULCAN_COOKIES=.*$", content, flags=re.M):
                    content = re.sub(
                        r"^VULCAN_COOKIES=.*$", line.rstrip("\n"), content,
                        flags=re.M,
                    )
                else:
                    content = content.rstrip("\n") + "\n" + line
                env_path.write_text(content, encoding="utf-8")
                print(f"✅ Записано у {env_path.resolve()} (не комітьте цей файл!)")
            else:
                env_path.write_text(line, encoding="utf-8")
                print(f"✅ Створено {env_path.resolve()} (не комітьте цей файл!)")
            print("Для сервера (Koyeb): скопіюйте VULCAN_COOKIES зі .env у "
                  "Environment Variables сервісу Koyeb.")

        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
