from __future__ import annotations

import asyncio
import html
import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

TELEGRAM_TEXT_LIMIT = 4096
STATUS_HISTORY_LIMIT = 4
CODE_BLOCK_RE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)


@dataclass(slots=True)
class RenderedTelegramText:
    text: str
    parse_mode: str | None = None
    fallback_text: str | None = None


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
        self._last_parse_mode: str | None = None
        self._status_lines: list[str] = []
        self._answer_text = ""
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
        self._last_parse_mode = None
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def publish_status(self, status: str) -> None:
        normalized = self._normalize_status(status)
        if not normalized:
            return
        async with self._edit_lock:
            if self._status_lines and self._status_lines[-1] == normalized:
                return
            self._status_lines.append(normalized)
            self._status_lines = self._status_lines[-STATUS_HISTORY_LIMIT:]

    async def publish_answer(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        async with self._edit_lock:
            if normalized == self._answer_text:
                return
            self._answer_text = normalized

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
            rendered = self._render_current_text()
            if rendered.text == self._last_rendered_text and rendered.parse_mode == self._last_parse_mode:
                return
            await self._safe_edit(rendered=rendered, reply_markup=None)

    def _render_current_text(self) -> RenderedTelegramText:
        elapsed = max(0, int(asyncio.get_running_loop().time() - self._started_at))
        if self._answer_text:
            return self._render_working_text(elapsed, answer_text=self._answer_text)
        title = "Telecodex working..." if self._status_lines else self._title
        lines = [f"{title} ({elapsed}s)"]
        if self._status_lines:
            lines.append("")
            lines.extend(self._status_lines[-STATUS_HISTORY_LIMIT:])
        return RenderedTelegramText(self._fit_text("\n".join(lines)))

    def _render_working_text(self, elapsed: int, answer_text: str) -> RenderedTelegramText:
        lines = [f"Telecodex working... ({elapsed}s)"]
        if self._status_lines:
            lines.append("")
            lines.extend(self._status_lines[-STATUS_HISTORY_LIMIT:])
        lines.append("")
        lines.append(self._stream_preview_text(answer_text))
        return RenderedTelegramText(self._fit_text("\n".join(lines)))

    def _stream_preview_text(self, text: str) -> str:
        body = text.strip() or "Ответ пуст."
        if len(body) <= TELEGRAM_TEXT_LIMIT:
            return body
        suffix = "\n\n[Показан хвост потокового ответа]"
        budget = max(0, TELEGRAM_TEXT_LIMIT - len(suffix))
        preview = self.tail(body, min(self.tail_chars, budget))
        if len(preview) > budget:
            preview = preview[-budget:]
        return preview + suffix

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
    def _render_final_text(cls, text: str) -> tuple[RenderedTelegramText, bool]:
        body = text.strip() or "Ответ пуст."
        if len(body) <= TELEGRAM_TEXT_LIMIT:
            html_rendered = cls._render_code_blocks_html(body)
            if html_rendered is not None and len(html_rendered.text) <= TELEGRAM_TEXT_LIMIT:
                return html_rendered, False
            return RenderedTelegramText(body), False
        suffix = "\n\n[Полный ответ отправлен файлом]"
        preview_limit = TELEGRAM_TEXT_LIMIT - len(suffix)
        truncated = body[:preview_limit].rstrip() + suffix
        return RenderedTelegramText(truncated), True

    @classmethod
    def _render_code_blocks_html(cls, text: str) -> RenderedTelegramText | None:
        if "```" not in text or text.count("```") % 2 != 0:
            return None
        parts: list[str] = []
        last = 0
        for match in CODE_BLOCK_RE.finditer(text):
            plain = text[last:match.start()]
            if plain:
                parts.append(html.escape(plain))
            code = match.group(1).rstrip("\n")
            parts.append(f"<pre>{html.escape(code)}</pre>")
            last = match.end()
        tail = text[last:]
        if tail:
            parts.append(html.escape(tail))
        if not parts:
            return None
        return RenderedTelegramText("".join(parts), parse_mode="HTML", fallback_text=text)

    async def _safe_edit(self, rendered: RenderedTelegramText, reply_markup: InlineKeyboardMarkup | None) -> None:
        if self.message is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message.message_id,
                text=rendered.text,
                reply_markup=reply_markup,
                parse_mode=rendered.parse_mode,
            )
            self._last_rendered_text = rendered.text
            self._last_parse_mode = rendered.parse_mode
        except TelegramBadRequest as exc:
            error_text = str(exc)
            if "message is not modified" in error_text:
                self._last_rendered_text = rendered.text
                self._last_parse_mode = rendered.parse_mode
                return
            if "message to edit not found" in error_text:
                return
            if rendered.parse_mode and "can't parse entities" in error_text and rendered.fallback_text is not None:
                fallback = RenderedTelegramText(rendered.fallback_text)
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message.message_id,
                    text=fallback.text,
                    reply_markup=reply_markup,
                )
                self._last_rendered_text = fallback.text
                self._last_parse_mode = None
                return
            raise

    async def finish(
        self,
        success: bool,
        summary: str,
        final_text: str | None = None,
        full_text: str = "",
        reply_markup: InlineKeyboardMarkup | None = None,
        attachment_text: str | None = None,
    ) -> str:
        if self.message is None:
            return full_text
        self._stop_event.set()
        if self._refresh_task is not None:
            await self._refresh_task
            self._refresh_task = None
        body = (final_text or self._answer_text or "").strip()
        rendered, was_truncated = self._render_final_text(body)
        async with self._edit_lock:
            try:
                await self._safe_edit(rendered=rendered, reply_markup=reply_markup)
            except TelegramBadRequest as exc:
                error_text = str(exc)
                if "message is too long" not in error_text and "text is too long" not in error_text:
                    raise
                fallback, was_truncated = self._render_final_text(body[: TELEGRAM_TEXT_LIMIT - 32])
                await self._safe_edit(rendered=fallback, reply_markup=reply_markup)
        document_text = attachment_text or full_text or body
        if len(document_text) >= self.send_log_threshold or was_truncated:
            data = document_text.encode("utf-8", errors="replace")
            doc = BufferedInputFile(data, filename="codex_output.md")
            caption = "Полный ответ" if success else f"Подробности ({summary})"
            await self.bot.send_document(self.chat_id, document=doc, caption=caption)
        return body
