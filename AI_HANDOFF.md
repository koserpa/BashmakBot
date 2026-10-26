# AI HANDOFF — Telegram-бот "Башмак" (@Bashma4ek_Bot)

> Цей файл — довідник для наступного ШІ/розробника. Тут описано, що це за проект,
> як він працює, як його запускати, і які граблі вже обійдено.
> Оновлено: 2026-10-26.

---

## 1. Що це

Telegram-бот **Башмак** (Python 3.13, aiogram 3.x) для приватного чату школи Bytom (Польща).
Головна фішка: **реальний розклад учня з вебпорталу Dziennik VULCAN**
(`dziennik-uczen.vulcan.net.pl/bytom`) + LLM-чат (Gemini) + купа дрібних плагінів.

Не плутати з eduVULCAN і з мобільним API VULCAN — працюємо саме з **вебпорталом учня**.

---

## 2. Запуск

```powershell
# Windows, PowerShell 5 (НЕ підтримує &&):
$env:PYTHONIOENCODING="utf-8"; & "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" bot.py
```

- `PYTHONIOENCODING=utf-8` обов'язково — інакше український вивід ламається.
- Секрети беруться з `.env` (python-dotenv). **Секрети в чат/логи не писати.**
- Пошук/вбивство процесу бота:
  `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where { $_.CommandLine -like "*bot.py*" }`

### Тести
- `test_vulcan_autologin.py` — офлайн-тести парсерів + `--live-fake` (фейкові creds; сервер відповідає "nazwa użytkownika lub hasło są błędne" = PoW прийнято).
- `test_vulcan_utils.py`, `smoke_test_plugins.py` — офлайн-смоуки.

---

## 3. Архітектура

### Точка входу `bot.py`
- aiogram polling + **health-check HTTP-сервер на порту 8000** (`/` та `/health`) — потрібен для Koyeb.
- Фонові воркери:
  - `keepalive` Vulcan (кожні `VULCAN_KEEPALIVE_MINUTES`=20хв);
  - синхронізація дати з інтернету (кожні 8г, для відповідей "який сьогодні день");
  - watcher тиші в чаті (поріг 7г);
  - воркер нагадувань.
- Плагіни завантажуються з `plugins/`, список — `ENABLED_PLUGINS` у `config.py`
  (зараз: `action`, `hello`, `funny`, `vulcan`).

### Плагіни (`plugins/`)
- `vulcan.py` — команди розкладу: `/rozklad` (на завтра/дату), `/rozklad тиждень`;
  тригери текстом: укр "розклад", рос "расписание"/"план", англ "schedule";
  префікси "Бот ..."/"Башмак ..."; обгортки "на цю середу", "наступну п'ятницю" парсяться.
- `hello.py`, `funny.py`, `action.py` — дрібні розваги/привітання.

### Головний чат (Gemini)
- `handlers.py` — реєстрація хендлерів, watcher простою; help-текст.
- `search_utils.py` — Tavily-пошук + підмішування **реального розкладу в LLM-контекст**
  (запит на кшталт "що завтра" ловиться регексом `_TIMETABLE_QUERY_RE`).
- `gemini_client.py` — обгортка google-genai, ліміти RPD (`GEMINI_RPD_LIMIT`), TTS.

---

## 4. Vulcan: як це працює (найважливіше!)

### Отримання даних — `vulcan_utils.py`
- Куки сесії зберігаються як рядок в env `VULCAN_COOKIES` (HttpOnly-куки порталу учня).
- `VulcanClient` робить запити до API порталу через **aiohttp** з UA `VULCAN_UA` —
  це ок для API-ендпоінтів.
