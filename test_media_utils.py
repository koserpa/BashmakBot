"""Офлайн-тести media_utils: reply-контекст.
Запуск: python test_media_utils.py (без мережі — медіа не качається).
"""
from __future__ import annotations

import asyncio
import sys

from media_utils import build_reply_media_context


class StubUser:
    def __init__(self, full_name="", username=""):
        self.full_name = full_name
        self.username = username


class StubMsg:
    """Мінімальний двійник aiogram Message для гілки тексту."""

    def __init__(self, text=None, caption=None, user=None):
        self.text = text
        self.caption = caption
        self.from_user = user
        self.photo = None
        self.sticker = None
        self.animation = None
        self.document = None
        self.voice = None
        self.video_note = None


async def fake_transcribe(data, mime, label):
    return ""


def test_reply_text_included() -> None:
    """Reply на чуже ТЕКСТОВЕ повідомлення: текст із автором потрапляє
    в опис для промпта (бот більше не відповідає «наосліп»)."""
    replied = StubMsg(text="я за мандарини проти, ойті занадто кислі",
                      user=StubUser("Влад", "gqwqeq"))
    parts, desc = asyncio.run(
        build_reply_media_context(None, replied, fake_transcribe))
    assert parts == []          # медіа нема — нічого качати
    assert "у відповідь на повідомлення від Влад" in desc
    assert "мандарини проти" in desc
    print("  reply на текст: текст + автор у промпті ✓")


def test_reply_text_capped_and_anonymous() -> None:
    """Довгий текст обрізається; людина без імені — без 'від кого'."""
    replied = StubMsg(text="х" * 3000)
    parts, desc = asyncio.run(
        build_reply_media_context(None, replied, fake_transcribe))
    assert len(desc) < 1000 and "х" * 100 in desc
    assert "від " not in desc          # автора нема (але "відповідь" — ок)
    print("  reply на текст: кап + без автора ✓")


def test_reply_no_text_no_media() -> None:
    """Стікер/порожнє — не ламається, опис за старими гілками."""
    sticker_msg = StubMsg()
    sticker_msg.sticker = type("S", (), {"is_animated": True, "is_video": False,
                                         "emoji": "🐸"})()
    parts, desc = asyncio.run(
        build_reply_media_context(None, sticker_msg, fake_transcribe))
    assert parts == [] and "стікер" in desc
    empty = StubMsg()
    parts, desc = asyncio.run(
        build_reply_media_context(None, empty, fake_transcribe))
    assert parts == [] and desc == ""
    print("  стікер/порожнє не ламаються ✓")


if __name__ == "__main__":
    test_reply_text_included()
    test_reply_text_capped_and_anonymous()
    test_reply_no_text_no_media()
    print("ALL TESTS OK")
    sys.exit(0)
