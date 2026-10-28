# AI HANDOFF — Telegram-бот "Башмак" (@Bashma4ek_Bot)

> Цей файл — довідник для наступного ШІ/розробника. Тут описано, що це за проект,
> як він працює, як його запускати, і які граблі вже обійдено.
> Оновлено: 2026-10-27 (вечір).

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
- Фонові воркери — УСІ під супервізором `tasks.py` (`spawn_watch(name, factory)`):
  впав цикл → лог з traceback → авто-перезапуск з бектрафом (10с→20с→…→кап 5хв);
  >5 падінь/добу → супервізор сам зупиняється. Живі watcher-х видно в
  **`/status`** (адмін): uptime, Gemini RPD, TTS RPD, чатів у памʼяті, циклі:
  `date_sync`, `idle_chat`, `reminders`, `vulcan_notify` (або `vulcan_keepalive`).
- Плагіни завантажуються з `plugins/`, список — `ENABLED_PLUGINS` у `config.py`
  (зараз: `action`, `hello`, `funny`, `vulcan`).

### Плагіни (`plugins/`)
- `vulcan.py` — команди розкладу: `/rozklad` (на завтра/дату), `/rozklad тиждень`;
  тригери текстом: укр "розклад", рос "расписание"/"план", англ "schedule";
  префікси "Бот ..."/"Башмак ..."; обгортки "на цю середу", "наступну п'ятницю" парсяться.
  **Кнопки**: під карткою дня/тижня inline-кнопки (завтра/тиждень/sprawdziany),
  callback-хендлер `on_vulcan_callback` ловить `vulcan:*`.
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

### Проактивні сповіщення — `vulcan_notify.py` (жовтень 2026)
Одна фаза (`notify_watcher`, стартує з `plugins/vulcan.py.register_watchers`) —
і сам цикл ТРИМАЄ СЕСІЮ ЖИВОЮ (кожна ітерація = запит до API), тому окремий
keepalive_watcher не стартує, коли notify увімкнено (`VULCAN_NOTIFY=false`
повертає класичний keepalive):
1. **Зміни розкладу**: кожні `VULCAN_CHANGES_POLL_MIN` хв (default 15) тягнемо
   `PlanZajec` на 7 днів **force=True**, diff підписів (`lesson_signature`:
   відміна+причина, заміна вчителя/кабінету, перенесення, зміна групи, примітки)
   проти снапшоту `data/vulcan_notify.json`. Коди `zmiany` (живий API):
   1=відмінено, 4=вихід класу/групи (НЕ відміна!), 5/6=перенесено з/на,
   7=заміна вчителя (іноді + кабінет), 8=заміна кабінету, 15=заняття з іншою
   групою; невідомі коди теж показуємо, а не губимо.
   Перший запуск (на Koyeb `data/` ephemeral → файлу немає) зберігає baseline,
   але **поточні зміни на сьогодні й далі надсилає** (раніше тихо ковтав —
   заміни, зроблені до старту бота, не приходили ніколи). Якщо картку не
   вдалося доставити — підписи не фіксуються, буде повтор наступного циклу.
   **Нічна пауза 00:00–06:00** (Варшава) — цикли пропускаються.
2. **Ранковий дайджест** (`VULCAN_MORNING_HOUR:MINUTE`, default 6:50):
   розклад на сьогодні + події сьогодні, раз на день (`morning_done_day`).
3. **За день до події**: після `VULCAN_REMIND_HOUR` (default 18) тягнемо
   `/api/SprawdzianyZadaniaDomowe` на завтра. **Словарь typ — стандартный VULCAN**
   (порядок у дропдауні «Rodzaj»: 1=Sprawdzian, 2=Kartkówka, 3=Praca klasowa,
   4=Zadanie domowe). Верифікація 27.10.2026: typ=1 та typ=2 підтверджено живо
   (typ=2 → Chemia 12.11 в UI = «Kartkówка», НЕ «Zadanie domowe» — попередня
   мапа 2=дз була НЕВІРНА). Невідомий typ → генерик «Подія (typ N)» (не падати).
   Ids сповіщених зберігаються з ДАТОЮ сповіщення
   та чистяться при _save() (TTL 35 днів, `NOTIFIED_TTL_DAYS`) —
   рестарти не спам знову, state не розростається.
