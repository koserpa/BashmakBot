"""Офлайн-тести state_store (Telegram-снапшот стану).

Запуск: python test_state_store.py
Мережа не потрібна: бот замінений фейком з тими самими методами.
"""
from __future__ import annotations

import asyncio
import io
import json

from aiogram.exceptions import TelegramBadRequest

import state_store as ss


class FakeDoc:
    def __init__(self, file_id: str = "file-1", file_name: str | None = "bashmak-state.json"):
        self.file_id = file_id
        self.file_name = file_name


class FakeMessage:
    def __init__(self, message_id: int, file_id: str = "file-1",
                 file_name: str | None = "bashmak-state.json"):
        self.message_id = message_id
        self.document = FakeDoc(file_id, file_name)


class FakePinned:
    def __init__(self, message_id: int, file_id: str = "file-1",
                 file_name: str | None = "bashmak-state.json"):
        self.message_id = message_id
        self.document = FakeDoc(file_id, file_name)


class FakeChat:
    def __init__(self, pinned=None):
        self.pinned_message = pinned


class FakeFile:
    file_path = "state.json"


class FakeBot:
    """Мінімум методів, які використовує state_store."""

    def __init__(self, blob: bytes | None = None, pinned: FakePinned | None = None,
                 fail_get: bool = False, fail_edit: bool = False,
                 edit_error: type[Exception] = TelegramBadRequest):
        self.blob = blob
        self.pinned = pinned
        self.fail_get = fail_get
        self.fail_edit = fail_edit
        self.edit_error = edit_error
        self.sent: list[bytes] = []
        self.edits: list[bytes] = []
        self.pinned_calls: list[int] = []
        self._next_id = 100

    async def get_chat(self, chat_id):
        if self.fail_get:
            raise RuntimeError("network down")
        return FakeChat(self.pinned)

    async def get_file(self, file_id):
        return FakeFile()

    async def download_file(self, path):
        return io.BytesIO(self.blob or b"")

    async def send_document(self, chat_id, document, **kwargs):
        self.sent.append(document.data)
        self._next_id += 1
        msg = FakeMessage(self._next_id)
        return msg

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned_calls.append(message_id)
        self.pinned = FakePinned(message_id)

    async def edit_message_media(self, chat_id, message_id, media, **kwargs):
        if self.fail_edit:
            # aiogram-виняток вимагає method+message
            raise self.edit_error(method=None, message="message can't be edited")
        self.edits.append(media.media.data)
        self.blob = media.media.data


def _section(name: str, box: dict, key: str = "value"):
    """Реєструє секцію навколо dict-а box[key]."""
    def getter():
        return box[key]

    def setter(data):
        box[key] = data

    ss.register(name, getter, setter)


def test_seed_then_edit() -> None:
    ss.reset_for_tests()
    box = {"value": {"lesson_changes": {"k": "sig"}, "chat_ids": [1, -2]}}
    _section("test_vn", box)
    bot = FakeBot()

    async def run():
        await ss.start(bot, 555)
        assert bot.sent, "перший запуск має створити снапшот"
        assert bot.pinned_calls, "снапшот має бути закріплений"
        saved = json.loads(bot.sent[0].decode("utf-8"))
        assert saved["sections"]["test_vn"]["chat_ids"] == [1, -2]
        assert ss.status()["synced"] and ss.status()["pinned"]

        # зміна стану -> edit, без нових повідомлень
        box["value"] = {"lesson_changes": {}, "chat_ids": [1]}
        ss.request_save()
        assert await ss.save_now() is True
        assert len(bot.sent) == 1 and len(bot.edits) == 1
        assert json.loads(bot.edits[0])["sections"]["test_vn"]["chat_ids"] == [1]
        await ss.stop()

    asyncio.run(run())
    print("  seed + edit ✓")


def test_restore_on_start() -> None:
    ss.reset_for_tests()
    payload = {
        "v": 1,
        "saved_at": "2026-10-31T00:00:00+00:00",
        "sections": {"test_vn": {"lesson_changes": {"a": "b"}, "chat_ids": [7]}},
    }
    blob = json.dumps(payload).encode("utf-8")
    bot = FakeBot(blob=blob, pinned=FakePinned(42))
    box = {"value": {}}
    _section("test_vn", box)

    async def run():
        await ss.start(bot, 555)
        assert box["value"]["chat_ids"] == [7], "стан має відновитись"
        assert ss.status()["restored_at"]
        assert not bot.sent, "при відновленні нічого не шлемо"
        await ss.stop()

    asyncio.run(run())
    print("  відновлення на старті ✓")


def test_network_error_does_not_overwrite() -> None:
    ss.reset_for_tests()
    bot = FakeBot(fail_get=True)
    box = {"value": {"x": 1}}
    _section("test_vn", box)

    async def run():
        await ss.start(bot, 555)
        assert not bot.sent, "при помилці читання не можна перезаписувати стан"
        assert ss.status()["synced"] is False
        await ss.stop()
        assert not bot.sent

    asyncio.run(run())
    print("  помилка мережі не перезаписує стан ✓")


def test_broken_json_not_overwritten() -> None:
    ss.reset_for_tests()
    bot = FakeBot(blob=b"not a json", pinned=FakePinned(42))
    _section("test_vn", {"value": {}})

    async def run():
        await ss.start(bot, 555)
        assert ss.status()["synced"] is False
        assert not bot.sent and not bot.edits
        await ss.stop()

    asyncio.run(run())
    print("  битий JSON не перезаписується ✓")


def test_recreate_when_edit_fails() -> None:
    ss.reset_for_tests()
    box = {"value": {"a": 1}}
    _section("test_vn", box)
    bot = FakeBot(blob=b'{"v":1,"sections":{"test_vn":{"a":1}}}',
                  pinned=FakePinned(42), fail_edit=True)

    async def run():
        await ss.start(bot, 555)
        box["value"] = {"a": 2}
        ss.request_save()
        assert await ss.save_now() is True
        assert len(bot.sent) == 1, "має створити новий документ"
        assert bot.pinned_calls, "новий документ теж піниться"
        await ss.stop()

    asyncio.run(run())
    print("  перестворення при невдалому edit ✓")


def test_foreign_pinned_document_creates_own() -> None:
    ss.reset_for_tests()
    box = {"value": {"a": 1}}
    _section("test_vn", box)
    # у чаті закріплено інший документ (не наш снапшот)
    bot = FakeBot(blob=b'{"v":1,"sections":{}}',
                  pinned=FakePinned(42, file_name="other.pdf"))

    async def run():
        await ss.start(bot, 555)
        assert bot.sent, "чужий пін не має блокувати створення снапшоту"
        assert bot.pinned_calls, "свій документ теж піниться"
        await ss.stop()

    asyncio.run(run())
    print("  чужий закріплений документ не блокує ✓")


def test_disabled_without_chat() -> None:
    ss.reset_for_tests()
    bot = FakeBot()

    async def run():
        await ss.start(bot, 0)
        assert ss.status()["configured"] is False
        assert not bot.sent
        await ss.stop()

    asyncio.run(run())
    print("  без STATE_CHAT_ID — no-op ✓")


if __name__ == "__main__":
    test_seed_then_edit()
    test_restore_on_start()
    test_network_error_does_not_overwrite()
    test_broken_json_not_overwritten()
    test_recreate_when_edit_fails()
    test_foreign_pinned_document_creates_own()
    test_disabled_without_chat()
    print("ALL TESTS OK")
