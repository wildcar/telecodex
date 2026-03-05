from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message

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
        @self.router.message(Command("start"))
        async def start(message: Message) -> None:
            await message.answer(
                "Telecodex bot готов. Используйте /projects, /project <name>, /session new, /run <task>."
            )

        @self.router.message(Command("projects"))
        async def projects(message: Message) -> None:
            lines = [f"{name}: {path}" for name, path in self.settings.projects.items()]
            await message.answer("Доступные проекты:\n" + "\n".join(lines))

        @self.router.message(Command("project"))
        async def set_project(message: Message) -> None:
            chat_id = message.chat.id
            args = _command_arg(message.text)
            if not args:
                await message.answer("Использование: /project <name>")
                return
            project_name = args.strip()
            if project_name not in self.settings.projects:
                await message.answer("Неизвестный проект. Смотрите /projects")
                return
            await self.repo.set_chat_state(chat_id=chat_id, project_name=project_name, session_id=None)
            await message.answer(f"Выбран проект: {project_name}. Сессия сброшена, выберите /session new")

        @self.router.message(Command("pwd"))
        async def pwd(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Проект не выбран. Используйте /project <name>")
                return
            project_path = self.settings.projects[state.project_name]
            await message.answer(f"{state.project_name}: {project_path}")

        @self.router.message(Command("sessions"))
        async def sessions(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.project_name:
                await message.answer("Сначала выберите проект через /project <name>")
                return
            items = await self.repo.list_sessions(state.project_name, self.settings.sessions_list_limit)
            if not items:
                await message.answer("Сессий пока нет. Используйте /session new")
                return
            lines = [f"{s.id} | {s.alias or '-'} | {s.updated_at}" for s in items]
            await message.answer("Сессии:\n" + "\n".join(lines))

        @self.router.message(Command("session"))
        async def session(message: Message) -> None:
            chat_id = message.chat.id
            arg = _command_arg(message.text)
            if not arg:
                await message.answer("Использование: /session <id|new>")
                return
            state = await self.repo.get_chat_state(chat_id)
            if not state or not state.project_name:
                await message.answer("Сначала выберите проект через /project <name>")
                return
            if arg == "new":
                created = await self._create_session(state.project_name)
                await self.repo.set_chat_state(chat_id, state.project_name, created.id)
                await message.answer(f"Создана сессия: {created.id}")
                return
            selected = await self.repo.get_session(arg)
            if not selected or selected.project_name != state.project_name:
                await message.answer("Сессия не найдена в текущем проекте")
                return
            await self.repo.set_chat_state(chat_id, state.project_name, selected.id)
            await message.answer(f"Выбрана сессия: {selected.id}")

        @self.router.message(Command("session_name"))
        async def session_name(message: Message) -> None:
            alias = _command_arg(message.text)
            if not alias:
                await message.answer("Использование: /session_name <alias>")
                return
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.session_id:
                await message.answer("Сначала выберите сессию")
                return
            updated = await self.repo.rename_session(state.session_id, alias.strip())
            if not updated:
                await message.answer("Не удалось обновить alias")
                return
            await message.answer("Alias обновлен")

        @self.router.message(Command("whereami"))
        async def whereami(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state:
                await message.answer("Состояние не найдено. Выберите /project")
                return
            alias = "-"
            if state.session_id:
                session_item = await self.repo.get_session(state.session_id)
                alias = session_item.alias if session_item and session_item.alias else "-"
            await message.answer(
                f"project={state.project_name or '-'}\nsession={state.session_id or '-'}\nalias={alias}"
            )

        @self.router.message(Command("status"))
        async def status(message: Message) -> None:
            run = self.active_runs.get(message.chat.id)
            if run is None:
                await message.answer("Активной задачи нет")
                return
            elapsed = int(asyncio.get_running_loop().time() - run.started_at)
            await message.answer(
                f"Идет задача: {elapsed}s\nproject={run.project_name}\nsession={run.session_id}"
            )

        @self.router.message(Command("cancel"))
        async def cancel(message: Message) -> None:
            run = self.active_runs.get(message.chat.id)
            if run is None:
                await message.answer("Активной задачи нет")
                return
            run.cancel_event.set()
            await message.answer("Отмена запрошена")

        @self.router.message(Command("last"))
        async def last(message: Message) -> None:
            state = await self.repo.get_chat_state(message.chat.id)
            if not state or not state.session_id:
                await message.answer("Сессия не выбрана")
                return
            session_item = await self.repo.get_session(state.session_id)
            if not session_item:
                await message.answer("Сессия не найдена")
                return
            output = _tail_file(Path(session_item.history_log_path), max_lines=60)
            if not output:
                await message.answer("Лог пуст")
                return
            await message.answer(output[-3900:])

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

    async def _execute_prompt(self, message: Message, prompt: str) -> None:
        chat_id = message.chat.id
        if chat_id in self.active_runs:
            await message.answer("В этом чате уже выполняется задача. Используйте /status или /cancel")
            return

        state = await self.repo.get_chat_state(chat_id)
        if not state or not state.project_name:
            await message.answer("Сначала выберите проект через /project <name>")
            return

        session_item = await self._ensure_session(state)

        stream = TelegramStreamEditor(
            bot=self.bot,
            chat_id=chat_id,
            interval_sec=self.settings.stream_update_interval_sec,
            tail_chars=self.settings.stream_tail_chars,
            send_log_threshold=self.settings.stream_send_log_threshold,
        )
        await stream.start(f"project={state.project_name} session={session_item.id}")

        cancel_event = asyncio.Event()
        self.active_runs[chat_id] = ActiveRun(
            started_at=asyncio.get_running_loop().time(),
            project_name=state.project_name,
            session_id=session_item.id,
            cancel_event=cancel_event,
        )

        recent_history = await self.repo.get_recent_history(session_item.id, self.settings.session_history_items)
        await self.repo.add_history(session_item.id, "user", prompt)
        try:
            result = await self.runner.run(
                session=session_item,
                user_prompt=prompt,
                recent_history=recent_history,
                on_output=lambda chunk: self._on_output(session_item, stream, chunk),
                cancel_event=cancel_event,
            )
        finally:
            self.active_runs.pop(chat_id, None)

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

        await stream.finish(result.success, summary, final_text=final_text)

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

    async def _on_output(self, session_item: SessionRecord, stream: TelegramStreamEditor, chunk: str) -> None:
        _append_log(Path(session_item.history_log_path), chunk)
        await stream.push(chunk)



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