4. **`/sprawdziany`** (публічний API `upcoming_events_text(days=30)`) —
   всі події на 30 днів, групування по датах. Текстові тригери БЕЗ слеша
   (регес `plugins/vulcan.py`: «башмак, розклад на завтра», «шо по дз»,
   «що по домашці») **спрацьовують лише зі зверненням до бота** —
   @username, ім'я-тригер на початку/в кінці або reply на бота
   (фільтр `AddressedToBot`). Голий «розклад» у розмові бота не смикає.
5. **`/vulcan_silence`** (alias `/vulcan_mute`) — toggle-тишина для чату
   (`muted_chats`); mutнути чати НЕ реєструються в цілі повторно.
6. Цілі-чати: env `VULCAN_NOTIFY_CHATS` (через кому) АБО чати, де використовували
   розклад/sprawdziany (авто-запам'ятовування). **Мертві чати**: 10 послідовних
   невдалих send'ів (`fail_counts`) → авто-виключення зі списку цілей.
   ⚠️ `data/` на Koyeb ефемерний: `chat_ids` із файлу зникають при кожному
   редеплої. Тому груповий чат треба прописати у `VULCAN_NOTIFY_CHATS`
   (env переживає редеплой; `_target_chats()` мержить env + файл).

### Живі API-ендпоінти вебпорталу (програв маршрут Chrome, 27.10.2026)
```
GET /api/Context                                   -> учень/ключ
GET /api/PlanZajec?key&dataOd&dataDo&zakresDanych=2 -> розклад (item.zmiany = заміни)
GET /api/DniWolne?key&dataOd&dataDo                -> свята
GET /api/SprawdzianyZadaniaDomowe?key&dataOd&dataDo -> sprawdziany + дз!
     (item: {typ: 1..4, przedmiotNazwa, data, hasAttachment, id};
      typ→назва див. словарь вище)
GET /api/*Tablica (SprawdzianyTablica, ZadaniaDomoweTablica, WazneDzisiaj...) -> тільки "сьогодні"
```
Всі ендпоінти GET+query, утилізуються з кукі сесії; невідомий шлях сервер віддає
**200 text/html** (SPA fallback) — при розвідці ловився як VulcanAuthError.
`SprawdzianyZadaniaDomowe` товаришується зі `key` учня; діапазон дат — UTC ISO
-кила `_iso_range_start/_iso_range_end`. Деталі (opis/zakres) НЕ в списку —
тільки предмет+дата+typ; якщо колись потрібен opis, шукати нове в JS-профілі.
Дам же, щоб дивитись події на UI: вкладка «Sprawdziany i zadania domowe».

---

## 5. Env-змінні (.env / Koyeb Secrets)

| Змінна | Призначення |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | LLM чат |
| `GEMINI_RPD_LIMIT`, `TTS_RPD_LIMIT` | денні ліміти |
| `GEMINI_MAX_OUTPUT` | ліміт output-токенів відповіді (1024); у thinking-моделях роздуми теж їдять ліміт — при обрізаних відповідях підняти на 2048+ |
| `GEMINI_FALLBACK_MODEL` | запасна модель на останній retry |
| `TAVILY_API_KEY` | інтернет-пошук |
| `VULCAN_COOKIES` | куки сесії порталу учня (можуть протухнути — це ОК, авто-логін оновить) |
| `VULCAN_BASE` | `https://dziennik-uczen.vulcan.net.pl/bytom` |
| `VULCAN_LOGIN`, `VULCAN_PASSWORD` | облікові дані для авто-логіну (обов'язково на Koyeb) |
| `VULCAN_AUTOLOGIN_COOLDOWN_MIN` | cooldown між спробами авто-логіну (60) |
| `VULCAN_KEEPALIVE_MINUTES` | період keepalive (20) |
| `VULCAN_UA` | UA для API-запитів |
| `VULCAN_NOTIFY_CHATS` | chat_id (через кому) для проактивних сповіщень; порожньо = чати, де юзали /rozklad |
| `VULCAN_CHANGES_POLL_MIN` | період перевірки замін у розкладі (15); ніч 00:00–06:00 — пауза |
| `VULCAN_REMIND_HOUR` | година (Варшава) нагадування про завтрашні sprawdzian/дз (18) |
| `VULCAN_MORNING_HOUR[_MINUTE]` | ранковий дайджест «розклад на сьогодні» (6:50) |
| `VULCAN_NOTIFY` | `false` — вимкнути всі проактивні сповіщення (штатний keepalive повертається) |
| `PORT` | порт health-сервера (8000) |
| `ADMIN_USERNAME` | `koserpa` |
| `TRIGGER_NAMES` | імена-тригери бота (⚠️ обов'язково на Koyeb Secrets — без нього name-тригери мовчать) |

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

## 8. Стан на зараз

- **CI GREEN (27.10.2026, run 9a093cf).** Перший прогін ebba2dd падав на
  `test_text_utils.py::test_strip_trigger`: тести залежали від локального `.env`
  (`TRIGGER_NAMES` читається в config.py при імпорті; на CI .env немає).
  Фікс: `test_text_utils.py` сам задає `os.environ["TRIGGER_NAMES"]` ДО імпорту
  text_utils/config. Логи CI анонімно не читаються (API 403), але юзер може
  скачати zip логів з GitHub UI — так діагноз і знайшли.
- НОВЕ (27.10.2026): проактивні сповіщення `vulcan_notify.py` — заміни/відміни
  (diff розкладу кожні 15хв, ніч 00:00–06:00 пауза) + нагадування за день до
  sprawdzian/дз (18:00) + ранковий дайджест (6:50) + `/sprawdziany` (текстові
  тригери «шо по дз» тощо) + `/vulcan_silence`. Цикл об'єднано з keepalive
  (менше запитів до порталу). Live-тест пройдено. state-чистка `notified_events`
  (дата замість true, TTL 35 днів у `_save`); inline-кнопки під розкладом
  день/тиждень (`vulcan:*`) — працюють (скріншоти юзера).
- **Фікс «бот не шле заміни» (28.10.2026):** причина була подвійна.
  1) Парсер знав лише `zmiana=1` (відміна) і `7` (вчитель), а живі зміни
  приходять ще кодами 4 (вихід у кіно), 5/6 (перенесення), 8 (кабінет),
  15 (заняття з іншою групою); гірше — будь-який непорожній
  `informacjeNieobecnosc` вважався відміною, тож «Wyjście do kina» показувалось
  як скасовані уроки. 2) Перший запуск тихо зберігав baseline, тому зміни,
  зроблені до старту (а на Koyeb state ephemeral), не надсилались ніколи.
  Тепер коди розібрані, seed показує поточні зміни (сьогодні+), а невдала
  доставка не фіксує підпис (повтор наступного циклу).
- **Друга хвиля фіксів доставки (28.10.2026):**
  1) нагадування «завтра» більше не позначається надісланим, якщо `_notify`
     не доставив (раніше губилось назавжди на свіжому деплої без цілей);
  2) `morning_done_day`/`remind_done_day` ставляться лише при успіху — одна
     помилка API о 6:50 більше не вбиває дайджест на всю добу; ранковий
     дайджест не шлеться після 12:00 (рестарт о 22:00 не пише «Доброго ранку»);
  3) `html.escape` на всіх даних з порталу (предмет/вчитель/кабінет/примітка/
     зміни): один `&` або `<` у назві раніше валив HTML-парсинг Telegram і
     картка не доходила взагалі;
  4) підпис уроку сортує список змін — інший порядок `zmiany` від порталу
     більше не виглядає як нова заміна (не було б спаму кожні 15 хв).
