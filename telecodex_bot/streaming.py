from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

class TelegramStreamEditor:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        interval_sec: float,
        tail_chars: int,
        send_log_threshold: int,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.interval_sec = interval_sec
        self.tail_chars = tail_chars
        self.send_log_threshold = send_log_threshold
        self.message: Message | None = None

    async def start(self, title: str) -> None:
        self.message = await self.bot.send_message(self.chat_id, title)

    @staticmethod
    def tail(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    async def finish(
        self,
        success: bool,
        summary: str,
        final_text: str | None = None,
        full_text: str = "",
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> str:
        if self.message is None:
            return full_text
        body = (final_text or "").strip()
        if not body:
            body = "Ответ пуст."
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message.message_id,
                text=body,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            text = str(exc)
            if "message is not modified" not in text and "message to edit not found" not in text:
                raise
        if len(full_text) >= self.send_log_threshold:
            data = full_text.encode("utf-8", errors="replace")
            doc = BufferedInputFile(data, filename="codex_output.log")
            caption = "Полный лог" if success else f"Полный лог ({summary})"
            await self.bot.send_document(self.chat_id, document=doc, caption=caption)
        return full_text
