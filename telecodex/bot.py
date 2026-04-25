from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import re
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telecodex.config import Settings
from telecodex.deepgram import DeepgramProviderError, DeepgramService, DeepgramServiceUnavailable
from telecodex.repository import ChatState, Repository, SessionRecord
from telecodex.runner import CodexRunner
from telecodex.streaming import TelegramStreamEditor

logger = logging.getLogger(__name__)

SAFE_HISTORY_FILENAME_RE = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]+')
TEXT_DOCUMENT_MAX_BYTES = 512 * 1024
CODEX_CONTEXT_DIAGNOSTIC_PROMPT = """Telecodex connection check.
Do not modify files.
Inspect the current workspace/session only as needed and answer concisely in plain text:
- current working directory
- whether previous session context is visible; if yes, summarize the previous assistant answer
- whether this project/session is usable
Keep the answer under 900 characters."""


@dataclass(slots=True)
class ActiveRun:
    started_at: float
    project_name: str
    codex_session_id: str | None
    cancel_event: asyncio.Event


@dataclass(slots=True)
class RestartRequest:
    chat_id: int
    requested_at: str


@dataclass(slots=True)
class PendingProjectDraft:
    name: str | None = None
    current_path: Path = Path("/")
    entries: list[str] = field(default_factory=list)


class AccessMiddleware(BaseMiddleware):
    def __init__(self, allowed_chat_ids: set[int]) -> None:
        self.allowed_chat_ids = allowed_chat_ids

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        chat_id = self._extract_chat_id(event)
        if chat_id in self.allowed_chat_ids:
            return await handler(event, data)
        await self._deny(event)
        return None

    @staticmethod
    def _extract_chat_id(event: Message | CallbackQuery) -> int | None:
        callback_message = getattr(event, "message", None)
        if callback_message is not None:
            return callback_message.chat.id
        chat = getattr(event, "chat", None)
        if chat is not None:
            return chat.id
        return None

    @staticmethod
    async def _deny(event: Message | CallbackQuery) -> None:
        callback_message = getattr(event, "message", None)
        if callback_message is not None:
            await callback_message.answer("Access denied")
            answer = getattr(event, "answer", None)
            if answer is not None:
                await answer()
            return
        await event.answer("Access denied")


