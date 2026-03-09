from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest
from aiogram import Bot, Dispatcher

from telecodex.bot import (
    AccessMiddleware,
    ActiveRun,
    PendingProjectDraft,
    TelecodexApplication,
    _append_conversation_log,
    _load_restart_request,
)
from telecodex.config import Settings
from telecodex.db import init_db
from telecodex.repository import Repository, SessionRecord
from telecodex.runner import CodexRunner


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="123:ABC",
        DB_PATH=tmp_path / "db.sqlite3",
        LOG_DIR=tmp_path / "logs",
        HISTORY_DIR=tmp_path / "history",
        TELECODEX_ADMIN_CHAT_IDS="1001,1002",
    )


def test_session_title_uses_project_and_last_used_stamp(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    session = SessionRecord(
        codex_session_id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path="/tmp/demo",
        alias="Fix deploy",
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
    )

    assert app._session_title(session) == "demo-1234567890ab|26-03-05|10:01"


def test_session_line_marks_active_session(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    session = SessionRecord(
        codex_session_id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path="/tmp/demo",
        alias=None,
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
    )

    line = app._format_session_line(session, session.codex_session_id)

    assert line == "→ <code>demo-1234567890ab|26-03-05|10:01</code>"


def test_menu_keyboard_hides_history_and_log_actions(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    buttons = [button.text for row in app._menu_keyboard().inline_keyboard for button in row]

    assert "History" not in buttons
    assert "Log" not in buttons


def test_project_keyboard_includes_new_project_action(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    app.projects = {"demo": Path("/tmp/demo"), "infra": Path("/tmp/infra")}

    buttons = [button.text for row in app._project_keyboard().inline_keyboard for button in row]

    assert buttons == ["demo (/tmp/demo)", "infra (/tmp/infra)", "Delete project", "New project", "Back"]


def test_project_delete_keyboard_marks_projects_with_red_cross(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    app.projects = {"demo": Path("/tmp/demo"), "infra": Path("/tmp/infra")}

    buttons = [button.text for row in app._project_delete_keyboard().inline_keyboard for button in row]

    assert buttons == ["❌ demo (/tmp/demo)", "❌ infra (/tmp/infra)", "Back"]


def test_project_delete_confirm_keyboard_has_yes_no_buttons(tmp_path: Path) -> None:
    app = _build_app(tmp_path)

    buttons = [
        (button.text, button.callback_data)
        for row in app._project_delete_confirm_keyboard("demo").inline_keyboard
        for button in row
    ]

    assert buttons == [("Yes", "project:delete:yes:demo"), ("No", "project:delete:no")]


def test_result_keyboard_hides_continue_and_log_actions(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    buttons = [button.text for row in app._result_keyboard().inline_keyboard for button in row]

    assert buttons == ["New session", "Switch project"]


def test_session_keyboard_includes_delete_action(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    session = SessionRecord(
        codex_session_id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path="/tmp/demo",
        alias=None,
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
    )

    buttons = [button.text for row in app._session_keyboard([session]).inline_keyboard for button in row]

    assert buttons == ["demo-1234567890ab|26-03-05|10:01", "Delete session", "New session", "Back"]


def test_session_delete_keyboard_marks_sessions_with_red_cross(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )
    session = SessionRecord(
        codex_session_id="12345678-1234-1234-1234-1234567890ab",
        project_name="demo",
        project_path="/tmp/demo",
        alias=None,
        created_at="2026-03-05T10:00:00+00:00",
        updated_at="2026-03-05T10:01:00+00:00",
    )

    buttons = [button.text for row in app._session_delete_keyboard([session]).inline_keyboard for button in row]

    assert buttons == ["❌ demo-1234567890ab|26-03-05|10:01", "Back"]


def test_session_delete_confirm_keyboard_has_yes_no_buttons(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    buttons = [
        (button.text, button.callback_data)
        for row in app._session_delete_confirm_keyboard("12345678-1234-1234-1234-1234567890ab").inline_keyboard
        for button in row
    ]

    assert buttons == [
        ("Yes", "session:delete:yes:12345678-1234-1234-1234-1234567890ab"),
        ("No", "session:delete:no"),
    ]


def test_bot_commands_include_main_menu_entries(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    commands = [(item.command, item.description) for item in app._bot_commands()]

    assert commands == [
        ("menu", "Show menu"),
        ("projects", "List projects"),
        ("sessions", "List sessions"),
        ("session_id", "Show current session ID"),
        ("status", "Current status"),
        ("cancel", "Stop task"),
        ("restart", "Restart service"),
    ]


@pytest.mark.asyncio
async def test_load_projects_merges_db_projects_into_runtime_map(tmp_path: Path) -> None:
    await init_db(str(tmp_path / "db.sqlite3"))
    app = _build_app(tmp_path)
    await app.repo.save_project("demo", "/tmp/demo")
    await app.repo.save_project("custom", str(tmp_path))

    await app.load_projects()

    assert "demo" in app.projects
    assert app.projects["custom"] == tmp_path


def test_conversation_log_path_uses_telegram_user_id(tmp_path: Path) -> None:
    app = TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
    )

    assert app._conversation_log_path(4242) == tmp_path / "history" / "conversation4242.log"


def test_append_conversation_log_keeps_plain_raw_content(tmp_path: Path) -> None:
    path = tmp_path / "history" / "conversation77.log"

    _append_conversation_log(
        path,
        timestamp=datetime(2026, 3, 6, 12, 34, 56, tzinfo=UTC),
        user_prompt="fix\nlog",
        command="codex exec 'raw prompt'",
        codex_output="[stdout] first line\n[stderr] second line\n",
    )

    assert path.read_text(encoding="utf-8") == (
        "[2026-03-06 12:34:56 UTC]\n"
        "USER MESSAGE:\n"
        "fix\n"
        "log\n"
        "COMMAND:\n"
        "codex exec 'raw prompt'\n"
        "CODEX OUTPUT:\n"
        "[stdout] first line\n"
        "[stderr] second line\n"
        "\n"
    )


@pytest.mark.asyncio
async def test_access_middleware_denies_non_admin_message() -> None:
    middleware = AccessMiddleware({1001})
    message = SimpleNamespace(chat=SimpleNamespace(id=2000), answer=AsyncMock())
    handler = AsyncMock()

    result = await middleware(handler, message, {})

    assert result is None
    message.answer.assert_awaited_once_with("Access denied")
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_middleware_denies_non_admin_callback() -> None:
    middleware = AccessMiddleware({1001})
    callback = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(id=2000), answer=AsyncMock()),
        answer=AsyncMock(),
    )
    handler = AsyncMock()

    result = await middleware(handler, callback, {})

    assert result is None
    callback.message.answer.assert_awaited_once_with("Access denied")
    callback.answer.assert_awaited_once_with()
    handler.assert_not_awaited()


def _build_restart_app(tmp_path: Path, restart_callback: AsyncMock | None = None) -> TelecodexApplication:
    return TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
        restart_callback=restart_callback,
    )


def _build_app(
    tmp_path: Path,
    *,
    deepgram=None,
    restart_callback: AsyncMock | None = None,
) -> TelecodexApplication:
    return TelecodexApplication(
        bot=Bot("123:ABC"),
        dispatcher=Dispatcher(),
        repo=Repository(tmp_path / "db.sqlite3"),
        runner=CodexRunner("codex exec", timeout_sec=1),
        settings=build_settings(tmp_path),
        deepgram=deepgram,
        restart_callback=restart_callback,
    )


@pytest.mark.asyncio
async def test_latest_session_for_project_returns_most_recent(tmp_path: Path) -> None:
    await init_db(str(tmp_path / "db.sqlite3"))
    app = _build_app(tmp_path)
    first = await app.repo.save_session("11111111-1111-1111-1111-111111111111", "demo", "/tmp/demo")
    latest = await app.repo.save_session("22222222-2222-2222-2222-222222222222", "demo", "/tmp/demo")

    selected = await app._latest_session_for_project("demo")

    assert selected is not None
    assert selected.codex_session_id == latest.codex_session_id


@pytest.mark.asyncio
async def test_restart_rejected_for_non_admin(tmp_path: Path) -> None:
    app = _build_restart_app(tmp_path)
    message = SimpleNamespace(chat=SimpleNamespace(id=2000), answer=AsyncMock())

    await app._handle_restart(message)

    message.answer.assert_awaited_once_with("Command unavailable.")


@pytest.mark.asyncio
async def test_restart_rejected_when_run_active(tmp_path: Path) -> None:
    restart_callback = AsyncMock()
    app = _build_restart_app(tmp_path, restart_callback=restart_callback)
    app.active_runs[2000] = ActiveRun(
        started_at=0.0,
        project_name="demo",
        codex_session_id="session-1",
        cancel_event=asyncio.Event(),
    )
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), answer=AsyncMock())

    await app._handle_restart(message)

    message.answer.assert_awaited_once_with("There are active tasks. Wait for them to finish or use /cancel first.")
    restart_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_schedules_callback_for_admin(tmp_path: Path) -> None:
    restart_callback = AsyncMock()
    app = _build_restart_app(tmp_path, restart_callback=restart_callback)
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), answer=AsyncMock())

    await app._handle_restart(message)
    await asyncio.sleep(0)

    message.answer.assert_awaited_once_with("Service restart requested.")
    restart_callback.assert_awaited_once()
    request = _load_restart_request(app._restart_marker_path())
    assert request is not None
    assert request.chat_id == 1001


@pytest.mark.asyncio
async def test_handle_session_id_requires_selected_session(tmp_path: Path) -> None:
    await init_db(str(tmp_path / "db.sqlite3"))
    app = _build_app(tmp_path)
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), answer=AsyncMock())

    await app._handle_session_id(message)

    message.answer.assert_awaited_once_with("No session selected.")


