from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest
from aiogram import Bot, Dispatcher

from telecodex_bot.bot import ActiveRun, TelecodexApplication
from telecodex_bot.config import Settings
from telecodex_bot.repository import Repository, SessionRecord
from telecodex_bot.runner import CodexRunner


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="123:ABC",
        TELECODEX_PROJECTS_JSON='{"demo":"/tmp/demo","infra":"/tmp/infra"}',
        DB_PATH=tmp_path / "db.sqlite3",
        LOG_DIR=tmp_path / "logs",
        HISTORY_DIR=tmp_path / "history",
        TELECODEX_ADMIN_CHAT_IDS="1001,1002",
    )


def test_session_title_prefers_alias(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    session = SessionRecord(
        id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path="/tmp/demo",
        alias="Fix deploy",
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
        history_log_path="/tmp/demo.log",
        codex_resume_ref=None,
    )

    assert app._session_title(session) == "Fix deploy"


def test_session_line_marks_active_session(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    session = SessionRecord(
        id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path="/tmp/demo",
        alias=None,
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
        history_log_path="/tmp/demo.log",
        codex_resume_ref=None,
    )

    line = app._format_session_line(session, session.id)

    assert line.startswith("→ Сессия 12345678")


def test_menu_keyboard_hides_history_and_log_actions(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    buttons = [button.text for row in app._menu_keyboard().inline_keyboard for button in row]

    assert "История" not in buttons
    assert "Лог" not in buttons


def test_result_keyboard_hides_continue_and_log_actions(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    buttons = [button.text for row in app._result_keyboard().inline_keyboard for button in row]

    assert buttons == ["Новая сессия", "Сменить проект"]


def _build_restart_app(tmp_path: Path, restart_callback: AsyncMock | None = None) -> TelecodexApplication:
    return TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
        restart_callback=restart_callback,
    )


@pytest.mark.asyncio
async def test_restart_rejected_for_non_admin(tmp_path: Path) -> None:
    app = _build_restart_app(tmp_path)
    message = SimpleNamespace(chat=SimpleNamespace(id=2000), answer=AsyncMock())

    await app._handle_restart(message)

    message.answer.assert_awaited_once_with("Команда недоступна.")


@pytest.mark.asyncio
async def test_restart_rejected_when_run_active(tmp_path: Path) -> None:
    restart_callback = AsyncMock()
    app = _build_restart_app(tmp_path, restart_callback=restart_callback)
    app.active_runs[2000] = ActiveRun(
        started_at=0.0,
        project_name="demo",
        session_id="session-1",
        cancel_event=asyncio.Event(),
    )
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), answer=AsyncMock())

    await app._handle_restart(message)

    message.answer.assert_awaited_once_with("Есть активные задачи. Сначала дождитесь завершения или выполните /cancel.")
    restart_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_schedules_callback_for_admin(tmp_path: Path) -> None:
    restart_callback = AsyncMock()
    app = _build_restart_app(tmp_path, restart_callback=restart_callback)
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), answer=AsyncMock())

    await app._handle_restart(message)
    await asyncio.sleep(0)

    message.answer.assert_awaited_once_with("Перезапуск сервиса запрошен. Возвращаюсь после рестарта.")
    restart_callback.assert_awaited_once()
