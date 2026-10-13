"""Action plugin: bot fills/reminds via /fill or natural phrase."""
from aiogram import types
from aiogram.filters import Command
from aiogram.types import Message
from reminder_utils import add_reminder

async def cmd_fill(message: Message):
    text = message.text[len("/fill"):].strip() if message.text else ""
    if not text:
        await message.reply("Напиши що нагадати, наприклад: /fill виключи газ через 15 хв")
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    mention = f"@{message.from_user.username}" if message.from_user.username else ""
    res = add_reminder(chat_id, user_id, mention, text)
    await message.reply(f"⏰ Нагадую: {text}\n{res}")

async def cmd_fill_natural(message: Message):
    txt = message.text or ""
    low = txt.lower()
    if ("напovни" in low or ("бот" in low and ("вик" in low or "через" in low or "в " in low))):
        # extract after keyword
        for kw in ("напovни", "бот"):
            if kw in low:
                rest = txt.split(kw, 1)[1].strip()
                if rest:
                    chat_id = message.chat.id; user_id = message.from_user.id
                    mention = f"@{message.from_user.username}" if message.from_user.username else ""
                    res = add_reminder(chat_id, user_id, mention, rest)
                    await message.reply(f"⏰ Нагадую: {rest}\n{res}")
                    return

from aiogram.filters import Command as C

def register_handlers(dp):
    dp.message.register(cmd_fill, C("fill"))
    dp.message.register(cmd_fill_natural, lambda msg: isinstance(msg, Message) and ("напovни" in (msg.text or "").lower() or ("бот" in (msg.text or "").lower() and ("вик" in (msg.text or "").lower() or "через" in (msg.text or "").lower() or "в " in (msg.text or "").lower()))))

def register_watchers(bot, tasks):
    pass
