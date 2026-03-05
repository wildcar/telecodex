from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, Message


@dataclass(slots=True)
class StreamState:
    full_output: list[str] = field(default_factory=list)
    tail_text: str = ""
    last_rendered: str = ""
    started_at_monotonic: float = field(default_factory=time.monotonic)


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
        self.state = StreamState()
        self.message: Message | None = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def start(self, title: str) -> None:
        self.message = await self.bot.send_message(self.chat_id, f"Running...\n{title}")
        self._last_edit = time.monotonic()

    @staticmethod
    def tail(text: str, max_chars: int) -> str:
        if len(text) <= max_chars:
            return text
        return text[-max_chars:]

    async def push(self, chunk: str, status: str = "running") -> None:
        async with self._lock:
            self.state.full_output.append(chunk)
            combined = "".join(self.state.full_output)
            self.state.tail_text = self.tail(combined, self.tail_chars)
            now = time.monotonic()
            if now - self._last_edit < self.interval_sec:
                return
            await self._render(status)

    async def force_render(self, status: str) -> None:
        async with self._lock:
            await self._render(status)

    async def _render(self, status: str) -> None:
        if self.message is None:
            return
        elapsed = int(time.monotonic() - self.state.started_at_monotonic)
        body = self.state.tail_text.strip() or "(no output yet)"
        text = f"[{status}] {elapsed}s\n\n{body}"
        if text == self.state.last_rendered:
            return
        self._last_edit = time.monotonic()
        self.state.last_rendered = text
        try:
            await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message.message_id, text=text)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                raise

    async def finish(self, success: bool, summary: str) -> str:
        status = "done" if success else "failed"
        await self.force_render(status)
        final_mark = "✅" if success else "❌"
        await self.bot.send_message(self.chat_id, f"{final_mark} {summary}")
        full_text = "".join(self.state.full_output)
        if len(full_text) >= self.send_log_threshold:
            data = full_text.encode("utf-8", errors="replace")
            doc = BufferedInputFile(data, filename="codex_output.log")
            await self.bot.send_document(self.chat_id, document=doc, caption="Полный лог")
        return full_text