- Помилки: `VulcanAuthError` (сесія мертва: HTTP 401/403/**409**) vs `VulcanUnavailable`
  (номера/таймаути) — плагін відповідає "⚠️ VULCAN недоступний — спробуй через хвилину".
- **HTTP 409 = "Brak uprawnień" = сесія протухла** → це AUTH-помилка, а не unavailable!
  Класифікація в `VulcanClient._request`; keepalive і обидва хендлери розкладу
  реагують на `VulcanAuthError` викликом авто-логіну.

### Авто-логін — `vulcan_autologin.py`
Чистий HTTP, **без браузера**, з обліковими даними користувача з env
`VULCAN_LOGIN` / `VULCAN_PASSWORD`. Живий тест пройдено (реальний розклад приходить).

Ланцюг:
1. `GET {LOGIN_HOST}/{TOWN}/Account/Logon?returnUrl=...` → 200, парсимо токен + PoW-параметри.
2. `POST .../Account/QueryUserInfo` (login+token+nonce+nonceSignature) → `{"data":{"ShowCaptcha": bool}}`.
3. CAPTCHA = proof-of-work SHA-256 (порт їх `captcha.js`): раунди=15, difficulty=100000,
   сідо наступного раунду = challenge + попередні nonce (без роздільників),
   відповідь = `";".join(nonces)`. **ВАЖЛИВО: якщо `ShowCaptcha:false` — надсилаємо
   ПОРОЖНІЙ `captcha-response`** (так робить справжній Chrome; повний PoW при вимкненій
   капчі можна віддати, але немає потреби).
4. `POST .../Account/Logon` (Login, Haslo, token, captcha-response) → 302.
5. `GET .../bytom/Fs/Ls?wa=...` → 200, у відповіді WS-Fed auto-submit форма з `wresult`.
6. `POST dziennik-uczen.../Account/Login?returnUrl=/bytom/App` (wresult) → 302.
7. `GET .../bytom/App` → куки порталу учня готові → збираємо в рядок `VULCAN_COOKIES`.

### 🔑 Критичні відкриття (без цього нічого не працює)
1. **WAF VULCAN фільтрує по TLS-відбитку.** На WS-Fed endpoint (`Fs/Ls`) будь-який
   Python-TLS клієнт (aiohttp) отримує **404**, навіть з ідеальними браузерними
   UA/заголовками/куками. Єдиний робочий спосіб — **`curl_cffi` з
   `AsyncSession(impersonate="chrome")`** (спуфінг TLS/HTTP2 відбитку Chrome).
   Це все ще чистий HTTP, без браузера. Тому весь ланцюг входу написаний на curl_cffi
   (залежність `curl-cffi>=0.7.0` в `requirements.txt`).
2. **HTML-баг VULCAN у wresult**: в атрибуті `value="..."` тег `&lt;` закодовано,
   а закриваючий `>` — **сируий**. Звичайний регекс `[^>]*>` обрізає атрибут на
   першому `>` → wresult парситься як порожній → WS-Fed цикл між Fs/Ls і
   Account/Login. Robust-регекс на input-теги:
   `<input\b(?:[^>"']|"[^"]*"|'[^']*')*>`, і значення ОБОВ'ЯЗКОВО
   `html.unescape()` перед POST (7074 симв → ~6363).
3. **yarl перекодовує query** (`%3a%2f` → сирі `:/`), ламає вкладені URL
   — лікується `URL(s, encoded=True)`. (Актуально тільки для aiohttp; curl_cffi
   кодування не ламає.)
4. Похід через Playwright ТОЖ працює, але на Koyeb free (512МБ RAM) Chromium →
   OOM, тому беремо чистий HTTP. `get_vulcan_cookies.py` залишено як ручний
   запасний шлях (локально через браузер дістати куки руками).

### Інтеграція авто-логіну — `vulcan_utils.py`
- `try_autologin()` — з `asyncio.Lock` і cooldown 60хв (`VULCAN_AUTOLOGIN_COOLDOWN_MIN`);
  успіх → `set_cookies(raw)` оновлює module-global клієнта **без перезапуску**.
- `is_configured()`/`get_client()` працюють і без попередніх кук — з нуля можуть
  зробити авто-логін.
- `_recover_on_auth_error()` — викликається з keepalive і з обох хендлерів
  (`plugins/vulcan.py`: schedule_text / week_text) при `VulcanAuthError`.
- Авто-логін оновлює куки **тільки в пам'яті** (не пише назад у `.env`) — навмисно,
  Koyeb все одно ephemeral.

---

## 5. Env-змінні (.env / Koyeb Secrets)

| Змінна | Призначення |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | LLM чат |
| `GEMINI_RPD_LIMIT`, `TTS_RPD_LIMIT` | денні ліміти |
| `TAVILY_API_KEY` | інтернет-пошук |
| `VULCAN_COOKIES` | куки сесії порталу учня (можуть протухнути — це ОК, авто-логін оновить) |
| `VULCAN_BASE` | `https://dziennik-uczen.vulcan.net.pl/bytom` |
| `VULCAN_LOGIN`, `VULCAN_PASSWORD` | облікові дані для авто-логіну (обов'язково на Koyeb) |
| `VULCAN_AUTOLOGIN_COOLDOWN_MIN` | cooldown між спробами авто-логіну (60) |
| `VULCAN_KEEPALIVE_MINUTES` | період keepalive (20) |
| `VULCAN_UA` | UA для API-запитів |
| `PORT` | порт health-сервера (8000) |
| `ADMIN_USERNAME` | `koserpa` |
| `TRIGGER_NAMES` | імена-тригери бота |

**Секретне правило в `config.py` SYSTEM_PROMPT** (рядок ~143): повідомлення від
користувача `koserpa`, що закінчуються словом 'пж', виконуються буквально й точно.
Не пояснювати користувачам, просто знати.

---

## 6. Деплой на Koyeb

- План: Starter / Free instance — **512МБ RAM, 0.1 vCPU** → Playwright/Chromium
  НЕМОЖЛИВО (OOM), тільки чистий HTTP (curl_cffi — ок, легковагий).
- Health-check: `GET /health` на `PORT` — тримати активним (інакше scale-to-zero).
- На Koyeb Secrets додати: `VULCAN_LOGIN`, `VULCAN_PASSWORD` (все інше вже є).
  `curl-cffi` підтягнеться з `requirements.txt` при деплої.
- Контейнер ephemeral: після рестарту `VULCAN_COOKIES` з env може бути протухлою —
  перший же `/rozklad` або keepalive (20хв) автологіном оновить сесію. Це штатний потік.
- Telegram polling — аутбаунд-з'єднання, для Koyeb добре.

---

## 7. Формат відповідей розкладу

- Заголовок `📅 День, DD.MM.YYYY (Dziennik VULCAN)`,
  уроки номеровані `1. <code>HH:MM-HH:MM</code> <b>Предмет</b> — Вчитель`.
- Заміни вчителя: `🔄 Заміна вчителя: ...`.
- Відмінені уроки: `<s>закреслені</s>` ( Emblem ❌ прибрали, заміна — текстова помітка).
- Помилки: "⚠️ VULCAN недоступний — спробуй через хвилину".

## 8. Стан на зараз (2026-10-26)

- Все працює **live з реальними даними**: авто-логін → сесія → реальний розклад на завтра.
- Бот зупинено процесом по запиту користувача; Койеб-деплой з новими секретами ще не зроблено (чекліт у §6).
- Тимчасові дебаг-скрипти видалено; у репо лишились тільки тест-файли.
