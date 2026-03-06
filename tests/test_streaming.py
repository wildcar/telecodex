import asyncio
from types import SimpleNamespace

from aiogram.types import InlineKeyboardMarkup

from telecodex_bot.streaming import TelegramStreamEditor


def test_tail_keeps_right_part() -> None:
    assert TelegramStreamEditor.tail("abcdef", 4) == "cdef"
    assert TelegramStreamEditor.tail("abc", 10) == "abc"


def test_render_done_uses_clean_answer_only() -> None:
    class DummyBot:
        def __init__(self) -> None:
            self.text: str | None = None
            self.document_sent = False
            self.reply_markup = None
            self.parse_mode = None

        async def send_message(self, chat_id: int, text: str, parse_mode=None) -> SimpleNamespace:
            self.text = text
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode=None) -> None:
            self.text = text
            self.reply_markup = reply_markup
            self.parse_mode = parse_mode

        async def send_document(self, chat_id: int, document, caption: str) -> None:
            self.document_sent = True

    async def run() -> tuple[str | None, str | None]:
        bot = DummyBot()
        editor = TelegramStreamEditor(bot=bot, chat_id=1, interval_sec=1.0, tail_chars=50, send_log_threshold=1000)
        await editor.start("Running...")
        await editor.finish(
            True,
            "done",
            final_text="Чистый ответ",
            full_text="Чистый ответ",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
        )
        assert bot.reply_markup is not None
        return bot.text, bot.parse_mode

    text, parse_mode = asyncio.run(run())

    assert text == "Чистый ответ"
    assert parse_mode is None


def test_streaming_answer_is_rendered_before_finish() -> None:
    class DummyBot:
        def __init__(self) -> None:
            self.text: str | None = None
            self.edits: list[str] = []

        async def send_message(self, chat_id: int, text: str, parse_mode=None) -> SimpleNamespace:
            self.text = text
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode=None) -> None:
            self.text = text
            self.edits.append(text)

        async def send_document(self, chat_id: int, document, caption: str) -> None:
            return None

    async def run() -> tuple[str | None, list[str]]:
        bot = DummyBot()
        editor = TelegramStreamEditor(bot=bot, chat_id=1, interval_sec=0.01, tail_chars=50, send_log_threshold=1000)
        await editor.start("Telecodex thinking")
        await editor.publish_status("Смотрю FS.md")
        await asyncio.sleep(0.03)
        await editor.publish_answer("Первая часть ответа")
        await asyncio.sleep(0.03)
        await editor.finish(True, "done", final_text="Финальный ответ", full_text="Финальный ответ")
        return bot.text, bot.edits

    final_text, edits = asyncio.run(run())

    assert any("Telecodex working" in item for item in edits)
    assert any("Смотрю FS.md" in item for item in edits)
    assert any("Первая часть ответа" in item for item in edits)
    assert any("Telecodex working" in item and "Первая часть ответа" in item for item in edits)
    assert any("<b>🔵 " in item for item in edits)
    assert not any("Telecodex working" in item and "\n\n" in item for item in edits)
    assert final_text == "Финальный ответ"


def test_finish_renders_code_blocks_as_html_when_short() -> None:
    class DummyBot:
        def __init__(self) -> None:
            self.text: str | None = None
            self.parse_mode: str | None = None

        async def send_message(self, chat_id: int, text: str, parse_mode=None) -> SimpleNamespace:
            self.text = text
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode=None) -> None:
            self.text = text
            self.parse_mode = parse_mode

        async def send_document(self, chat_id: int, document, caption: str) -> None:
            return None

    async def run() -> tuple[str | None, str | None]:
        bot = DummyBot()
        editor = TelegramStreamEditor(bot=bot, chat_id=1, interval_sec=1.0, tail_chars=50, send_log_threshold=1000)
        await editor.start("Running...")
        await editor.finish(True, "done", final_text="Ответ:\n```python\nprint('hi')\n```", full_text="Ответ:\n```python\nprint('hi')\n```")
        return bot.text, bot.parse_mode

    text, parse_mode = asyncio.run(run())

    assert text is not None
    assert "<pre>" in text
    assert parse_mode == "HTML"


def test_finish_truncates_too_long_final_text_and_sends_document() -> None:
    class DummyBot:
        def __init__(self) -> None:
            self.text: str | None = None
            self.document_sent = False

        async def send_message(self, chat_id: int, text: str, parse_mode=None) -> SimpleNamespace:
            self.text = text
            return SimpleNamespace(message_id=10)

        async def edit_message_text(self, *, chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode=None) -> None:
            self.text = text

        async def send_document(self, chat_id: int, document, caption: str) -> None:
            self.document_sent = True

    async def run() -> tuple[str | None, bool]:
        bot = DummyBot()
        editor = TelegramStreamEditor(bot=bot, chat_id=1, interval_sec=1.0, tail_chars=50, send_log_threshold=100000)
        await editor.start("Running...")
        await editor.finish(True, "done", final_text="x" * 5000, full_text="y" * 5000)
        return bot.text, bot.document_sent

    final_text, document_sent = asyncio.run(run())

    assert final_text is not None
    assert len(final_text) <= 4096
    assert final_text.endswith("[Полный ответ отправлен файлом]")
    assert document_sent is True