class TelecodexApplication:
    def __init__(
        self,
        bot: Bot,
        dispatcher: Dispatcher,
        repo: Repository,
        runner: CodexRunner,
        settings: Settings,
        deepgram: DeepgramService | None = None,
        restart_callback: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.repo = repo
        self.runner = runner
        self.settings = settings
        self.deepgram = deepgram
        self.restart_callback = restart_callback or self._restart_process
        self.active_runs: dict[int, ActiveRun] = {}
        self.projects: dict[str, Path] = {}
        self.pending_project_drafts: dict[int, PendingProjectDraft] = {}
        self.router = Router()
        access_middleware = AccessMiddleware(self.settings.admin_chat_ids)
        self.router.message.middleware(access_middleware)
        self.router.callback_query.middleware(access_middleware)
        self._register_handlers()
        self.dispatcher.include_router(self.router)

    async def configure_bot_commands(self) -> None:
        await self.bot.set_my_commands(self._bot_commands())

    async def load_projects(self) -> None:
        stored = await self.repo.list_projects()
        self.projects = {item.name: Path(item.project_path) for item in stored}

    @staticmethod
    def _bot_commands() -> list[BotCommand]:
        return [
            BotCommand(command="menu", description="Show menu"),
            BotCommand(command="projects", description="List projects"),
            BotCommand(command="sessions", description="List sessions"),
            BotCommand(command="session_id", description="Show current session ID"),
            BotCommand(command="status", description="Current status"),
            BotCommand(command="cancel", description="Stop task"),
            BotCommand(command="restart", description="Restart service"),
        ]

    def _register_handlers(self) -> None:
        @self.router.message(Command("start", "menu"))
        async def start(message: Message) -> None:
            await self._send_menu(message)

        @self.router.message(Command("projects"))
        async def projects(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await message.answer("Available projects:", reply_markup=self._project_keyboard())

        @self.router.message(Command("project"))
        async def set_project(message: Message) -> None:
            chat_id = message.chat.id
            self.pending_project_drafts.pop(chat_id, None)
            args = _command_arg(message.text)
            if not args:
                await message.answer("Select a project:", reply_markup=self._project_keyboard())
                return
            project_name = args.strip()
            if project_name not in self.projects:
                await message.answer("Unknown project. Use the buttons or /projects.")
                return
            latest_session = await self._latest_session_for_project(project_name)
            await self.repo.set_chat_state(
                chat_id=chat_id,
                project_name=project_name,
                codex_session_id=latest_session.codex_session_id if latest_session else None,
            )
            status_message = await message.answer(f"Checking project/session with Codex: {project_name}")
            await status_message.edit_text(
                await self._run_context_diagnostic(chat_id, project_name, latest_session),
                reply_markup=self._menu_keyboard(),
                parse_mode="HTML",
            )

        @self.router.message(Command("pwd"))
        async def pwd(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("No project selected. Use /menu.")
                return
            project_path = self.projects[state.project_name]
            await message.answer(f"{state.project_name}: {project_path}")

        @self.router.message(Command("sessions"))
        async def sessions(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            arg = _command_arg(message.text).strip()
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Select a project first via /menu.")
                return
            if not arg:
                items = await self.repo.list_sessions(state.project_name, self.settings.sessions_list_limit)
                if not items:
                    await message.answer(
                        f"No sessions yet for project {state.project_name}.",
                        reply_markup=self._new_session_keyboard(),
                    )
                    return
                lines = [self._format_session_line(item, state.codex_session_id) for item in items]
                await message.answer(
                    self._sessions_title(state.project_name) + "\n" + "\n".join(lines),
                    reply_markup=self._session_keyboard(items),
                    parse_mode="HTML",
                )
                return
            if arg == "new":
                await self.repo.set_chat_state(message.chat.id, state.project_name, None)
                await message.answer(
                    "The next message/request will start a new Codex session",
                    reply_markup=self._menu_keyboard(),
                )
                return
            selected = await self.repo.get_session(arg)
            if not selected or selected.project_name != state.project_name:
                await message.answer("Session not found in the current project.")
                return
            await self.repo.set_chat_state(message.chat.id, state.project_name, selected.codex_session_id)
            status_message = await message.answer(f"Checking session with Codex: {self._session_title(selected)}")
            await status_message.edit_text(
                await self._run_context_diagnostic(message.chat.id, state.project_name, selected),
                reply_markup=self._menu_keyboard(),
                parse_mode="HTML",
            )

        @self.router.message(Command("session_name"))
        async def session_name(message: Message) -> None:
            alias = _command_arg(message.text)
            if not alias:
                await message.answer("Usage: /session_name <alias>")
                return
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.codex_session_id:
                await message.answer("Select a session first.")
                return
            updated = await self.repo.rename_session(state.codex_session_id, alias.strip())
            if not updated:
                await message.answer("Could not update alias.")
                return
            await message.answer("Session name updated.", reply_markup=self._menu_keyboard())

        @self.router.message(Command("session_id"))
        async def session_id(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await self._handle_session_id(message)

        @self.router.message(Command("whereami", "status"))
        async def whereami(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await message.answer(await self._state_card(message.chat.id), reply_markup=self._menu_keyboard(), parse_mode="HTML")

        @self.router.message(Command("cancel"))
        async def cancel(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            run = self.active_runs.get(message.chat.id)
            if run is None:
                await message.answer("There is no active task.")
                return
            run.cancel_event.set()
            await message.answer("Cancellation requested.", reply_markup=self._menu_keyboard())

        @self.router.message(Command("restart"))
        async def restart(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await self._handle_restart(message)

        @self.router.message(F.voice)
        async def run_voice(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await self._handle_voice_message(message)

        @self.router.message(F.document)
        async def run_document(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await self._handle_document_message(message)

        @self.router.message(F.text & ~F.text.startswith("/"))
        async def run_text(message: Message) -> None:
            if message.chat.id in self.pending_project_drafts:
                await self._handle_project_creation_input(message)
                return
            await self._execute_prompt(message, message.text or "")

        @self.router.callback_query(F.data == "menu:root")
        async def menu_root(callback: CallbackQuery) -> None:
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            await self._edit_callback_message(
                callback,
                await self._state_card(callback.message.chat.id),
                self._menu_keyboard(),
                parse_mode="HTML",
            )
            await callback.answer()

        @self.router.callback_query(F.data == "project:list")
        async def project_list(callback: CallbackQuery) -> None:
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            await self._show_project_list(callback)
            await callback.answer()

        @self.router.callback_query(F.data == "project:delete:list")
        async def project_delete_list(callback: CallbackQuery) -> None:
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            await self._show_project_delete_list(callback)
            await callback.answer()

        @self.router.callback_query(F.data.startswith("project:delete:confirm:"))
        async def project_delete_confirm(callback: CallbackQuery) -> None:
            project_name = callback.data.removeprefix("project:delete:confirm:")
            project_path = self.projects.get(project_name)
            if project_path is None:
                await callback.answer("Project not found.", show_alert=True)
                return
            await self._edit_callback_message(
                callback,
                "Delete this project?\n" f"<code>{html.escape(self._project_button_label(project_name, project_path))}</code>",
                self._project_delete_confirm_keyboard(project_name),
                parse_mode="HTML",
            )
            await callback.answer()

        @self.router.callback_query(F.data.startswith("project:delete:yes:"))
        async def project_delete_yes(callback: CallbackQuery) -> None:
            project_name = callback.data.removeprefix("project:delete:yes:")
            deleted = await self.repo.delete_project(project_name)
            if not deleted:
                await callback.answer("Project not found.", show_alert=True)
                return
            self.projects.pop(project_name, None)
            await self._show_project_delete_list(callback)
            await callback.answer("Project deleted.")

        @self.router.callback_query(F.data == "project:delete:no")
        async def project_delete_no(callback: CallbackQuery) -> None:
            await self._show_project_delete_list(callback)
            await callback.answer("Deletion cancelled.")

        @self.router.callback_query(F.data == "project:new")
        async def project_new(callback: CallbackQuery) -> None:
            self.pending_project_drafts[callback.message.chat.id] = PendingProjectDraft()
            await self._edit_callback_message(
                callback,
                "Enter the new project name:",
                self._project_creation_keyboard(),
            )
            await callback.answer()

        @self.router.callback_query(F.data == "project:new:cancel")
        async def project_new_cancel(callback: CallbackQuery) -> None:
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            await self._edit_callback_message(callback, "Select a project:", self._project_keyboard())
            await callback.answer("Project creation cancelled.")

        @self.router.callback_query(F.data == "project:new:path:up")
        async def project_new_path_up(callback: CallbackQuery) -> None:
            draft = self.pending_project_drafts.get(callback.message.chat.id)
            if draft is None or draft.name is None:
                await callback.answer("Enter the project name first.", show_alert=True)
                return
            if draft.current_path != draft.current_path.parent:
                draft.current_path = draft.current_path.parent
            await self._edit_callback_message(
                callback,
                self._project_path_browser_text(draft),
                self._project_path_browser_keyboard(draft),
                parse_mode="HTML",
            )
            await callback.answer()

        @self.router.callback_query(F.data.startswith("project:new:path:open:"))
        async def project_new_path_open(callback: CallbackQuery) -> None:
            draft = self.pending_project_drafts.get(callback.message.chat.id)
            if draft is None or draft.name is None:
                await callback.answer("Enter the project name first.", show_alert=True)
                return
            index_text = callback.data.removeprefix("project:new:path:open:")
            try:
                index = int(index_text)
            except ValueError:
                await callback.answer("Folder not found.", show_alert=True)
                return
            entries = self._project_browser_entries(draft.current_path)
            if index < 0 or index >= len(entries):
                await callback.answer("Folder not found.", show_alert=True)
                return
            draft.current_path = draft.current_path / entries[index]
            await self._edit_callback_message(
                callback,
                self._project_path_browser_text(draft),
                self._project_path_browser_keyboard(draft),
                parse_mode="HTML",
            )
            await callback.answer()

        @self.router.callback_query(F.data == "project:new:path:select")
        async def project_new_path_select(callback: CallbackQuery) -> None:
            draft = self.pending_project_drafts.get(callback.message.chat.id)
            if draft is None or draft.name is None:
                await callback.answer("Enter the project name first.", show_alert=True)
                return
            await self._complete_project_creation(callback.message.chat.id, draft)
            await self._edit_callback_message(
                callback,
                f"Project created: {draft.name}\nPath: {draft.current_path}\nThe next request will start a new session.",
                self._menu_keyboard(),
            )
            await callback.answer("Project created.")

        @self.router.callback_query(F.data.startswith("project:set:"))
        async def project_set(callback: CallbackQuery) -> None:
            project_name = callback.data.removeprefix("project:set:")
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            if project_name not in self.projects:
                await callback.answer("Project not found.", show_alert=True)
                return
            latest_session = await self._latest_session_for_project(project_name)
            await self.repo.set_chat_state(
                callback.message.chat.id,
                project_name,
                latest_session.codex_session_id if latest_session else None,
            )
            await self._edit_callback_message(callback, f"Checking project/session with Codex: {project_name}")
            await callback.answer("Project updated.")
            await self._edit_callback_message(
                callback,
                await self._run_context_diagnostic(callback.message.chat.id, project_name, latest_session),
                self._menu_keyboard(),
                parse_mode="HTML",
            )

        @self.router.callback_query(F.data == "session:list")
        async def session_list(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            await self._show_session_list(callback, state.project_name, state.codex_session_id)
            await callback.answer()

        @self.router.callback_query(F.data == "session:new")
        async def session_new(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            await self.repo.set_chat_state(callback.message.chat.id, state.project_name, None)
            await self._edit_callback_message(
                callback,
                "The next message/request will start a new Codex session",
                self._menu_keyboard(),
            )
            await callback.answer("A new session will be started.")

        @self.router.callback_query(F.data == "session:delete:list")
        async def session_delete_list(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            await self._show_session_delete_list(callback, state.project_name)
            await callback.answer()

        @self.router.callback_query(F.data.startswith("session:delete:confirm:"))
        async def session_delete_confirm(callback: CallbackQuery) -> None:
            codex_session_id = callback.data.removeprefix("session:delete:confirm:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            session_item = await self.repo.get_session(codex_session_id)
            if not session_item or session_item.project_name != state.project_name:
                await callback.answer("Session not found.", show_alert=True)
                return
            await self._edit_callback_message(
                callback,
                "Delete this session?\n" f"<code>{html.escape(self._session_title(session_item))}</code>",
                self._session_delete_confirm_keyboard(session_item.codex_session_id),
                parse_mode="HTML",
            )
            await callback.answer()

        @self.router.callback_query(F.data.startswith("session:delete:yes:"))
        async def session_delete_yes(callback: CallbackQuery) -> None:
            codex_session_id = callback.data.removeprefix("session:delete:yes:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            deleted = await self.repo.delete_session(codex_session_id)
            if not deleted:
                await callback.answer("Session not found.", show_alert=True)
                return
            await self._show_session_delete_list(callback, state.project_name)
            await callback.answer("Session deleted.")

        @self.router.callback_query(F.data == "session:delete:no")
        async def session_delete_no(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            await self._show_session_delete_list(callback, state.project_name)
            await callback.answer("Deletion cancelled.")

        @self.router.callback_query(F.data.startswith("session:set:"))
        async def session_set(callback: CallbackQuery) -> None:
            codex_session_id = callback.data.removeprefix("session:set:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Select a project first.", show_alert=True)
                return
            selected = await self.repo.get_session(codex_session_id)
            if not selected or selected.project_name != state.project_name:
                await callback.answer("Session not found.", show_alert=True)
                return
            await self.repo.set_chat_state(callback.message.chat.id, state.project_name, selected.codex_session_id)
            await self._edit_callback_message(callback, f"Checking session with Codex: {self._session_title(selected)}")
            await callback.answer("Session updated.")
            await self._edit_callback_message(
                callback,
                await self._run_context_diagnostic(callback.message.chat.id, state.project_name, selected),
                self._menu_keyboard(),
                parse_mode="HTML",
            )

        @self.router.callback_query(F.data == "action:stop")
        async def action_stop(callback: CallbackQuery) -> None:
            run = self.active_runs.get(callback.message.chat.id)
            if run is None:
                await callback.answer("There is no active task.")
                return
            run.cancel_event.set()
            await callback.answer("Cancellation requested.")

        @self.router.callback_query(F.data == "help:show")
        async def help_show(callback: CallbackQuery) -> None:
            text = (
                "How to use the bot:\n"
                "1. Select a project.\n"
                "2. Select a saved session or start a new one.\n"
                "3. Send your task as a regular message."
            )
            await self._edit_callback_message(callback, text, self._menu_keyboard())
            await callback.answer()

    async def _execute_prompt(self, message: Message, prompt: str) -> None:
        chat_id = message.chat.id
        if chat_id in self.active_runs:
            await message.answer("A task is already running in this chat. Use /status or the Stop button.")
            return

        state = await self.repo.get_chat_state(chat_id)
        if not state or not state.project_name:
            await message.answer("Select a project first via /menu.")
            return

        current_session = await self._get_selected_session(state)
        telegram_user_id = message.from_user.id if message.from_user else chat_id

        stream = TelegramStreamEditor(
            bot=self.bot,
            chat_id=chat_id,
            interval_sec=self.settings.stream_update_interval_sec,
            tail_chars=self.settings.stream_tail_chars,
            send_log_threshold=self.settings.stream_send_log_threshold,
        )
        await stream.start("Telecodex thinking")
        cancel_event = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_loop(chat_id, cancel_event))
        self.active_runs[chat_id] = ActiveRun(
            started_at=asyncio.get_running_loop().time(),
            project_name=state.project_name,
            codex_session_id=current_session.codex_session_id if current_session else None,
            cancel_event=cancel_event,
        )

        result = None
        runner_error: Exception | None = None
        try:
            result = await self.runner.run(
                project_path=str(self.projects[state.project_name]),
                codex_session_id=current_session.codex_session_id if current_session else None,
                user_prompt=prompt,
                on_progress=stream.publish_status,
                on_message=stream.publish_answer,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            runner_error = exc
            logger.exception(
                "Codex runner crashed",
                extra={"chat_id": chat_id, "project_name": state.project_name},
            )
        finally:
            cancel_event.set()
            await typing_task
            self.active_runs.pop(chat_id, None)

        if runner_error is not None:
            await stream.finish(
                False,
                "failed: internal error",
                final_text="Codex failed before it could produce a reply.",
                reply_markup=self._result_keyboard(),
                attachment_text=str(runner_error),
            )
            return

        _append_conversation_log(
            self._conversation_log_path(telegram_user_id, state.project_name),
            timestamp=datetime.now(UTC),
            user_prompt=prompt,
            command=result.command,
            codex_output=result.raw_output,
        )

        active_codex_session_id = current_session.codex_session_id if current_session else None
        if result.codex_session_id:
            saved = await self.repo.save_session(
                result.codex_session_id,
                state.project_name,
                str(self.projects[state.project_name]),
            )
            await self.repo.set_chat_state(chat_id, state.project_name, saved.codex_session_id)
            active_codex_session_id = saved.codex_session_id
        elif active_codex_session_id:
            await self.repo.touch_session(active_codex_session_id)

        assistant_text = (result.assistant_text or result.display_text or result.output).strip()
        if result.cancelled:
            summary = "cancelled"
            final_text = assistant_text or "Run cancelled."
        elif result.timed_out:
            summary = "failed: timeout"
            final_text = assistant_text or "Codex did not return a reply before the timeout."
        elif result.success:
            summary = "done"
            final_text = assistant_text or "Empty response."
        else:
            summary = f"failed: code={result.return_code}"
            final_text = assistant_text or "Codex exited with an error and no text response."

        await stream.finish(
            result.success,
            summary,
            final_text=final_text,
            full_text=assistant_text,
            reply_markup=self._result_keyboard(),
            attachment_text=result.raw_output if not result.success and not assistant_text else assistant_text,
        )

    async def _handle_voice_message(self, message: Message) -> None:
        if not message.voice:
            return
        if self.deepgram is None:
            await message.answer("Voice messages are unavailable: DEEPGRAM_API_KEY is not configured.")
            return

        try:
            voice_bytes = await self._download_voice_bytes(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        except Exception:
            logger.exception("Voice download failed", extra={"chat_id": message.chat.id})
            await message.answer("Failed to download the voice message.")
            return

        status_message = await message.answer("Transcribing voice message ⠋")
        stop_event = asyncio.Event()
        indicator_task = asyncio.create_task(_progress_message_indicator(status_message, stop_event))
        try:
            transcript = await self.deepgram.transcribe_ogg_opus(voice_bytes)
        except DeepgramServiceUnavailable:
            await message.answer("Deepgram is temporarily unavailable. Try again later.")
            return
        except DeepgramProviderError as exc:
            await message.answer(f"Transcription error: {exc}")
            return
        except Exception:
            logger.exception("Deepgram unexpected error", extra={"chat_id": message.chat.id})
            await message.answer("Unexpected voice transcription error.")
            return
        finally:
            stop_event.set()
            with suppress(Exception):
                await indicator_task
            with suppress(Exception):
                await status_message.delete()

        await message.answer(transcript)
        await self._execute_prompt(message, transcript)

    async def _handle_document_message(self, message: Message) -> None:
        if not message.document:
            return

        try:
            filename, document_text = await self._download_text_document(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        except Exception:
            logger.exception("Document download failed", extra={"chat_id": message.chat.id})
            await message.answer("Failed to download the text file.")
            return

        prompt = _build_document_prompt(filename, message.caption or "", document_text)
        await self._execute_prompt(message, prompt)

    async def _download_voice_bytes(self, message: Message) -> bytes:
        if not message.voice:
            raise ValueError("Voice message not found.")
        file = await message.bot.get_file(message.voice.file_id)
        if not file.file_path:
            raise ValueError("Could not retrieve the voice file.")
        buffer = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buffer)
        voice_bytes = buffer.getvalue()
        if not voice_bytes:
            raise ValueError("Could not download the voice message.")
        return voice_bytes

    async def _download_text_document(self, message: Message) -> tuple[str, str]:
        if not message.document:
            raise ValueError("Text file not found.")
        document = message.document
        filename = document.file_name or "attachment.txt"
        if document.file_size and document.file_size > TEXT_DOCUMENT_MAX_BYTES:
            raise ValueError("Text files larger than 512 KiB are not supported.")
        file = await message.bot.get_file(document.file_id)
        if not file.file_path:
            raise ValueError("Could not retrieve the text file.")
        buffer = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buffer)
        payload = buffer.getvalue()
        if not payload:
            raise ValueError("Could not download the text file.")
        if len(payload) > TEXT_DOCUMENT_MAX_BYTES:
            raise ValueError("Text files larger than 512 KiB are not supported.")
        return filename, _decode_text_document(payload)

    async def _get_selected_session(self, state: ChatState) -> SessionRecord | None:
        if not state.codex_session_id:
            return None
        session = await self.repo.get_session(state.codex_session_id)
        if session:
            return session
        await self.repo.set_chat_state(state.chat_id, state.project_name, None)
        return None

    def _conversation_log_path(self, telegram_user_id: int, project_name: str) -> Path:
        return self.settings.history_dir / str(telegram_user_id) / f"{_safe_history_log_stem(project_name)}.log"

    def _restart_marker_path(self) -> Path:
        return self.settings.db_path.parent / "restart_request.json"

    async def _typing_loop(self, chat_id: int, cancel_event: asyncio.Event) -> None:
        while not cancel_event.is_set():
            try:
                await self.bot.send_chat_action(chat_id=chat_id, action="typing")
            except Exception:
                logger.exception("Failed to send typing action", extra={"chat_id": chat_id})
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                continue

    async def _latest_session_for_project(self, project_name: str) -> SessionRecord | None:
        items = await self.repo.list_sessions(project_name, limit=1)
        return items[0] if items else None

    async def _run_context_diagnostic(
        self,
        chat_id: int,
        project_name: str,
        session: SessionRecord | None,
    ) -> str:
        if chat_id in self.active_runs:
            return "Codex check skipped: another task is already running in this chat."

        project_path = self.projects[project_name]
        cancel_event = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_loop(chat_id, cancel_event))
        self.active_runs[chat_id] = ActiveRun(
            started_at=asyncio.get_running_loop().time(),
            project_name=project_name,
            codex_session_id=session.codex_session_id if session else None,
            cancel_event=cancel_event,
        )
        try:
            result = await self.runner.run(
                project_path=str(project_path),
                codex_session_id=session.codex_session_id if session else None,
                user_prompt=CODEX_CONTEXT_DIAGNOSTIC_PROMPT,
                on_progress=None,
                on_message=None,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            logger.exception("Codex context diagnostic failed", extra={"chat_id": chat_id, "project_name": project_name})
            return self._context_diagnostic_error_card(project_name, project_path, session, str(exc))
        finally:
            cancel_event.set()
            await typing_task
            self.active_runs.pop(chat_id, None)

        selected_session = session
        if result.codex_session_id:
            selected_session = await self.repo.save_session(result.codex_session_id, project_name, str(project_path))
            await self.repo.set_chat_state(chat_id, project_name, selected_session.codex_session_id)
        elif selected_session:
            await self.repo.touch_session(selected_session.codex_session_id)

        return self._context_diagnostic_card(project_name, project_path, selected_session, result)

    def _context_diagnostic_card(
        self,
        project_name: str,
        project_path: Path,
        session: SessionRecord | None,
        result,
    ) -> str:
        model, effort = self._codex_model_and_effort()
        session_text = self._session_title(session) if session else "new session"
        reply = (result.assistant_text or result.display_text or result.output or "").strip()
        if not reply:
            reply = "No diagnostic reply returned."
        status = "ok" if result.success else f"failed: code={result.return_code}"
        if result.cancelled:
            status = "cancelled"
        elif result.timed_out:
            status = "failed: timeout"
        lines = [
            f"Project: {html.escape(project_name)}",
            f"Path: <code>{html.escape(str(project_path))}</code>",
            f"Session: {self._session_text_html(session_text)}",
            f"Codex status: {html.escape(status)}",
            f"Model: {html.escape(model)}",
            f"Effort: {html.escape(effort)}",
        ]
        context_remaining = _format_context_remaining(result.token_info)
        five_hour_limit = _format_rate_limit(result.rate_limits, 300)
        weekly_limit = _format_rate_limit(result.rate_limits, 10080)
        if context_remaining is not None:
            lines.append(f"Context: {html.escape(context_remaining)}")
        if five_hour_limit is not None:
            lines.append(f"5h limit: {html.escape(five_hour_limit)}")
        if weekly_limit is not None:
            lines.append(f"Weekly limit: {html.escape(weekly_limit)}")
        lines.append("")
        lines.append(f"Codex reply:\n{html.escape(_truncate_text(reply, 1600))}")
        return "\n".join(lines)

    def _context_diagnostic_error_card(
        self,
        project_name: str,
        project_path: Path,
        session: SessionRecord | None,
        error_text: str,
    ) -> str:
        model, effort = self._codex_model_and_effort()
        session_text = self._session_title(session) if session else "new session"
        return (
            f"Project: {html.escape(project_name)}\n"
            f"Path: <code>{html.escape(str(project_path))}</code>\n"
            f"Session: {self._session_text_html(session_text)}\n"
            "Codex status: failed before reply\n"
            f"Model: {html.escape(model)}\n"
            f"Effort: {html.escape(effort)}\n\n"
            f"Codex error:\n{html.escape(_truncate_text(error_text, 1600))}"
        )

    def _codex_model_and_effort(self) -> tuple[str, str]:
        model = _extract_cli_option(self.runner.command, "--model", "-m") or _read_codex_config_value("model") or "unknown"
        effort = _extract_config_value(self.runner.command, "model_reasoning_effort")
        if effort is None:
            effort = _read_codex_config_value("model_reasoning_effort")
        return model, effort or "unknown"

    async def _send_menu(self, message: Message) -> None:
        self.pending_project_drafts.pop(message.chat.id, None)
        await message.answer(await self._state_card(message.chat.id), reply_markup=self._menu_keyboard(), parse_mode="HTML")

    async def _handle_project_creation_input(self, message: Message) -> None:
        draft = self.pending_project_drafts.get(message.chat.id)
        if draft is None:
            await self._execute_prompt(message, message.text or "")
            return
        user_text = (message.text or "").strip()
        if not user_text:
            await message.answer("Enter a non-empty value.", reply_markup=self._project_creation_keyboard())
            return
        if draft.name is None:
            if user_text in self.projects:
                await message.answer("A project with this name already exists. Enter a different name.", reply_markup=self._project_creation_keyboard())
                return
            draft.name = user_text
            draft.current_path = Path("/")
            await message.answer(
                self._project_path_browser_text(draft),
                reply_markup=self._project_path_browser_keyboard(draft),
                parse_mode="HTML",
            )
            return

        await message.answer(
            "Choose the project path with the buttons below.",
            reply_markup=self._project_path_browser_keyboard(draft),
        )

    async def _handle_restart(self, message: Message) -> None:
        chat_id = message.chat.id
        if not self._is_admin_chat(chat_id):
            await message.answer("Command unavailable.")
            return
        if self.active_runs:
            await message.answer("There are active tasks. Wait for them to finish or use /cancel first.")
            return
        logger.warning("Restart requested by admin", extra={"chat_id": chat_id})
        _save_restart_request(self._restart_marker_path(), chat_id=chat_id, requested_at=datetime.now(UTC))
        await message.answer("Service restart requested.")
        asyncio.create_task(self.restart_callback())

    async def _handle_session_id(self, message: Message) -> None:
        state = await self.repo.get_chat_state(message.chat.id)
        if not state or not state.codex_session_id:
            await message.answer("No session selected.")
            return
        session = await self._get_selected_session(state)
        if session is None:
            await message.answer("No session selected.")
            return
        await message.answer(f"Current session ID:\n<code>{html.escape(session.codex_session_id)}</code>", parse_mode="HTML")

    async def notify_restart_success_if_needed(self) -> None:
        request = _load_restart_request(self._restart_marker_path())
        if request is None:
            return
        try:
            await self.bot.send_message(request.chat_id, "The service restarted successfully.")
        except Exception:
            logger.exception("Failed to send restart success notification", extra={"chat_id": request.chat_id})
            return
        _clear_restart_request(self._restart_marker_path())

    async def _state_card(self, chat_id: int) -> str:
        state = await self.repo.get_chat_state(chat_id)
        run = self.active_runs.get(chat_id)
        project = state.project_name if state and state.project_name else "not selected"
        project_path = str(self.projects[state.project_name]) if state and state.project_name else "-"
        session_text = "not selected"
        updated_at = state.updated_at if state else ""
        if state and state.codex_session_id:
            session_item = await self.repo.get_session(state.codex_session_id)
            if session_item:
                session_text = self._session_title(session_item)
                updated_at = session_item.updated_at
        status = "running" if run else "idle"
        if run:
            elapsed = int(asyncio.get_running_loop().time() - run.started_at)
            status = f"running ({elapsed}s)"
        last_seen = self._format_timestamp(updated_at) if updated_at else "-"
        return (
            "Telecodex\n"
            f"Project: {html.escape(project)}\n"
            f"Path: {html.escape(project_path)}\n"
            f"Session: {self._session_text_html(session_text)}\n"
            f"Status: {html.escape(status)}\n"
            f"Last activity: {html.escape(last_seen)}"
        )

    async def _edit_callback_message(
        self,
        callback: CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if callback.message is None:
            return
        await callback.message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

    def _menu_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Project", callback_data="project:list")
        builder.button(text="Session", callback_data="session:list")
        builder.button(text="New session", callback_data="session:new")
        builder.button(text="Stop", callback_data="action:stop")
        builder.button(text="Help", callback_data="help:show")
        builder.adjust(2, 2, 1)
        return builder.as_markup()

    def _project_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for name, path in self.projects.items():
            builder.button(text=self._project_button_label(name, path), callback_data=f"project:set:{name}")
        builder.button(text="Delete project", callback_data="project:delete:list")
        builder.button(text="New project", callback_data="project:new")
        builder.button(text="Back", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _project_delete_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for name, path in self.projects.items():
            builder.button(
                text=f"❌ {self._project_button_label(name, path)}",
                callback_data=f"project:delete:confirm:{name}",
            )
        builder.button(text="Back", callback_data="project:list")
        builder.adjust(1)
        return builder.as_markup()

    def _project_delete_confirm_keyboard(self, project_name: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Yes", callback_data=f"project:delete:yes:{project_name}")
        builder.button(text="No", callback_data="project:delete:no")
        builder.adjust(2)
        return builder.as_markup()

    def _project_creation_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Back", callback_data="project:new:cancel")
        builder.adjust(1)
        return builder.as_markup()

    @staticmethod
    def _project_button_label(project_name: str, project_path: Path) -> str:
        return f"{project_name} ({project_path})"

    def _project_path_browser_keyboard(self, draft: PendingProjectDraft) -> InlineKeyboardMarkup:
        entries = self._project_browser_entries(draft.current_path)
        draft.entries = entries
        rows: list[list[InlineKeyboardButton]] = []
        if draft.current_path != draft.current_path.parent:
            rows.append([InlineKeyboardButton(text="⬆️ ..", callback_data="project:new:path:up")])
        rows.extend(self._project_browser_folder_rows(entries))
        rows.append([InlineKeyboardButton(text=f"✅ {draft.current_path}", callback_data="project:new:path:select")])
        rows.append([InlineKeyboardButton(text="Back", callback_data="project:new:cancel")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _project_path_browser_text(self, draft: PendingProjectDraft) -> str:
        return (
            f"Project name: {html.escape(draft.name or '-')}\n"
            "Choose the project root folder:\n"
            f"<code>{html.escape(str(draft.current_path))}</code>"
        )

    def _project_browser_entries(self, path: Path) -> list[str]:
        try:
            items = [item.name for item in path.iterdir() if item.is_dir()]
        except OSError:
            logger.warning("Failed to list project browser directory", extra={"path": str(path)})
            return []
        return sorted(items, key=str.casefold)

    def _project_browser_folder_rows(self, entries: list[str]) -> list[list[InlineKeyboardButton]]:
        rows: list[list[InlineKeyboardButton]] = []
        current_row: list[InlineKeyboardButton] = []
        current_width = 0
        for index, name in enumerate(entries):
            button = InlineKeyboardButton(text=f"📁 {name}", callback_data=f"project:new:path:open:{index}")
            button_width = max(8, len(name) + 3)
            if current_row and (len(current_row) >= 3 or current_width + button_width > 30):
                rows.append(current_row)
                current_row = []
                current_width = 0
            current_row.append(button)
            current_width += button_width
        if current_row:
            rows.append(current_row)
        return rows

    async def _complete_project_creation(self, chat_id: int, draft: PendingProjectDraft) -> None:
        project_name = draft.name or ""
        project_path = draft.current_path
        await self.repo.save_project(project_name, str(project_path))
        self.projects[project_name] = project_path
        self.pending_project_drafts.pop(chat_id, None)
        await self.repo.set_chat_state(chat_id, project_name, None)

    async def _show_project_list(self, callback: CallbackQuery) -> None:
        await self._edit_callback_message(callback, "Select a project:", self._project_keyboard())

    async def _show_project_delete_list(self, callback: CallbackQuery) -> None:
        if not self.projects:
            await self._edit_callback_message(
                callback,
                "No projects yet.",
                self._project_creation_keyboard(),
            )
            return
        await self._edit_callback_message(
            callback,
            "Delete projects:",
            self._project_delete_keyboard(),
        )

    def _session_keyboard(self, sessions: list[SessionRecord]) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for item in sessions:
            builder.button(text=self._session_title(item), callback_data=f"session:set:{item.codex_session_id}")
        builder.button(text="Delete session", callback_data="session:delete:list")
        builder.button(text="New session", callback_data="session:new")
        builder.button(text="Back", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _session_delete_keyboard(self, sessions: list[SessionRecord]) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for item in sessions:
            builder.button(
                text=f"❌ {self._session_title(item)}",
                callback_data=f"session:delete:confirm:{item.codex_session_id}",
            )
        builder.button(text="Back", callback_data="session:list")
        builder.adjust(1)
        return builder.as_markup()

    def _session_delete_confirm_keyboard(self, codex_session_id: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Yes", callback_data=f"session:delete:yes:{codex_session_id}")
        builder.button(text="No", callback_data="session:delete:no")
        builder.adjust(2)
        return builder.as_markup()

    def _session_delete_empty_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Back", callback_data="session:list")
        builder.adjust(1)
        return builder.as_markup()

    def _new_session_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="New session", callback_data="session:new")
        builder.button(text="Back", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _result_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="New session", callback_data="session:new")
        builder.button(text="Switch project", callback_data="project:list")
        builder.adjust(2)
        return builder.as_markup()

    def _is_admin_chat(self, chat_id: int) -> bool:
        return chat_id in self.settings.admin_chat_ids

    async def _restart_process(self) -> None:
        await asyncio.sleep(0.2)
        await self.dispatcher.stop_polling()
        os.kill(os.getpid(), signal.SIGTERM)

    @staticmethod
    def _session_title(session: SessionRecord) -> str:
        updated = TelecodexApplication._format_session_stamp(session.updated_at)
        return f"{session.project_name}{session.codex_session_id[-13:]}|{updated}"

    @staticmethod
    def _sessions_title(project_name: str) -> str:
        return f"Project sessions: {project_name}"

    @staticmethod
    def _delete_sessions_title(project_name: str) -> str:
        return f"Delete sessions: {project_name}"

    def _format_session_line(self, session: SessionRecord, active_session_id: str | None) -> str:
        marker = "•"
        if session.codex_session_id == active_session_id:
            marker = "→"
        return f"{marker} <code>{html.escape(self._session_title(session))}</code>"

    async def _show_session_list(
        self,
        callback: CallbackQuery,
        project_name: str,
        active_session_id: str | None,
    ) -> None:
        items = await self.repo.list_sessions(project_name, self.settings.sessions_list_limit)
        if not items:
            await self._edit_callback_message(
                callback,
                f"No sessions yet for project {project_name}.",
                self._new_session_keyboard(),
            )
            return
        lines = [self._format_session_line(item, active_session_id) for item in items]
        await self._edit_callback_message(
            callback,
            self._sessions_title(project_name) + "\n" + "\n".join(lines),
            self._session_keyboard(items),
            parse_mode="HTML",
        )

    async def _show_session_delete_list(self, callback: CallbackQuery, project_name: str) -> None:
        items = await self.repo.list_sessions(project_name, self.settings.sessions_list_limit)
        if not items:
            await self._edit_callback_message(
                callback,
                f"No sessions yet for project {project_name}.",
                self._session_delete_empty_keyboard(),
            )
            return
        await self._edit_callback_message(
            callback,
            self._delete_sessions_title(project_name),
            self._session_delete_keyboard(items),
        )

    @staticmethod
    def _format_timestamp(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    @staticmethod
    def _format_session_stamp(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%y-%m-%d|%H:%M")
        except ValueError:
            return "--.--.--|--:--"

    @staticmethod
    def _session_text_html(value: str) -> str:
        if value == "not selected":
            return value
        return f"<code>{html.escape(value)}</code>"


def _extract_cli_option(command: list[str], long_name: str, short_name: str) -> str | None:
    for index, item in enumerate(command):
        if item in {long_name, short_name} and index + 1 < len(command):
            return command[index + 1]
        prefix = f"{long_name}="
        if item.startswith(prefix):
            return item.removeprefix(prefix)
    return None


def _extract_config_value(command: list[str], key: str) -> str | None:
    for index, item in enumerate(command):
        value = None
        if item in {"--config", "-c"} and index + 1 < len(command):
            value = command[index + 1]
        elif item.startswith("--config="):
            value = item.removeprefix("--config=")
        if not value:
            continue
        if not value.startswith(f"{key}="):
            continue
        return value.split("=", 1)[1].strip().strip("\"'")
    return None


def _read_codex_config_value(key: str) -> str | None:
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.+?)\s*$")
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        return match.group(1).strip().strip("\"'")
    return None


def _format_context_remaining(token_info: dict[str, Any] | None) -> str | None:
    if not token_info:
        return None
    window = _coerce_int(token_info.get("model_context_window"))
    usage = token_info.get("last_token_usage")
    if not isinstance(usage, dict):
        usage = token_info.get("total_token_usage")
    used = _usage_total_tokens(usage) if isinstance(usage, dict) else None
    if window is None or used is None:
        return None
    remaining = max(0, window - used)
    percent = remaining / window * 100 if window else 0
    return f"{percent:.1f}% remaining"


def _format_rate_limit(rate_limits: dict[str, Any] | None, window_minutes: int) -> str | None:
    if not rate_limits:
        return None
    bucket = _rate_limit_bucket(rate_limits, window_minutes)
    if not bucket:
        return None
    used_percent = _coerce_float(bucket.get("used_percent"))
    resets_at = _coerce_int(bucket.get("resets_at"))
    parts: list[str] = []
    if used_percent is not None:
        remaining = max(0.0, 100.0 - used_percent)
        parts.append(f"{remaining:.1f}% remaining")
    if resets_at is not None:
        parts.append(f"resets {datetime.fromtimestamp(resets_at, UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    return ", ".join(parts) if parts else None


def _rate_limit_bucket(rate_limits: dict[str, Any], window_minutes: int) -> dict[str, Any] | None:
    for key in ("primary", "secondary"):
        value = rate_limits.get(key)
        if not isinstance(value, dict):
            continue
        if _coerce_int(value.get("window_minutes")) == window_minutes:
            return value
    return None


def _usage_total_tokens(usage: dict[str, Any]) -> int | None:
    explicit_total = _coerce_int(usage.get("total_tokens"))
    if explicit_total is not None:
        return explicit_total
    input_tokens = _coerce_int(usage.get("input_tokens"))
    output_tokens = _coerce_int(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _command_arg(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _append_conversation_log(
    path: Path,
    *,
    timestamp: datetime,
    user_prompt: str,
    command: str,
    codex_output: str,
) -> None:
    body = (
        f"[{timestamp.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S %Z')}]\n"
        "USER MESSAGE:\n"
        f"{user_prompt.rstrip()}\n"
        "COMMAND:\n"
        f"{command.rstrip()}\n"
        "CODEX OUTPUT:\n"
        f"{codex_output.rstrip()}\n"
        "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(body)


def _safe_history_log_stem(project_name: str) -> str:
    normalized = SAFE_HISTORY_FILENAME_RE.sub("_", project_name).strip().rstrip(".")
    if not normalized or normalized in {".", ".."}:
        return "unnamed-project"
    return normalized


def _build_document_prompt(filename: str, caption: str, document_text: str) -> str:
    sections: list[str] = []
    note = caption.strip()
    if note:
        sections.append(f"User explanation:\n{note}")
    sections.append(
        f"Attached text file: {filename}\n"
        "--- BEGIN ATTACHED FILE ---\n"
        f"{document_text.rstrip()}\n"
        "--- END ATTACHED FILE ---"
    )
    return "\n\n".join(sections)


def _decode_text_document(payload: bytes) -> str:
    try:
        text = payload.decode("utf-8-sig")
        if _is_supported_plain_text(text):
            return text
    except UnicodeDecodeError:
        pass
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = payload.decode("utf-16")
            if _is_supported_plain_text(text):
                return text
        except UnicodeDecodeError:
            pass
    if b"\x00" in payload:
        raise ValueError("Only plaintext files are supported.")
    raise ValueError("Only decodable plaintext files are supported. Use UTF-8 or UTF-16 text files.")


def _is_supported_plain_text(text: str) -> bool:
    return not any(char not in "\n\r\t" and ord(char) < 32 for char in text)


def _save_restart_request(path: Path, *, chat_id: int, requested_at: datetime) -> None:
    payload = {
        "chat_id": chat_id,
        "requested_at": requested_at.astimezone(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def _load_restart_request(path: Path) -> RestartRequest | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read restart request marker", extra={"path": str(path)})
        return None
    if not isinstance(payload, dict):
        return None
    chat_id = payload.get("chat_id")
    requested_at = payload.get("requested_at")
    if not isinstance(chat_id, int) or not isinstance(requested_at, str):
        return None
    return RestartRequest(chat_id=chat_id, requested_at=requested_at)


def _clear_restart_request(path: Path) -> None:
    with suppress(FileNotFoundError):
        path.unlink()


async def _progress_message_indicator(
    status_message: Message,
    stop_event: asyncio.Event,
    base_text: str = "Transcribing voice message",
) -> None:
    frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    idx = 0
    while not stop_event.is_set():
        with suppress(Exception):
            await status_message.edit_text(f"{base_text} {frames[idx % len(frames)]}")
        idx += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.45)
        except TimeoutError:
            continue