- **Формат змін + групи (28.10.2026, вечір):**
  - зміни показуються як **«нове значення»**, без повтору того, що вже стоїть
    у рядку уроку: `🔄 Заміна вчителя → Wakat5`, `🔄 Заміна кабінету → 101`,
    `🔀 Заміна на: Chemia (4TIR)`, `🕒 Перенесено: 13:15 → 15:00`
    (коди 5+6 зливаються в один рядок). Старе значення свідомо не дублюється —
    його видно у самому рядку уроку.
  - рядок уроку в картці: `• 12:25 <b>Предмет</b> (Вчитель, каб. N) — зміна`;
    вчитель і кабінет додані, щоб картка була самодостатньою. Якщо змін
    кілька — вони в ОДНОМУ рядку через `;`, назва уроку не повторюється.
  - заголовок картки: «Зміни в розкладі» (було «Сталося в розкладі»).
  - **фікс «картки тільки в приват»**: `remember_chat` відкидав `chat_id <= 0`,
    а в груп це id від'ємний → група ніколи не реєструвалась як ціль.
    Тепер приймаються всі, крім 0. Після фіксу групі достатньо один раз
    викликати `/rozklad` (або текстовий тригер), щоб потрапити в розсилку.
- **Стабільний підпис змін + живий `/health` (28.10.2026, ніч):**
  - `lesson_signature` рахується зі СИРИХ полів `raw_changes`/`raw_notes`
    (`vulcan_utils.lesson_from_raw`), а не з готового тексту картки. Раніше
    правка формулювання («Заміна:» → «Заміна на:») змінювала підпис, і після
    деплою бот слав дубль усіх карток. Тепер формулювання на diff не впливає.
  - міграція state: `_legacy_signature` (старий текстовий формат) вважається
    «незміненим» протягом одного циклу, далі перезаписується новим підписом —
    деплой не спамить.
  - `/health` тепер JSON: `status` (ok/degraded), `uptime_sec`, `vulcan`
    (`last_poll_at`, `last_poll_ok`, `last_poll_error`, `targets`, `baseline`,
    `tracked_lessons`, `muted_chats`). HTTP-код завжди 200: 503 змусив би Koyeb
    перезапускати бота, а перезапуск не лікує мертву сесію VULCAN і губить
    ephemeral state (дублі карток).
  - CI: `py_compile` по `git ls-files '*.py'` + прогін усіх `test_*.py`
    (раніше і модулі, і тести перелічувались руками — новий файл забувався).
  - `.dockerignore`: `.env`/`data`/логи не потрапляють у шар образу;
    `.gitignore` тепер ігнорує `*.log` і `_debug_help.py`.
