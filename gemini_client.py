"""Все, що стосується звернень до Gemini API: генерація тексту,
транскрибування медіа, лічильник запитів, парсинг маркера REACTION."""
import logging
import time
import io
import wave

from config import GEMINI_TTS_MODEL, TTS_VOICE_NAME  # додати до існуючого імпорту з config

from google import genai
from google.genai import errors, types

from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_RPD_LIMIT, SYSTEM_PROMPT
from config import GEMINI_API_KEY, GEMINI_MODEL, GEMINI_RPD_LIMIT, SYSTEM_PROMPT, TTS_RPD_LIMIT

log = logging.getLogger("Bashma4ek_Bot.gemini")

ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Скільки разів повторити запит до Gemini при тимчасових помилках
# (мережа/сервер), перш ніж здатися.
GEMINI_MAX_RETRIES = 2
GEMINI_RETRY_DELAY = 2.0

# Маркер, яким модель може позначити "хочу відповісти реакцією, а не
# текстом".
REACTION_PREFIX = "REACTION:"
VOICE_PREFIX = "VOICE:"

# Емодзі-реакції, дозволені Telegram Bot API для звичайних (не преміум)
# ботів. Список неповний, але покриває базові емоції.
ALLOWED_REACTIONS = {
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱",
    "🤬", "😢", "🎉", "🤩", "🤮", "💩", "🙏", "👌", "🕊", "🤡",
    "🥱", "🥴", "😍", "🐳", "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡",
    "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨",
    "🤝", "✍", "🤗", "🫡", "🎅", "🎄", "☃", "💅", "🤪", "🗿",
    "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎", "👾", "🤷‍♂",
    "🤷", "🤷‍♀", "😡", "🤙",
}


# --- Лічильник запитів до Gemini (для /model, без походу в логи) -----------
class RequestStats:
    def __init__(self):
        self.day = time.strftime("%Y-%m-%d")
        self.count_today = 0
        self.count_total = 0

    def record(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.count_today = 0
        self.count_today += 1
        self.count_total += 1


gemini_stats = RequestStats()
tts_stats = RequestStats()


def tts_quota_low() -> bool:
    """TTS має набагато жорсткіший ліміт, ніж текст (одиниці запитів на
    добу) — перевіряємо окремо, щоб не спамити 429 в логи."""
    return tts_stats.count_today >= TTS_RPD_LIMIT

def quota_low() -> bool:
    """Чи близько до денного ліміту Gemini — якщо так, фонові
    (не обов'язкові) запити варто пропускати, щоб не з'їсти квоту
    на реальні відповіді користувачам."""
    if not GEMINI_RPD_LIMIT:
        return False
    return gemini_stats.count_today >= GEMINI_RPD_LIMIT * 0.9  # залишок < 10%


def parse_reaction_answer(answer: str) -> str | None:
    """Якщо відповідь моделі — це маркер REACTION:<емодзі>, повертає сам
    емодзі (якщо він у дозволеному списку). Інакше None."""
    stripped = answer.strip()
    if not stripped.startswith(REACTION_PREFIX):
        return None
    emoji = stripped[len(REACTION_PREFIX):].strip()
    if emoji in ALLOWED_REACTIONS:
        return emoji
    log.warning(f"Модель попросила недозволену реакцію: {emoji!r}, ігнорую маркер")
    return None

def parse_voice_marker(answer: str) -> tuple[bool, str]:
    """Перевіряє, чи модель попросила озвучити відповідь через маркер
    VOICE: на початку. Повертає (чи_треба_голос, текст_без_маркера)."""
    stripped = answer.strip()
    if stripped.startswith(VOICE_PREFIX):
        return True, stripped[len(VOICE_PREFIX):].strip()
    return False, answer

def _build_system_instruction(current_date_str: str) -> str:
    return (
        SYSTEM_PROMPT
        + f"\n\n[Сьогоднішня дата: {current_date_str}] — врахуй це, якщо "
          "питання стосується часу, віку, дедлайнів, свят чи актуальних подій."
        + " У переписці повідомлення користувачів позначені як "
          "'Ім'я: текст' — звертай увагу, хто саме що написав, "
          "але у своїй відповіді імена дублювати не треба. "
          ""
          "ІНОДІ доречніше не писати текст, а просто поставити "
          "емодзі-реакцію на повідомлення (наприклад, коротке "
          "'ахах', 'лол', '+', 'згоден', жарт що не вартий "
          "розгорнутої відповіді, чи щось шокуюче/смішне). "
          "Якщо вирішив відповісти реакцією — виведи ЛИШЕ рядок "
          f"'{REACTION_PREFIX}<емодзі>' і нічого більше, без "
          "жодного тексту до чи після. Дозволені емодзі: "
          + " ".join(sorted(ALLOWED_REACTIONS))
          + ". Не зловживай цим — переважно все ж пиши звичайну "
            "текстову відповідь, реакція лише коли вона реально "
            "доречніша за слова. "
          ""
          "Якщо у повідомленні через @ тегнуто кількох людей "
          "одразу (і для них є [Про згаданих людей] в промпті) — "
          "можеш відповісти, врахувавши обох/усіх, а не тільки "
          "того, хто писав. "
          ""
          "Коли тобі надсилають фото, стікер, гіфку, голосове чи "
          "файл (зокрема через reply на чиєсь повідомлення з "
          "позначкою '[у відповідь на ...]') — НЕ роби сухий "
          "'звіт' про вміст. Реагуй на це як жива людина в "
          "переписці: постьобатися, здивуватись, оцінити, "
          "прокоментувати по суті — залежно від того, що на "
          "фото/в файлі і в якому режимі тону зараз розмова. "
          "Опис вмісту — лише якщо він реально потрібен для "
          "відповіді, а не сама мета відповіді."
        + "\n\nОКРЕМО про голос: якщо тобі написали голосовим "
          "повідомленням — відповідь ЗАЗВИЧАЙ і так прийде голосом "
          "автоматично (це вирішує код, не ти). Маркер VOICE: "
          "використовуй ЛИШЕ для інших типів повідомлень (текст, фото "
          "тощо), і лише коли голос реально додає цінності: потрібна "
          "інтонація/емоція, жарт що краще заходить голосом, пісня, "
          "передражнювання когось. Це ДУЖЕ обмежений ресурс (кілька "
          "разів на добу на весь чат) — став маркер рідко, як виключення. "
          f"Якщо вирішив озвучити — виведи '{VOICE_PREFIX}' РІВНО на "
          "початку відповіді, а після нього — звичайний текст."
    )


async def ask_gemini(contents: list, current_date_str: str) -> str:
    """Викликає Gemini API. Пошук в інтернеті вже підмішаний у текст промпту
    заздалегідь (детерміновано, у хендлерах) — сюди він приходить готовим.
    При тимчасових (мережа/сервер) помилках робить кілька повторних спроб."""
    last_error: Exception | None = None
    system_instruction = _build_system_instruction(current_date_str)

    for attempt in range(GEMINI_MAX_RETRIES + 1):
        gemini_stats.record()
        try:
            response = await ai_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    max_output_tokens=600,
                    temperature=0.7,
                ),
            )
            return (response.text or "").strip()

        except errors.ClientError as e:
            if e.code == 429:
                log.warning("Запит відхилено: ліміт 429 (RESOURCE_EXHAUSTED)")
                return "Зараз отримую занадто багато запитів 🤯. Зачекай 1-2 хвилини!"
            log.error(f"Помилка Gemini API (ClientError, без повтору): {e}")
            return "Виникла помилка при зверненні до AI 😔"

        except errors.ServerError as e:
            last_error = e
            log.warning(
                f"Тимчасова помилка Gemini API (спроба {attempt + 1}/"
                f"{GEMINI_MAX_RETRIES + 1}): {e}"
            )
        except Exception as e:
            last_error = e
            log.warning(
                f"Несподівана помилка в ask_gemini (спроба {attempt + 1}/"
                f"{GEMINI_MAX_RETRIES + 1}): {e}"
            )

        if attempt < GEMINI_MAX_RETRIES:
            import asyncio
            await asyncio.sleep(GEMINI_RETRY_DELAY)

    log.error(f"ask_gemini: усі спроби вичерпано, остання помилка: {last_error}")
    return "Не вдалося сформулювати відповідь 😔"