@pytest.mark.asyncio
async def test_handle_session_id_returns_selected_session(tmp_path: Path) -> None:
    await init_db(str(tmp_path / "db.sqlite3"))
    app = _build_app(tmp_path)
    await app.repo.save_project("demo", "/tmp/demo")
    await app.repo.save_session("11111111-1111-1111-1111-111111111111", "demo", "/tmp/demo")
    await app.repo.set_chat_state(1001, "demo", "11111111-1111-1111-1111-111111111111")
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), answer=AsyncMock())

    await app._handle_session_id(message)

    message.answer.assert_awaited_once_with(
        "Current session ID:\n<code>11111111-1111-1111-1111-111111111111</code>",
        parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_notify_restart_success_sends_message_and_clears_marker(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    marker_path = app._restart_marker_path()
    marker_path.write_text('{"chat_id": 1001, "requested_at": "2026-03-06T14:30:00+00:00"}', encoding="utf-8")
    app.bot.send_message = AsyncMock()

    await app.notify_restart_success_if_needed()

    app.bot.send_message.assert_awaited_once_with(1001, "The service restarted successfully.")
    assert not marker_path.exists()


@pytest.mark.asyncio
async def test_handle_voice_message_requires_deepgram(tmp_path: Path) -> None:
    app = _build_app(tmp_path, deepgram=None)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1001),
        voice=SimpleNamespace(file_id="voice-1"),
        answer=AsyncMock(),
    )

    await app._handle_voice_message(message)

    message.answer.assert_awaited_once_with("Voice messages are unavailable: DEEPGRAM_API_KEY is not configured.")


