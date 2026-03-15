from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, Message

TELEGRAM_TEXT_LIMIT = 4096
STATUS_HISTORY_LIMIT = 4
CODE_BLOCK_RE = re.compile(r"```[^\n`]*\n?(.*?)```", re.DOTALL)
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
DEFAULT_TELEGRAM_API_TIMEOUT_SEC = 15.0
DEFAULT_STALLED_DELIVERY_SEC = 30.0
DEFAULT_FINALIZE_WAIT_SEC = 5.0

logger = logging.getLogger(__name__)


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
        *,
        api_timeout_sec: float = DEFAULT_TELEGRAM_API_TIMEOUT_SEC,
        stalled_delivery_sec: float = DEFAULT_STALLED_DELIVERY_SEC,
        finalize_wait_sec: float = DEFAULT_FINALIZE_WAIT_SEC,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.interval_sec = interval_sec
        self.tail_chars = tail_chars
        self.send_log_threshold = send_log_threshold
        self.api_timeout_sec = api_timeout_sec
        self.stalled_delivery_sec = stalled_delivery_sec
        self.finalize_wait_sec = finalize_wait_sec
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
        self._spinner_index = 0
        self._last_delivery_at = 0.0

    async def start(self, title: str) -> None:
        self._title = title
        self._started_at = asyncio.get_running_loop().time()
        self._stop_event = asyncio.Event()
        self._spinner_index = 0
        rendered = self._render_current_text()
        await self._safe_send(rendered=rendered, reply_markup=None)
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
            try:
                await self._render_progress()
            except Exception:
                logger.warning("Telegram stream refresh failed for chat_id=%s", self.chat_id, exc_info=True)
                await self._recover_stalled_message()
            self._spinner_index = (self._spinner_index + 1) % len(SPINNER_FRAMES)
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
        title = "Telecodex working" if self._status_lines else self._title
        plain_lines = [self._status_header_plain(title, elapsed)]
        html_lines = [self._status_header_html(title, elapsed)]
        if self._status_lines:
            plain_lines.extend(self._status_lines[-STATUS_HISTORY_LIMIT:])
            html_lines.extend(html.escape(item) for item in self._status_lines[-STATUS_HISTORY_LIMIT:])
        return self._render_progress_text(plain_lines, html_lines)

    def _render_working_text(self, elapsed: int, answer_text: str) -> RenderedTelegramText:
        plain_lines = [self._status_header_plain("Telecodex working", elapsed)]
        html_lines = [self._status_header_html("Telecodex working", elapsed)]
        if self._status_lines:
            plain_lines.extend(self._status_lines[-STATUS_HISTORY_LIMIT:])
            html_lines.extend(html.escape(item) for item in self._status_lines[-STATUS_HISTORY_LIMIT:])
        preview_text = self._stream_preview_text(answer_text)
        plain_lines.append(preview_text)
        html_lines.append(html.escape(preview_text))
        return self._render_progress_text(plain_lines, html_lines)

    def _render_progress_text(self, plain_lines: list[str], html_lines: list[str]) -> RenderedTelegramText:
        plain_text = self._fit_text("\n".join(plain_lines))
        html_text = self._fit_text("\n".join(html_lines))
        return RenderedTelegramText(html_text, parse_mode="HTML", fallback_text=plain_text)

    def _status_header_plain(self, title: str, elapsed: int) -> str:
        indicator = "🟢" if title == "Telecodex working" else "🔵"
        return f"{indicator} {title} {SPINNER_FRAMES[self._spinner_index]} ({elapsed}s)"

    def _status_header_html(self, title: str, elapsed: int) -> str:
        return f"<b>{html.escape(self._status_header_plain(title, elapsed))}</b>"

    def _stream_preview_text(self, text: str) -> str:
        body = text.strip() or "Empty response."
        if len(body) <= TELEGRAM_TEXT_LIMIT:
            return body
        suffix = "\n\n[Showing streamed response tail]"
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
        body = text.strip() or "Empty response."
        if len(body) <= TELEGRAM_TEXT_LIMIT:
            html_rendered = cls._render_code_blocks_html(body)
            if html_rendered is not None and len(html_rendered.text) <= TELEGRAM_TEXT_LIMIT:
                return html_rendered, False
            return RenderedTelegramText(body), False
        suffix = "\n\n[Full response sent as file]"
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
            await asyncio.wait_for(
                self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message.message_id,
                    text=rendered.text,
                    reply_markup=reply_markup,
                    parse_mode=rendered.parse_mode,
                ),
                timeout=self.api_timeout_sec,
            )
            self._last_rendered_text = rendered.text
            self._last_parse_mode = rendered.parse_mode
            self._last_delivery_at = asyncio.get_running_loop().time()
        except TelegramBadRequest as exc:
            error_text = str(exc)
            if "message is not modified" in error_text:
                self._last_rendered_text = rendered.text
                self._last_parse_mode = rendered.parse_mode
                self._last_delivery_at = asyncio.get_running_loop().time()
                return
            if "message to edit not found" in error_text:
                return
            if rendered.parse_mode and "can't parse entities" in error_text and rendered.fallback_text is not None:
                fallback = RenderedTelegramText(rendered.fallback_text)
                await asyncio.wait_for(
                    self.bot.edit_message_text(
                        chat_id=self.chat_id,
                        message_id=self.message.message_id,
                        text=fallback.text,
                        reply_markup=reply_markup,
                    ),
                    timeout=self.api_timeout_sec,
                )
                self._last_rendered_text = fallback.text
                self._last_parse_mode = None
                self._last_delivery_at = asyncio.get_running_loop().time()
                return
            raise

    async def _safe_send(self, rendered: RenderedTelegramText, reply_markup: InlineKeyboardMarkup | None) -> None:
        kwargs: dict[str, object] = {"parse_mode": rendered.parse_mode}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        self.message = await asyncio.wait_for(
            self.bot.send_message(self.chat_id, rendered.text, **kwargs),
            timeout=self.api_timeout_sec,
        )
        self._last_rendered_text = rendered.text
        self._last_parse_mode = rendered.parse_mode
        self._last_delivery_at = asyncio.get_running_loop().time()

    async def _recover_stalled_message(self) -> None:
        if self.message is None:
            return
        now = asyncio.get_running_loop().time()
        if self._last_delivery_at and now - self._last_delivery_at < self.stalled_delivery_sec:
            return
        rendered = self._render_current_text()
        try:
            await self._safe_send(rendered=rendered, reply_markup=None)
        except Exception:
            logger.warning("Telegram stream recovery send failed for chat_id=%s", self.chat_id, exc_info=True)

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
            try:
                await asyncio.wait_for(self._refresh_task, timeout=self.finalize_wait_sec)
            except asyncio.TimeoutError:
                self._refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._refresh_task
            self._refresh_task = None
        body = (final_text or self._answer_text or "").strip()
        rendered, was_truncated = self._render_final_text(body)
        async with self._edit_lock:
            try:
                try:
                    await self._safe_edit(rendered=rendered, reply_markup=reply_markup)
                except TelegramBadRequest as exc:
                    error_text = str(exc)
                    if "message is too long" not in error_text and "text is too long" not in error_text:
                        raise
                    fallback, was_truncated = self._render_final_text(body[: TELEGRAM_TEXT_LIMIT - 32])
                    await self._safe_edit(rendered=fallback, reply_markup=reply_markup)
            except Exception:
                logger.warning("Telegram final edit failed for chat_id=%s; sending a fresh final message", self.chat_id, exc_info=True)
                await self._safe_send(rendered=rendered, reply_markup=reply_markup)
        document_text = attachment_text or full_text or body
        if len(document_text) >= self.send_log_threshold or was_truncated:
            data = document_text.encode("utf-8", errors="replace")
            doc = BufferedInputFile(data, filename="codex_output.md")
            caption = "Full response" if success else f"Details ({summary})"
            await self.bot.send_document(self.chat_id, document=doc, caption=caption)
        return body
