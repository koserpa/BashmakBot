"""Action plugin: команда /fill ставить нагадування (синонім /remind).

Раніше тут був ще "природний" тригер за словами «бот»/«в »/«через» —
він містив латинські літери в слові «наповни» і перехоплював майже будь-яке
повідомлення з цими словами, тому його прибрано."""
from aiogram.filters import Command
from aiogram.types import Message

from reminder_utils import add_reminder


async def cmd_fill(message: Message):
    text = message.text[len("/fill"):].strip() if message.text else ""
    # на випадок /fill@BotName
    if text.startswith("@"):
        text = text.split(maxsplit=1)[1] if " " in text else ""
    if not text:
        await message.reply("Напиши що нагадати, наприклад: /fill 15m виключи газ")
        return

    user = message.from_user
    user_id = user.id if user else 0
    mention = f"@{user.username}" if user and user.username else (user.full_name if user else "Хтось")

    res = add_reminder(message.chat.id, user_id, mention, text)
    await message.reply(res)


def register_handlers(dp):
    dp.message.register(cmd_fill, Command("fill"))


def register_watchers(bot, tasks):
    pass
