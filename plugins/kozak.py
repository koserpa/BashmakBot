"""Kozak plugin: фейкові козацькі цитати.

Три способи отримати цитату:
- команда /kozak (можно з темою: /kozak сесія), або /kozak у відповідь
  на чиєсь повідомлення — цитата буде саме про нього;
- природний тригер: у повідомленні є слово «козак»/«kozak» (і не команда);
- у реплаї на повідомлення бота — теж згенерує.

Цитати генерує Gemini окремим промптом (перекриває активну персону);
при вичерпаній квоті або збої випадає canned-цитата зі списку. На чат
діє кулдаун, щоб слово «козак» у розмові не сипало цитатами щохвилини."""
import logging
import random
import re
import time

from aiogram import types
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message

import bot_state
from gemini_client import ask_gemini, quota_low, strip_reaction_marker
from search_utils import get_current_date_str

log = logging.getLogger("BashmakBot.kozak")

# Слово-тригер: «козак», «козаки», «kozak»... але не «козакова» у складі
# звичайних слів з довгим хвостом — тому лише ця форма + ще кілька.
_TRIGGER_RE = re.compile(
    r"(?i)\b(козак|козаки|козаче|kozak)\b"
)

COOLDOWN_S = 120
_last_quote: dict[int, float] = {}

# Стиль-промпт цитатника: перекриває персону (prompt_override), тому
# цитати виходять однаково козацькі при будь-якій активній персоне.
QUOTE_SYSTEM_PROMPT = (
    "Ти — майстер фейкових «козацьких цитат». За текстом чату вигадуєш "
    "вигадані козацькі приказки й прикладаєш їх до сучасних побутових "
    "ситуацій. "

    "ФОРМАТИ (обери найкращий під ситуацію, можна чергувати): "
    "1. «Коли замість „X“ сказав: „Y“» — X — те, що людина реально сказала "
    "б у сучасній ситуації, Y — вигадана козацька приказка, яка те саме "
    "означає, старомодно й піднесено. "
    "2. «<короткий опис ситуації з чату>, але не засмутився, бо <вигадана "
    "козацька приказка>». "
    "3. Просто вигадана козацька приказка з натяком на ситуацію з чату. "

    "ПРИКЛАДИ (стиль, не текст — справжніх приказок не цитуй): "
    "- «Коли замість „у мене вертольоти“ сказав: „коли стелиться доріжка "
    "козакові, не до ліжка вона“». "
    "- «Коли сказав „стакан пустує, а козак сумує“ замість „наливай, що "
    "ждеш“». "
    "- «Неправильно відповів на уроці, але не засмутився, бо не той козак, "
    "що за водою пливе, а той, що проти води». "

    "ПРАВИЛА: "
    "- Мова — виключно українська, з легким старомодним колоритом; "
    "канцеляризми й сучасний сленг заборонені (у частині „X“ форматів 1 "
    "можна передати сучасну фразу як є — так і задумано). "
    "- Приказка має звучати як фольклор: конкретні образи (коня, шабля, "
    "порох, Дніпро, Січ, кумполе, шинок, чебрець), ритм, паралелізм. "
    "Вигадуй НОВІ — справжні приказки не цитуй. "
    "- Гумор у розбіжності: дрібна сучасна дрібниця (дедлайн, оцінка, "
    "лаба, доставка їжі) підноситься як козацька чеснота, доля чи лихо. "
    "- МАКСИМУМ 3 речення. Без вступів, без пояснень жарту, без лапок "
    "навколо всієї відповіді. Лише сама цитата."
)

# Коли квота вичерпана чи Gemini мовчить — козацька мудрість все одно
# не може замовкнути. Canned-цитати: гратися в рулетку ризиковано.
FALLBACK_QUOTES = [
    "Не той козак щасливий, що спить до обіду, а той, що снідав двічі.",
    "Козак без дедлайну — що Дніпро без берегів: розливається, та не знає, "
    "куди тікать.",
    "Хто лабу склав з другого разу — той на Січі був з першого.",
    "Коли замість «я не вчив» сказав: «шабля гостра, та чубасом не "
    "наточена».",
    "Козак, що запізнився на пару, на Січ прибуває вчасно.",
    "Неправильно скинув лабу, але не засмутився, бо кінь не питає, чим "
    "кумпола годували.",
    "Коли замість «мені лінь» сказав: «козакова воля — не панська служба».",
    "Сьогодні шинок зачинено, а козак думку тримає — до ранку.",
]


