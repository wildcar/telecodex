from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from telecodex_bot.config import Settings
from telecodex_bot.repository import ChatState, Repository, SessionRecord
from telecodex_bot.runner import CodexRunner
from telecodex_bot.streaming import TelegramStreamEditor

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ActiveRun:
    started_at: float
    project_name: str
    session_id: str
    cancel_event: asyncio.Event


class TelecodexApplication:
    def __init__(self, bot: Bot, dispatcher: Dispatcher, repo: Repository, runner: CodexRunner, settings: Settings) -> None:
        self.bot = bot
        self.dispatcher = dispatcher
        self.repo = repo
        self.runner = runner
        self.settings = settings
        self.active_runs: dict[int, ActiveRun] = {}
        self.router = Router()
        self._register_handlers()
        self.dispatcher.include_router(self.router)

    def _register_handlers(self) -> None:
        @self.router.message(Command("start", "menu"))
        async def start(message: Message) -> None:
            await self._send_menu(message)

        @self.router.message(Command("projects"))
        async def projects(message: Message) -> None:
            await message.answer(
                "Доступные проекты:\n" + "\n".join(f"• {name}" for name in self.settings.projects),
                reply_markup=self._project_keyboard(),
            )

        @self.router.message(Command("project"))
        async def set_project(message: Message) -> None:
            chat_id = message.chat.id
            args = _command_arg(message.text)
            if not args:
                await message.answer("Выберите проект:", reply_markup=self._project_keyboard())
                return
            project_name = args.strip()
            if project_name not in self.settings.projects:
                await message.answer("Неизвестный проект. Используйте кнопки или /projects.")
                return
            await self.repo.set_chat_state(chat_id=chat_id, project_name=project_name, session_id=None)
            await message.answer(
                f"Проект: {project_name}\nСессия сброшена.",
                reply_markup=self._menu_keyboard(),
            )

        @self.router.message(Command("pwd"))
        async def pwd(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Проект не выбран. Используйте /menu.")
                return
            project_path = self.settings.projects[state.project_name]
            await message.answer(f"{state.project_name}: {project_path}")

        @self.router.message(Command("sessions"))
        async def sessions(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Сначала выберите проект через /menu.")
                return
            items = await self.repo.list_sessions(state.project_name, self.settings.sessions_list_limit)
            if not items:
                await message.answer("Сессий пока нет.", reply_markup=self._new_session_keyboard())
                return
            lines = [self._format_session_line(item, state.session_id) for item in items]
            await message.answer("Сессии:\n" + "\n".join(lines), reply_markup=self._session_keyboard(items))

        @self.router.message(Command("session"))
        async def session(message: Message) -> None:
            chat_id = message.chat.id
            arg = _command_arg(message.text)
            state = await self.repo.get_chat_state(chat_id)
            if not state or not state.project_name:
                await message.answer("Сначала выберите проект через /menu.")
                return
            if not arg:
                items = await self.repo.list_sessions(state.project_name, self.settings.sessions_list_limit)
                await message.answer("Выберите сессию:", reply_markup=self._session_keyboard(items))
                return
            if arg == "new":
                created = await self._create_session(state.project_name)
                await self.repo.set_chat_state(chat_id, state.project_name, created.id)
                await message.answer(
                    f"Создана сессия: {self._session_title(created)}",
                    reply_markup=self._menu_keyboard(),
                )
                return
            selected = await self.repo.get_session(arg)
            if not selected or selected.project_name != state.project_name:
                await message.answer("Сессия не найдена в текущем проекте.")
                return
            await self.repo.set_chat_state(chat_id, state.project_name, selected.id)
            await message.answer(
                f"Выбрана сессия: {self._session_title(selected)}",
                reply_markup=self._menu_keyboard(),
            )

        @self.router.message(Command("session_name"))
        async def session_name(message: Message) -> None:
            alias = _command_arg(message.text)
            if not alias:
                await message.answer("Использование: /session_name <alias>")
                return
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.session_id:
                await message.answer("Сначала выберите сессию.")
                return
            updated = await self.repo.rename_session(state.session_id, alias.strip())
            if not updated:
                await message.answer("Не удалось обновить alias.")
                return
            await message.answer("Название сессии обновлено.", reply_markup=self._menu_keyboard())

        @self.router.message(Command("whereami", "status"))
        async def whereami(message: Message) -> None:
            await message.answer(await self._state_card(message.chat.id), reply_markup=self._menu_keyboard())

        @self.router.message(Command("cancel"))
        async def cancel(message: Message) -> None:
            run = self.active_runs.get(message.chat.id)
            if run is None:
                await message.answer("Активной задачи нет.")
                return
            run.cancel_event.set()
            await message.answer("Отмена запрошена.", reply_markup=self._menu_keyboard())

        @self.router.message(Command("last"))
        async def last(message: Message) -> None:
            await self._send_last_log(message)

        @self.router.message(Command("run"))
        async def run_cmd(message: Message) -> None:
            prompt = _command_arg(message.text)
            if not prompt:
                await message.answer("Использование: /run <task>")
                return
            await self._execute_prompt(message, prompt)

        @self.router.message(F.text & ~F.text.startswith("/"))
        async def run_text(message: Message) -> None:
            await self._execute_prompt(message, message.text or "")

        @self.router.callback_query(F.data == "menu:root")
        async def menu_root(callback: CallbackQuery) -> None:
            await self._edit_callback_message(callback, await self._state_card(callback.message.chat.id), self._menu_keyboard())
            await callback.answer()

        @self.router.callback_query(F.data == "project:list")
        async def project_list(callback: CallbackQuery) -> None:
            await self._edit_callback_message(callback, "Выберите проект:", self._project_keyboard())
            await callback.answer()

        @self.router.callback_query(F.data.startswith("project:set:"))
        async def project_set(callback: CallbackQuery) -> None:
            project_name = callback.data.removeprefix("project:set:")
            if project_name not in self.settings.projects:
                await callback.answer("Проект не найден.", show_alert=True)
                return
            await self.repo.set_chat_state(callback.message.chat.id, project_name, None)
            await self._edit_callback_message(
                callback,
                f"Проект переключен на {project_name}.\nСессия сброшена.",
                self._menu_keyboard(),
            )
            await callback.answer("Проект обновлен.")

        @self.router.callback_query(F.data == "session:list")
        async def session_list(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            items = await self.repo.list_sessions(state.project_name, self.settings.sessions_list_limit)
            if not items:
                await self._edit_callback_message(callback, "Сессий пока нет.", self._new_session_keyboard())
                await callback.answer()
                return
            lines = [self._format_session_line(item, state.session_id) for item in items]
            await self._edit_callback_message(callback, "Сессии:\n" + "\n".join(lines), self._session_keyboard(items))
            await callback.answer()

        @self.router.callback_query(F.data == "session:new")
        async def session_new(callback: CallbackQuery) -> None:
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            created = await self._create_session(state.project_name)
            await self.repo.set_chat_state(callback.message.chat.id, state.project_name, created.id)
            await self._edit_callback_message(
                callback,
                f"Новая сессия готова: {self._session_title(created)}",
                self._menu_keyboard(),
            )
            await callback.answer("Сессия создана.")

        @self.router.callback_query(F.data.startswith("session:set:"))
        async def session_set(callback: CallbackQuery) -> None:
            session_id = callback.data.removeprefix("session:set:")
            state = await self.repo.get_chat_state(callback.message.chat.id)
            if not state or not state.project_name:
                await callback.answer("Сначала выберите проект.", show_alert=True)
                return
            selected = await self.repo.get_session(session_id)
            if not selected or selected.project_name != state.project_name:
                await callback.answer("Сессия не найдена.", show_alert=True)
                return
            await self.repo.set_chat_state(callback.message.chat.id, state.project_name, selected.id)
            await self._edit_callback_message(
                callback,
                f"Выбрана сессия: {self._session_title(selected)}",
                self._menu_keyboard(),
            )
            await callback.answer("Сессия обновлена.")

        @self.router.callback_query(F.data == "action:continue")
        async def action_continue(callback: CallbackQuery) -> None:
            await callback.answer("Отправьте следующее сообщение в этот чат.")

        @self.router.callback_query(F.data == "action:stop")
        async def action_stop(callback: CallbackQuery) -> None:
            run = self.active_runs.get(callback.message.chat.id)
            if run is None:
                await callback.answer("Активной задачи нет.")
                return
            run.cancel_event.set()
            await callback.answer("Отмена запрошена.")

        @self.router.callback_query(F.data == "action:log")
        async def action_log(callback: CallbackQuery) -> None:
            if callback.message is not None:
                await self._send_last_log(callback.message)
            await callback.answer()

        @self.router.callback_query(F.data == "help:show")
        async def help_show(callback: CallbackQuery) -> None:
            text = (
                "Как пользоваться:\n"
                "1. Выберите проект.\n"
                "2. Выберите или создайте сессию.\n"
                "3. Отправьте задачу обычным сообщением.\n\n"
                "Команды как fallback: /project, /session, /run, /cancel, /last."
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

        session_item = await self._ensure_session(state)

        stream = TelegramStreamEditor(
            bot=self.bot,
            chat_id=chat_id,
            interval_sec=self.settings.stream_update_interval_sec,
            tail_chars=self.settings.stream_tail_chars,
            send_log_threshold=self.settings.stream_send_log_threshold,
        )
        await stream.start("Running...")

        cancel_event = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_loop(chat_id, cancel_event))
        self.active_runs[chat_id] = ActiveRun(
            started_at=asyncio.get_running_loop().time(),
            project_name=state.project_name,
            session_id=session_item.id,
            cancel_event=cancel_event,
        )

        recent_history = await self.repo.get_recent_history(session_item.id, self.settings.session_history_items)
        try:
            result = await self.runner.run(
                session=session_item,
                user_prompt=prompt,
                recent_history=recent_history,
                on_output=lambda chunk: self._on_output(session_item, chunk),
                cancel_event=cancel_event,
            )
        finally:
            cancel_event.set()
            await typing_task
            self.active_runs.pop(chat_id, None)

        await self.repo.add_history(session_item.id, "user", prompt)
        assistant_text = (result.assistant_text or result.display_text or result.output).strip()
        await self.repo.add_history(session_item.id, "assistant", assistant_text[-10000:])
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
            full_text=result.output,
            reply_markup=self._result_keyboard(),
        )

    async def _create_session(self, project_name: str) -> SessionRecord:
        project_path = self.settings.projects[project_name]
        session = await self.repo.create_session(
            project_name=project_name,
            project_path=str(project_path),
            history_log_path=str(self.settings.history_dir / f"{project_name}_{uuid.uuid4()}.log"),
        )
        return session

    async def _ensure_session(self, state: ChatState) -> SessionRecord:
        if state.session_id:
            session = await self.repo.get_session(state.session_id)
            if session:
                return session
        created = await self._create_session(state.project_name)  # type: ignore[arg-type]
        await self.repo.set_chat_state(state.chat_id, state.project_name, created.id)
        return created

    async def _on_output(self, session_item: SessionRecord, chunk: str) -> None:
        _append_log(Path(session_item.history_log_path), chunk)

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

    async def _send_menu(self, message: Message) -> None:
        await message.answer(await self._state_card(message.chat.id), reply_markup=self._menu_keyboard())

    async def _state_card(self, chat_id: int) -> str:
        state = await self.repo.get_chat_state(chat_id)
        run = self.active_runs.get(chat_id)
        project = state.project_name if state and state.project_name else "не выбран"
        session_text = "не выбрана"
        updated_at = state.updated_at if state else ""
        if state and state.session_id:
            session_item = await self.repo.get_session(state.session_id)
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
            f"Проект: {project}\n"
            f"Сессия: {session_text}\n"
            f"Статус: {status}\n"
            f"Последняя активность: {last_seen}"
        )

    async def _send_last_log(self, message: Message) -> None:
        state = await self.repo.get_chat_state(message.chat.id)
        if not state or not state.session_id:
            await message.answer("Сессия не выбрана.")
            return
        session_item = await self.repo.get_session(state.session_id)
        if not session_item:
            await message.answer("Сессия не найдена.")
            return
        output = _tail_file(Path(session_item.history_log_path), max_lines=60)
        if not output:
            await message.answer("Лог пуст.")
            return
        await message.answer(output[-3900:])

    async def _edit_callback_message(
        self,
        callback: CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if callback.message is None:
            return
        await callback.message.edit_text(text, reply_markup=reply_markup)

    def _menu_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        builder.button(text="Проект", callback_data="project:list")
        builder.button(text="Сессия", callback_data="session:list")
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="История", callback_data="action:log")
        builder.button(text="Стоп", callback_data="action:stop")
        builder.button(text="Помощь", callback_data="help:show")
        builder.adjust(2, 2, 2)
        return builder.as_markup()

    def _project_keyboard(self) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for name in self.settings.projects:
            builder.button(text=name, callback_data=f"project:set:{name}")
        builder.button(text="Назад", callback_data="menu:root")
        builder.adjust(1)
        return builder.as_markup()

    def _session_keyboard(self, sessions: list[SessionRecord]) -> InlineKeyboardMarkup:
        builder = InlineKeyboardBuilder()
        for item in sessions:
            builder.button(text=self._session_title(item), callback_data=f"session:set:{item.id}")
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="Назад", callback_data="menu:root")
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
        builder.button(text="Продолжить", callback_data="action:continue")
        builder.button(text="Новая сессия", callback_data="session:new")
        builder.button(text="Сменить проект", callback_data="project:list")
        builder.button(text="Лог", callback_data="action:log")
        builder.adjust(2, 2)
        return builder.as_markup()

    @staticmethod
    def _session_title(session: SessionRecord) -> str:
        return session.alias or f"Сессия {session.id[:8]}"

    def _format_session_line(self, session: SessionRecord, active_session_id: str | None) -> str:
        marker = "•"
        if session.id == active_session_id:
            marker = "→"
        return f"{marker} {self._session_title(session)} · {self._format_timestamp(session.updated_at)}"

    @staticmethod
    def _format_timestamp(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value


def _command_arg(text: str | None) -> str:
    if not text:
        return ""
    parts = text.split(maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _append_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(text)


def _tail_file(path: Path, max_lines: int) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as fp:
        lines = fp.readlines()
    return "".join(lines[-max_lines:])