- **/help фікс (27.10.2026):** TelegramBadRequest «Unsupported start tag "час"» —
  бот шле HTML, а в тексті /help стояло `/remind <час> <текст>`; Telegram їв
  `<час>` за тег і відмовлявся показувати повідомлення. Замінено на літеральні
  приклади («/remind 15m текст»). Правило: у HTML-меседжах не писати
  кутові `<слово>` без тега/без `<code>`, і після редагувань хелпу перевіряти
  ії на живому ботові.
- **Тест-гігієна:** `test_vulcan_notify.py` ізолює state (`vn.STATE_FILE` →
  tempdir; раніше тест маратив реальний `data/vulcan_notify.json`, а в state
  lez фейковий чат 999 → «chat not found(1/10)» у логах).
- Пуші 27.10.2026: `f79e27a` (typ-мапа + /help), `9a093cf` (детермінованість
  тестів) — поверх ebba2dd; origin/main == local main.
- ⚠️ **На Koyeb обов'язково є `TRIGGER_NAMES` у Secrets** — без нього
  name-тригери мовчать, працює тільки @mention. Локально лежить у `.env`.
- **Мапа typ ВИПРАВЛЕНА (27.10.2026, вечір):** стандартний VULCAN —
  1=Sprawdzian, 2=Kartkówka, 3=Praca klasowa, 4=Zadanie domowe. typ=2→Kartkówка
  підтверджено скріншотом юзера (Chemia 12.11 в UI = «Kartkówка», попередня
  мапа «2=дз» була хибною). typ=3 цього року ще не зустрічався (0 записів),
  typ=4 — Informatyka 05.10/Chemia 15.10 (минулий час; звірити в UI колись).
  SPA-бандл обфускований — статично назви з нього не сидять; i18n містить
  порядок «Sprawdzian, Kartkówka, Praca klasowa, Zadanie domowe» = 1..4.
- Супервізор фонових циклів `tasks.py`
  (авто-перезапуск впалых watcher-ів, реєстр `WATCHERS` → видно в `/status`);
  `GEMINI_MAX_OUTPUT` (env, default 1024 — thinking-токени теж лімітовані);
  безпечний parse retry-after (429, кап 90с); /status розширено (квоти, чати,
  живі циклі); reminder-parser приймає кириличні «30м/45с/2 днів» (був тьхий
  bug: «днів» давав 0); оффлайн-тести `test_text_utils.py` (у CI теж).