def _under_cooldown(chat_id: int) -> bool:
    return time.time() - _last_quote.get(chat_id, 0) < COOLDOWN_S


def _mark_chat(chat_id: int) -> None:
    _last_quote[chat_id] = time.time()


def _history_context(chat_id: int, max_msgs: int = 10) -> str:
    """Останні повідомлення чату у форматі «Ім'я: текст» (юзерські вже
    так зберігаються; ботські — додаємо префікс самі)."""
    lines: list[str] = []
    for m in bot_state.get_history(chat_id)[-max_msgs:]:
        if not isinstance(m, dict):
            continue
        text = " ".join(
            p.get("text", "") for p in (m.get("parts") or [])
            if isinstance(p, dict)
        ).strip()
        if not text:
            continue
        if m.get("role") == "model":
            lines.append(f"Бот: {text}")
        else:
            lines.append(text)
    return "\n".join(lines)


async def _generate_quote(context: str) -> str:
    """Генерує цитату по контексту; без квоти/при збої — canned-варіант."""
    if quota_low():
        return random.choice(FALLBACK_QUOTES)

    prompt = (
        f"[СИСТЕМНЕ]: придумай ОДНУ фейкову козацьку цитату (формат і "
        f"правила — у системному промпті) за ситуацією з чату. Ситуація:\n"
        f"{context}"
    )
    try:
        answer = await ask_gemini(
            [{"role": "user", "parts": [{"text": prompt}]}],
            get_current_date_str(),
            prompt_override=QUOTE_SYSTEM_PROMPT,
        )
    except Exception:
        log.exception("Не вдалось згенерувати козацьку цитату")
        return random.choice(FALLBACK_QUOTES)

    answer = strip_reaction_marker(answer or "").strip()
    return answer or random.choice(FALLBACK_QUOTES)


def _trigger_context(message: types.Message) -> str | None:
    """Контекст цитати для природного тригера: реплай -> та цитата,
    інакше — останні повідомлення чату."""
    if message.reply_to_message:
        m = message.reply_to_message
        who = m.from_user.full_name if m.from_user else "хтось"
        text = (m.text or m.caption or "").strip()
        if text:
            return f"{who} написав: «{text[:500]}»"
    return _history_context(message.chat.id)


async def cmd_kozak(message: types.Message):
    """/kozak [тема] — цитата на тему, з реплаю чи по контексту чату."""
    args = message.text[len("/kozak"):].strip() if message.text else ""
    if args.startswith("@"):  # на випадок /kozak@BotName
        args = args.split(maxsplit=1)[1] if " " in args else ""

    if args:
        context = f"Людина попросила цитату на тему: «{args[:500]}»."
    elif message.reply_to_message:
        m = message.reply_to_message
        who = m.from_user.full_name if m.from_user else "хтось"
        text = (m.text or m.caption or "").strip()
        context = (
            f"{who} написав: «{text[:500]}»" if text
            else _history_context(message.chat.id)
        )
    else:
        context = _history_context(message.chat.id)

    if not context:
        context = "У чаті поки тихо. Придумай універсальну козацьку приказку."

    quote = await _generate_quote(context)
    _mark_chat(message.chat.id)
    await message.reply(f"⚔️ {quote}")


async def on_trigger(message: types.Message):
    """Природний тригер: у повідомленні слово «козак»/«kozak»."""
    import persona
    if persona.mode() == "kozak":
        # Козак вже в чаті сам — слово «козак» лишаємо звичайній розмові,
        # щоб тригер не з'їдав репліки персони.
        return
    if _under_cooldown(message.chat.id):
        return

    context = _trigger_context(message)
    if not context:
        return

    quote = await _generate_quote(context)
    _mark_chat(message.chat.id)
    if quote:
        await message.reply(f"⚔️ {quote}")


def register_handlers(dp):
    dp.message.register(cmd_kozak, Command("kozak"))
    # Фільтр для природного тригера: перевіряємо message.text через regex
    class KozakTriggerFilter(BaseFilter):
        async def __call__(self, message: Message) -> bool:
            text = message.text or message.caption or ""
            return bool(_TRIGGER_RE.search(text))

    dp.message.register(on_trigger, KozakTriggerFilter())


def register_watchers(bot, tasks):
    pass
