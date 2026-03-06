from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

TELEGRAM_TEXT_LIMIT = 4096
STATUS_HISTORY_LIMIT = 4


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
        self._title = ""
        self._last_rendered_text = ""
        self._status_lines: list[str] = []
        self._stop_event = asyncio.Event()
        self._refresh_task: asyncio.Task[None] | None = None
        self._edit_lock = asyncio.Lock()
        self._started_at = 0.0

    async def start(self, title: str) -> None:
        self._title = title
        self._started_at = asyncio.get_running_loop().time()
        self._stop_event = asyncio.Event()
        self.message = await self.bot.send_message(self.chat_id, title)
        self._last_rendered_text = title
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def publish_status(self, status: str) -> None:
        normalized = self._normalize_status(status)
        if not normalized:
            return
        async with self._edit_lock:
            if self._status_lines and self._status_lines[-1] == normalized:
                return
            self._status_lines.append(normalized)
            self._status_lines = self._status_lines[-3:]

    @staticmethod
    def tail(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    async def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            await self._render_progress()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                continue

    async def _render_progress(self) -> None:
        if self.message is None:
            return
        async with self._edit_lock:
            text = self._render_status_text()
            if text == self._last_rendered_text:
                return
            await self._safe_edit(text=text, reply_markup=None)

    def _render_status_text(self) -> str:
        elapsed = max(0, int(asyncio.get_running_loop().time() - self._started_at))
        lines = [f"{self._title} ({elapsed}s)"]
        if self._status_lines:
            lines.append("")
            lines.extend(self._status_lines[-STATUS_HISTORY_LIMIT:])
        return self._fit_text("\n".join(lines))

    @staticmethod
    def _normalize_status(status: str) -> str:
        normalized = " ".join(status.split()).strip()
        if not normalized:
            return ""
        if len(normalized) > 200:
            normalized = normalized[:197].rstrip() + "..."
        return normalized

    @staticmethod
    def _fit_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @classmethod
    def _final_text(cls, text: str) -> tuple[str, bool]:
        body = text.strip() or "Ответ пуст."
        if len(body) <= TELEGRAM_TEXT_LIMIT:
            return body, False
        suffix = "\n\n[Полный ответ отправлен файлом]"
        truncated = cls._fit_text(body, TELEGRAM_TEXT_LIMIT - len(suffix)) + suffix
        return truncated, True

    async def _safe_edit(self, text: str, reply_markup: InlineKeyboardMarkup | None) -> None:
        if self.message is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message.message_id,
                text=text,
                reply_markup=reply_markup,
            )
            self._last_rendered_text = text
        except TelegramBadRequest as exc:
            error_text = str(exc)
            if "message is not modified" in error_text:
                self._last_rendered_text = text
                return
            if "message to edit not found" in error_text:
                return
            raise

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
        self._stop_event.set()
        if self._refresh_task is not None:
            await self._refresh_task
            self._refresh_task = None
        body = (final_text or "").strip()
        body, was_truncated = self._final_text(body)
        async with self._edit_lock:
            try:
                await self._safe_edit(text=body, reply_markup=reply_markup)
            except TelegramBadRequest as exc:
                error_text = str(exc)
                if "message is too long" not in error_text and "text is too long" not in error_text:
                    raise
                fallback_text, was_truncated = self._final_text(body)
                await self._safe_edit(text=fallback_text, reply_markup=reply_markup)
        if len(full_text) >= self.send_log_threshold or was_truncated:
            data = (full_text or final_text or "").encode("utf-8", errors="replace")
            doc = BufferedInputFile(data, filename="codex_output.log")
            caption = "Полный лог" if success else f"Полный лог ({summary})"
            await self.bot.send_document(self.chat_id, document=doc, caption=caption)
        return full_text