@pytest.mark.asyncio
async def test_handle_voice_message_transcribes_and_runs_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deepgram = SimpleNamespace(transcribe_ogg_opus=AsyncMock(return_value="Transcribed text"))
    app = _build_app(tmp_path, deepgram=deepgram)
    app._execute_prompt = AsyncMock()

    async def _no_indicator(status_message, stop_event, base_text="Transcribing voice message") -> None:
        await stop_event.wait()

    monkeypatch.setattr("telecodex.bot._progress_message_indicator", _no_indicator)

    status_message = SimpleNamespace(delete=AsyncMock())
    bot = SimpleNamespace(
        get_file=AsyncMock(return_value=SimpleNamespace(file_path="voice/file.ogg")),
        download_file=AsyncMock(side_effect=lambda file_path, destination: destination.write(b"voice-bytes")),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1001),
        voice=SimpleNamespace(file_id="voice-1"),
        bot=bot,
        answer=AsyncMock(side_effect=[status_message, None]),
    )

    await app._handle_voice_message(message)

    bot.get_file.assert_awaited_once_with("voice-1")
    bot.download_file.assert_awaited_once()
    deepgram.transcribe_ogg_opus.assert_awaited_once_with(b"voice-bytes")
    status_message.delete.assert_awaited_once()
    assert message.answer.await_args_list[0].args == ("Transcribing voice message ⠋",)
    assert message.answer.await_args_list[1].args == ("Transcribed text",)
    app._execute_prompt.assert_awaited_once_with(message, "Transcribed text")


@pytest.mark.asyncio
async def test_handle_project_creation_input_creates_project_and_selects_it(tmp_path: Path) -> None:
    await init_db(str(tmp_path / "db.sqlite3"))
    app = _build_app(tmp_path)
    app.pending_project_drafts[1001] = PendingProjectDraft()
    message = SimpleNamespace(chat=SimpleNamespace(id=1001), text="custom", answer=AsyncMock())

    await app._handle_project_creation_input(message)

    draft = app.pending_project_drafts[1001]

    assert draft.name == "custom"
    assert draft.current_path == Path("/")
    message.answer.assert_awaited_once_with(
        app._project_path_browser_text(draft),
        reply_markup=app._project_path_browser_keyboard(draft),
        parse_mode="HTML",
    )


def test_project_path_browser_keyboard_lists_directories_and_confirm(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    (tmp_path / "alpha").mkdir()
    (tmp_path / "beta").mkdir()
    draft = PendingProjectDraft(name="custom", current_path=tmp_path)

    keyboard = app._project_path_browser_keyboard(draft)
    buttons = [button.text for row in keyboard.inline_keyboard for button in row]

    assert buttons == [
        "⬆️ ..",
        "📁 alpha",
        "📁 beta",
        f"✅ {tmp_path}",
        "Back",
    ]
    assert [len(row) for row in keyboard.inline_keyboard] == [1, 2, 1, 1]


@pytest.mark.asyncio
async def test_complete_project_creation_persists_project_and_selects_it(tmp_path: Path) -> None:
    await init_db(str(tmp_path / "db.sqlite3"))
    app = _build_app(tmp_path)
    draft = PendingProjectDraft(name="custom", current_path=tmp_path)
    app.pending_project_drafts[1001] = draft

    await app._complete_project_creation(1001, draft)

    state = await app.repo.get_chat_state(1001)
    saved = await app.repo.get_project("custom")

    assert saved is not None
    assert saved.project_path == str(tmp_path)
    assert state is not None
    assert state.project_name == "custom"
    assert state.codex_session_id is None
    assert app.projects["custom"] == tmp_path