async def synthesize_speech(text: str) -> bytes | None:
    if tts_quota_low():
        log.info("TTS-квота на сьогодні вичерпана — пропускаю озвучку")
        return None
    tts_stats.record()

    try:
        response = await ai_client.aio.models.generate_content(
            model=GEMINI_TTS_MODEL,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=TTS_VOICE_NAME
                        )
                    )
                ),
            ),
        )
        pcm = response.candidates[0].content.parts[0].inline_data.data
    except Exception:
        log.exception("Не вдалось згенерувати озвучку відповіді")
        return None

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    return buffer.getvalue()


async def transcribe_media(data: bytes, mime_type: str, kind_label: str) -> str:
    """Спільна логіка транскрибування аудіо/відео (voice / video_note)."""
    contents = [
        {
            "role": "user",
            "parts": [
                {
                    "text": "Транскрибуй мовлення з цього медіа дослівно, "
                    "тією мовою, якою його промовлено. У відповідь дай ТІЛЬКИ "
                    "текст транскрипції, без жодних коментарів чи лапок."
                },
                types.Part.from_bytes(data=data, mime_type=mime_type),
            ],
        }
    ]
    gemini_stats.record()
    try:
        response = await ai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(max_output_tokens=400, temperature=0.2),
        )
        return (response.text or "").strip()
    except Exception:
        log.exception(f"Транскрибування ({kind_label}) не вдалося")
        return ""


async def check_reaction_worthy(sender: str, content_text: str) -> str | None:
    """Дешевий окремий запит: чи варто поставити емодзі-реакцію на
    повідомлення, яке не було адресоване боту напряму. Повертає емодзі
    або None."""
    prompt = (
        f"Повідомлення в чаті від {sender}: \"{content_text}\"\n\n"
        "Чи варто відреагувати на нього емодзі-реакцією (без тексту)? "
        "Це доречно ЛИШЕ для дійсно яскравих випадків: дуже смішне, "
        "шокуюче, влучне, драма, класна новина, бʼючий факап тощо. "
        "Для нейтральних, буденних чи незрозумілих повідомлень — реакція "
        "НЕ потрібна, це має бути рідкісна дія, а не звичка. "
        "Якщо доречно — виведи РІВНО ОДНЕ емодзі з цього списку: "
        + " ".join(sorted(ALLOWED_REACTIONS))
        + ". Якщо ні — виведи рівно слово NONE. Без пояснень і зайвих символів."
    )

    gemini_stats.record()
    try:
        response = await ai_client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=[{"role": "user", "parts": [{"text": prompt}]}],
            config=types.GenerateContentConfig(max_output_tokens=10, temperature=0.4),
        )
        answer = (response.text or "").strip()
    except Exception:
        log.exception("Не вдалось перевірити доречність незапитаної реакції")
        return None

    return answer if answer in ALLOWED_REACTIONS else None
