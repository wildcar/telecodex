import asyncio
from types import SimpleNamespace

from telecodex_bot.streaming import TelegramStreamEditor


def test_tail_keeps_right_part() -> None:
    assert TelegramStreamEditor.tail("abcdef", 4) == "cdef"
    assert TelegramStreamEditor.tail("abc", 10) == "abc"


def test_render_done_uses_clean_answer_only() -> None:
    class DummyBot:
        def __init__(self) -> None:
            self.text: str | None = None
            self.document_sent = False

        async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
            self.text = text

        async def send_document(self, chat_id: int, document, caption: str) -> None:
            self.document_sent = True

    async def run() -> str | None:
        bot = DummyBot()
        editor = TelegramStreamEditor(bot=bot, chat_id=1, interval_sec=1.0, tail_chars=50, send_log_threshold=1000)
        editor.message = SimpleNamespace(message_id=10)
        await editor.finish(True, "done", final_text="Чистый ответ", full_text="технический лог")
        return bot.text

    assert asyncio.run(run()) == "Чистый ответ"
