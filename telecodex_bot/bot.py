from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
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

from telecodex_bot.config import Settings
from telecodex_bot.deepgram import DeepgramProviderError, DeepgramService, DeepgramServiceUnavailable
from telecodex_bot.repository import ChatState, Repository, SessionRecord
from telecodex_bot.runner import CodexRunner
from telecodex_bot.streaming import TelegramStreamEditor

logger = logging.getLogger(__name__)


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
            await callback_message.answer("Нет доступа")
            answer = getattr(event, "answer", None)
            if answer is not None:
                await answer()
            return
        await event.answer("Нет доступа")


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
        self.projects: dict[str, Path] = dict(self.settings.projects)
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
        for name, path in self.settings.projects.items():
            await self.repo.save_project(name, str(path))
        stored = await self.repo.list_projects()
        self.projects = {item.name: Path(item.project_path) for item in stored}

    @staticmethod
    def _bot_commands() -> list[BotCommand]:
        return [
            BotCommand(command="menu", description="Показать меню"),
            BotCommand(command="projects", description="Список проектов"),
            BotCommand(command="sessions", description="Список сессий"),
            BotCommand(command="status", description="Текущий статус"),
            BotCommand(command="cancel", description="Остановить задачу"),
            BotCommand(command="restart", description="Перезапустить сервис"),
        ]

    def _register_handlers(self) -> None:
        @self.router.message(Command("start", "menu"))
        async def start(message: Message) -> None:
            await self._send_menu(message)

        @self.router.message(Command("projects"))
        async def projects(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await message.answer("Доступные проекты:", reply_markup=self._project_keyboard())

        @self.router.message(Command("project"))
        async def set_project(message: Message) -> None:
            chat_id = message.chat.id
            self.pending_project_drafts.pop(chat_id, None)
            args = _command_arg(message.text)
            if not args:
                await message.answer("Выберите проект:", reply_markup=self._project_keyboard())
                return
            project_name = args.strip()
            if project_name not in self.projects:
                await message.answer("Неизвестный проект. Используйте кнопки или /projects.")
                return
            latest_session = await self._latest_session_for_project(project_name)
            await self.repo.set_chat_state(
                chat_id=chat_id,
                project_name=project_name,
                codex_session_id=latest_session.codex_session_id if latest_session else None,
            )
            if latest_session is None:
                await message.answer(
                    f"Проект: {project_name}\nСессий проекта пока нет. Следующий запуск начнет новую сессию.",
                    reply_markup=self._menu_keyboard(),
                )
                return
            await message.answer(
                f"Проект: {project_name}\nВыбрана последняя сессия проекта: <code>{html.escape(self._session_title(latest_session))}</code>",
                reply_markup=self._menu_keyboard(),
                parse_mode="HTML",
            )

        @self.router.message(Command("pwd"))
        async def pwd(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Проект не выбран. Используйте /menu.")
                return
            project_path = self.projects[state.project_name]
            await message.answer(f"{state.project_name}: {project_path}")

        @self.router.message(Command("sessions"))
        async def sessions(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            arg = _command_arg(message.text).strip()
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Сначала выберите проект через /menu.")
                return
            if not arg:
                items = await self.repo.list_sessions(state.project_name, self.settings.sessions_list_limit)
                if not items:
                    await message.answer(
                        f"Сессий проекта {state.project_name} пока нет.",
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
                    "Введенное сообщение/запрос начнет новую сессию Codex",
                    reply_markup=self._menu_keyboard(),
                )
                return
            selected = await self.repo.get_session(arg)
            if not selected or selected.project_name != state.project_name:
                await message.answer("Сессия не найдена в текущем проекте.")
                return
            await self.repo.set_chat_state(message.chat.id, state.project_name, selected.codex_session_id)
            await message.answer(
                f"Выбрана сессия: <code>{html.escape(self._session_title(selected))}</code>",
                reply_markup=self._menu_keyboard(),
                parse_mode="HTML",
            )

        @self.router.message(Command("session_name"))
        async def session_name(message: Message) -> None:
            alias = _command_arg(message.text)
            if not alias:
                await message.answer("Использование: /session_name <alias>")
                return
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.codex_session_id:
                await message.answer("Сначала выберите сессию.")
                return
            updated = await self.repo.rename_session(state.codex_session_id, alias.strip())
            if not updated:
                await message.answer("Не удалось обновить alias.")
                return
            await message.answer("Название сессии обновлено.", reply_markup=self._menu_keyboard())

        @self.router.message(Command("whereami", "status"))
        async def whereami(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await message.answer(await self._state_card(message.chat.id), reply_markup=self._menu_keyboard(), parse_mode="HTML")

        @self.router.message(Command("cancel"))
        async def cancel(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            run = self.active_runs.get(message.chat.id)
            if run is None:
                await message.answer("Активной задачи нет.")
                return
            run.cancel_event.set()
            await message.answer("Отмена запрошена.", reply_markup=self._menu_keyboard())

        @self.router.message(Command("restart"))
        async def restart(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await self._handle_restart(message)

        @self.router.message(F.voice)
        async def run_voice(message: Message) -> None:
            self.pending_project_drafts.pop(message.chat.id, None)
            await self._handle_voice_message(message)

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
            await self._edit_callback_message(callback, "Выберите проект:", self._project_keyboard())
            await callback.answer()

        @self.router.callback_query(F.data == "project:new")
        async def project_new(callback: CallbackQuery) -> None:
            self.pending_project_drafts[callback.message.chat.id] = PendingProjectDraft()
            await self._edit_callback_message(
                callback,
                "Введите название нового проекта:",
                self._project_creation_keyboard(),
            )
            await callback.answer()

        @self.router.callback_query(F.data == "project:new:cancel")
        async def project_new_cancel(callback: CallbackQuery) -> None:
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            await self._edit_callback_message(callback, "Выберите проект:", self._project_keyboard())
            await callback.answer("Создание проекта отменено.")

        @self.router.callback_query(F.data == "project:new:path:up")
        async def project_new_path_up(callback: CallbackQuery) -> None:
            draft = self.pending_project_drafts.get(callback.message.chat.id)
            if draft is None or draft.name is None:
                await callback.answer("Сначала введите название проекта.", show_alert=True)
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
                await callback.answer("Сначала введите название проекта.", show_alert=True)
                return
            index_text = callback.data.removeprefix("project:new:path:open:")
            try:
                index = int(index_text)
            except ValueError:
                await callback.answer("Папка не найдена.", show_alert=True)
                return
            entries = self._project_browser_entries(draft.current_path)
            if index < 0 or index >= len(entries):
                await callback.answer("Папка не найдена.", show_alert=True)
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
                await callback.answer("Сначала введите название проекта.", show_alert=True)
                return
            await self._complete_project_creation(callback.message.chat.id, draft)
            await self._edit_callback_message(
                callback,
                f"Проект создан: {draft.name}\nПуть: {draft.current_path}\nСледующий запуск начнет новую сессию.",
                self._menu_keyboard(),
            )
            await callback.answer("Проект создан.")

        @self.router.callback_query(F.data.startswith("project:set:"))
        async def project_set(callback: CallbackQuery) -> None:
            project_name = callback.data.removeprefix("project:set:")
            self.pending_project_drafts.pop(callback.message.chat.id, None)
            if project_name not in self.projects:
                await callback.answer("Проект не найден.", show_alert=True)
                return
            latest_session = await self._latest_session_for_project(project_name)
            await self.repo.set_chat_state(
                callback.message.chat.id,
                project_name,
                latest_session.codex_session_id if latest_session else None,
            )
            if latest_session is None:
                await self._edit_callback_message(
                    callback,
                    f"Проект переключен на {project_name}.\nСессий проекта пока нет. Следующий запуск начнет новую сессию.",
                    self._menu_keyboard(),
                )
            else:
                await self._edit_callback_message(
                    callback,
                    (
                        f"Проект переключен на {project_name}.\n"
                        f"Выбрана последняя сессия проекта: <code>{html.escape(self._session_title(latest_session))}</code>"
                    ),
                    self._menu_keyboard(),
                    parse_mode="HTML",
                )
            await callback.answer("Проект обновлен.")

        @self.router.callback_query(F.data == "session:list")
        async def session_list(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            await self._show_session_list(callback, state.project_name, state.codex_session_id)
            await callback.answer()

        @self.router.callback_query(F.data == "session:new")
        async def session_new(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            await self.repo.set_chat_state(callback.message.chat.id, state.project_name, None)
            await self._edit_callback_message(
                callback,
                "Введенное сообщение/запрос начнет новую сессию Codex",
                self._menu_keyboard(),
            )
            await callback.answer("Будет создана новая сессия.")

        @self.router.callback_query(F.data == "session:delete:list")
        async def session_delete_list(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            await self._show_session_delete_list(callback, state.project_name)
            await callback.answer()

        @self.router.callback_query(F.data.startswith("session:delete:confirm:"))
        async def session_delete_confirm(callback: CallbackQuery) -> None:
            codex_session_id = callback.data.removeprefix("session:delete:confirm:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            session_item = await self.repo.get_session(codex_session_id)
            if not session_item or session_item.project_name != state.project_name:
                await callback.answer("Сессия не найдена.", show_alert=True)
                return
            await self._edit_callback_message(
                callback,
                "Удалить сессию?\n" f"<code>{html.escape(self._session_title(session_item))}</code>",
                self._session_delete_confirm_keyboard(session_item.codex_session_id),
                parse_mode="HTML",
            )
            await callback.answer()

        @self.router.callback_query(F.data.startswith("session:delete:yes:"))
        async def session_delete_yes(callback: CallbackQuery) -> None:
            codex_session_id = callback.data.removeprefix("session:delete:yes:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            deleted = await self.repo.delete_session(codex_session_id)
            if not deleted:
                await callback.answer("Сессия не найдена.", show_alert=True)
                return
            await self._show_session_delete_list(callback, state.project_name)
            await callback.answer("Сессия удалена.")

        @self.router.callback_query(F.data == "session:delete:no")
        async def session_delete_no(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            await self._show_session_delete_list(callback, state.project_name)
            await callback.answer("Удаление отменено.")

        @self.router.callback_query(F.data.startswith("session:set:"))
        async def session_set(callback: CallbackQuery) -> None:
            codex_session_id = callback.data.removeprefix("session:set:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            selected = await self.repo.get_session(codex_session_id)
            if not selected or selected.project_name != state.project_name:
                await callback.answer("Сессия не найдена.", show_alert=True)
                return
            await self.repo.set_chat_state(callback.message.chat.id, state.project_name, selected.codex_session_id)
            await self._edit_callback_message(
                callback,
                f"Выбрана сессия: <code>{html.escape(self._session_title(selected))}</code>",
                self._menu_keyboard(),
                parse_mode="HTML",
            )
            await callback.answer("Сессия обновлена.")

        @self.router.callback_query(F.data == "action:stop")
        async def action_stop(callback: CallbackQuery) -> None:
            run = self.active_runs.get(callback.message.chat.id)
            if run is None:
                await callback.answer("Активной задачи нет.")
                return
            run.cancel_event.set()
            await callback.answer("Отмена запрошена.")

        @self.router.callback_query(F.data == "help:show")
        async def help_show(callback: CallbackQuery) -> None:
            text = (
                "Как пользоваться:\n"
                "1. Выберите проект.\n"
                "2. Выберите сохраненную сессию или начните новую.\n"
                "3. Отправьте задачу обычным сообщением.\n\n"
                "Команды как fallback: /project, /sessions, /cancel, /restart."
            )
            await self._edit_callback_message(callback, text, self._menu_keyboard())
            await callback.answer()

    async def _execute_prompt(self, message: Message, prompt: str) -> None:
        chat_id = message.chat.id
        if chat_id in self.active_runs:
            await message.answer("В этом чате уже выполняется задача. Используйте /status или кнопку Стоп.")
            return

        state = await self.repo.get_chat_state(chat_id)
        if not state or not state.project_name:
            await message.answer("Сначала выберите проект через /menu.")
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
        await stream.start("Telecodex thinking...")
        cancel_event = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_loop(chat_id, cancel_event))
        self.active_runs[chat_id] = ActiveRun(
            started_at=asyncio.get_running_loop().time(),
            project_name=state.project_name,
            codex_session_id=current_session.codex_session_id if current_session else None,
            cancel_event=cancel_event,
        )

        try:
            result = await self.runner.run(
                project_path=str(self.projects[state.project_name]),
                codex_session_id=current_session.codex_session_id if current_session else None,
                user_prompt=prompt,
                on_progress=stream.publish_status,
                on_message=stream.publish_answer,
                cancel_event=cancel_event,
            )
        finally:
            cancel_event.set()
            await typing_task
            self.active_runs.pop(chat_id, None)

        _append_conversation_log(
            self._conversation_log_path(telegram_user_id),
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
            final_text = assistant_text or "Запуск отменен."
        elif result.timed_out:
            summary = "failed: timeout"
            final_text = assistant_text or "Codex не успел вернуть ответ до таймаута."
        elif result.success:
            summary = "done"
            final_text = assistant_text or "Ответ пуст."
        else:
            summary = f"failed: code={result.return_code}"
            final_text = assistant_text or "Codex завершился с ошибкой без текстового ответа."

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
            await message.answer("Голосовые сообщения недоступны: не настроен DEEPGRAM_API_KEY.")
            return

        try:
            voice_bytes = await self._download_voice_bytes(message)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        except Exception:
            logger.exception("Voice download failed", extra={"chat_id": message.chat.id})
            await message.answer("Ошибка при загрузке голосового сообщения.")
            return

        status_message = await message.answer("Распознаю голосовое сообщение ⠋")
        stop_event = asyncio.Event()
        indicator_task = asyncio.create_task(_progress_message_indicator(status_message, stop_event))
        try:
            transcript = await self.deepgram.transcribe_ogg_opus(voice_bytes)
        except DeepgramServiceUnavailable:
            await message.answer("Deepgram временно недоступен. Попробуйте позже.")
            return
        except DeepgramProviderError as exc:
            await message.answer(f"Ошибка распознавания: {exc}")
            return
        except Exception:
            logger.exception("Deepgram unexpected error", extra={"chat_id": message.chat.id})
            await message.answer("Непредвиденная ошибка распознавания голоса.")
            return
        finally:
            stop_event.set()
            with suppress(Exception):
                await indicator_task
            with suppress(Exception):
                await status_message.delete()

        await message.answer(transcript)
        await self._execute_prompt(message, transcript)

    async def _download_voice_bytes(self, message: Message) -> bytes:
        if not message.voice:
            raise ValueError("Голосовое сообщение не найдено.")
        file = await message.bot.get_file(message.voice.file_id)
        if not file.file_path:
            raise ValueError("Не удалось получить голосовой файл.")
        buffer = io.BytesIO()
        await message.bot.download_file(file.file_path, destination=buffer)
        voice_bytes = buffer.getvalue()
        if not voice_bytes:
            raise ValueError("Не удалось скачать голосовое сообщение.")
        return voice_bytes

    async def _get_selected_session(self, state: ChatState) -> SessionRecord | None:
        if not state.codex_session_id:
            return None
        session = await self.repo.get_session(state.codex_session_id)
        if session:
            return session
        await self.repo.set_chat_state(state.chat_id, state.project_name, None)
        return None

    def _conversation_log_path(self, telegram_user_id: int) -> Path:
        return self.settings.history_dir / f"conversation{telegram_user_id}.log"

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
            await message.answer("Введите непустое значение.", reply_markup=self._project_creation_keyboard())
            return
        if draft.name is None:
            if user_text in self.projects:
                await message.answer("Проект с таким названием уже существует. Введите другое название.", reply_markup=self._project_creation_keyboard())
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
            "Путь проекта выбирается кнопками ниже.",
            reply_markup=self._project_path_browser_keyboard(draft),
        )

    async def _handle_restart(self, message: Message) -> None:
        chat_id = message.chat.id
        if not self._is_admin_chat(chat_id):
            await message.answer("Команда недоступна.")
            return
        if self.active_runs:
            await message.answer("Есть активные задачи. Сначала дождитесь завершения или выполните /cancel.")
            return
        logger.warning("Restart requested by admin", extra={"chat_id": chat_id})
        _save_restart_request(self._restart_marker_path(), chat_id=chat_id, requested_at=datetime.now(UTC))
        await message.answer("Перезапуск сервиса запрошен.")
        asyncio.create_task(self.restart_callback())

    async def notify_restart_success_if_needed(self) -> None:
        request = _load_restart_request(self._restart_marker_path())
        if request is None:
            return
        try:
            await self.bot.send_message(request.chat_id, "Сервис был перезапущен успешно.")
        except Exception:
            logger.exception("Failed to send restart success notification", extra={"chat_id": request.chat_id})
            return
        _clear_restart_request(self._restart_marker_path())

    async def _state_card(self, chat_id: int) -> str:
        state = await self.repo.get_chat_state(chat_id)
        run = self.active_runs.get(chat_id)
        project = state.project_name if state and state.project_name else "не выбран"
        project_path = str(self.projects[state.project_name]) if state and state.project_name else "-"
        session_text = "не выбрана"
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
            f"Проект: {html.escape(project)}\n"
            f"Путь: {html.escape(project_path)}\n"
            f"Сессия: {self._session_text_html(session_text)}\n"
            f"Статус: {html.escape(status)}\n"
            f"Последняя активность: {html.escape(last_seen)}"
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
        builder.button(text="Проект", callback_data="project:list")
        builder.button(text="Сессия", callback_data="session:list")
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="Стоп", callback_data="action:stop")
        builder.button(text="Помощь", callback_data="help:show")
        builder.adjust(2, 2, 1)
        return builder.as_markup()

    def _project_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for name, path in self.projects.items():
            builder.button(text=f"{name} ({path})", callback_data=f"project:set:{name}")
        builder.button(text="Новый проект", callback_data="project:new")
        builder.button(text="Назад", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _project_creation_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Назад", callback_data="project:new:cancel")
        builder.adjust(1)
        return builder.as_markup()

    def _project_path_browser_keyboard(self, draft: PendingProjectDraft) -> InlineKeyboardMarkup:
        entries = self._project_browser_entries(draft.current_path)
        draft.entries = entries
        rows: list[list[InlineKeyboardButton]] = []
        if draft.current_path != draft.current_path.parent:
            rows.append([InlineKeyboardButton(text="⬆️ ..", callback_data="project:new:path:up")])
        rows.extend(self._project_browser_folder_rows(entries))
        rows.append([InlineKeyboardButton(text=f"✅ {draft.current_path}", callback_data="project:new:path:select")])
        rows.append([InlineKeyboardButton(text="Назад", callback_data="project:new:cancel")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def _project_path_browser_text(self, draft: PendingProjectDraft) -> str:
        return (
            f"Название проекта: {html.escape(draft.name or '-')}\n"
            "Выберите корневую папку проекта:\n"
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

    def _session_keyboard(self, sessions: list[SessionRecord]) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for item in sessions:
            builder.button(text=self._session_title(item), callback_data=f"session:set:{item.codex_session_id}")
        builder.button(text="Удалить сессию", callback_data="session:delete:list")
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="Назад", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _session_delete_keyboard(self, sessions: list[SessionRecord]) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for item in sessions:
            builder.button(
                text=f"❌ {self._session_title(item)}",
                callback_data=f"session:delete:confirm:{item.codex_session_id}",
            )
        builder.button(text="Назад", callback_data="session:list")
        builder.adjust(1)
        return builder.as_markup()

    def _session_delete_confirm_keyboard(self, codex_session_id: str) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Да", callback_data=f"session:delete:yes:{codex_session_id}")
        builder.button(text="Нет", callback_data="session:delete:no")
        builder.adjust(2)
        return builder.as_markup()

    def _session_delete_empty_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Назад", callback_data="session:list")
        builder.adjust(1)
        return builder.as_markup()

    def _new_session_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="Назад", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _result_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="Сменить проект", callback_data="project:list")
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
        return f"Сессии проекта {project_name}:"

    @staticmethod
    def _delete_sessions_title(project_name: str) -> str:
        return f"Удаление сессий проекта {project_name}:"

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
                f"Сессий проекта {project_name} пока нет.",
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
                f"Сессий проекта {project_name} пока нет.",
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
        if value == "не выбрана":
            return value
        return f"<code>{html.escape(value)}</code>"


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
    base_text: str = "Распознаю голосовое сообщение",
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
