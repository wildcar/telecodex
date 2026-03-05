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

        async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
            self.text = text

    async def run() -> str | None:
        bot = DummyBot()
        editor = TelegramStreamEditor(bot=bot, chat_id=1, interval_sec=1.0, tail_chars=50, send_log_threshold=1000)
        editor.message = SimpleNamespace(message_id=10)
        editor.state.tail_text = "Чистый ответ"
        await editor.force_render("done")
        return bot.text

    assert asyncio.run(run()) == "Чистый ответ"
