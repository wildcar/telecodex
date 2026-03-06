from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest
from aiogram import Bot, Dispatcher

from telecodex_bot.bot import (
    AccessMiddleware,
    ActiveRun,
    TelecodexApplication,
    _append_conversation_log,
    _load_restart_request,
)
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
        ("menu", "Показать меню"),
        ("projects", "Список проектов"),
        ("sessions", "Список сессий"),
        ("status", "Текущий статус"),
        ("cancel", "Остановить задачу"),
        ("restart", "Перезапустить сервис"),
    ]


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
        user_prompt="почини\nлог",
        command="codex exec 'сырой prompt'",
        codex_output="[stdout] первая строка\n[stderr] вторая строка\n",
    )

    assert path.read_text(encoding="utf-8") == (
        "[2026-03-06 12:34:56 UTC]\n"
        "USER MESSAGE:\n"
        "почини\n"
        "лог\n"
        "COMMAND:\n"
        "codex exec 'сырой prompt'\n"
        "CODEX OUTPUT:\n"
        "[stdout] первая строка\n"
        "[stderr] вторая строка\n"
        "\n"
    )


@pytest.mark.asyncio
async def test_access_middleware_denies_non_admin_message() -> None:
    middleware = AccessMiddleware({1001})
    message = SimpleNamespace(chat=SimpleNamespace(id=2000), answer=AsyncMock())
    handler = AsyncMock()

    result = await middleware(handler, message, {})

    assert result is None
    message.answer.assert_awaited_once_with("Нет доступа")
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
    callback.message.answer.assert_awaited_once_with("Нет доступа")
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
        codex_session_id="session-1",
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

    message.answer.assert_awaited_once_with("Перезапуск сервиса запрошен.")
    restart_callback.assert_awaited_once()
    request = _load_restart_request(app._restart_marker_path())
    assert request is not None
    assert request.chat_id == 1001


@pytest.mark.asyncio
async def test_notify_restart_success_sends_message_and_clears_marker(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    marker_path = app._restart_marker_path()
    marker_path.write_text('{"chat_id": 1001, "requested_at": "2026-03-06T14:30:00+00:00"}', encoding="utf-8")
    app.bot.send_message = AsyncMock()

    await app.notify_restart_success_if_needed()

    app.bot.send_message.assert_awaited_once_with(1001, "Сервис был перезапущен успешно.")
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

    message.answer.assert_awaited_once_with("Голосовые сообщения недоступны: не настроен DEEPGRAM_API_KEY.")


@pytest.mark.asyncio
async def test_handle_voice_message_transcribes_and_runs_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deepgram = SimpleNamespace(transcribe_ogg_opus=AsyncMock(return_value="Расшифрованный текст"))
    app = _build_app(tmp_path, deepgram=deepgram)
    app._execute_prompt = AsyncMock()

    async def _no_indicator(status_message, stop_event, base_text="Распознаю голосовое сообщение") -> None:
        await stop_event.wait()

    monkeypatch.setattr("telecodex_bot.bot._progress_message_indicator", _no_indicator)

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
    assert message.answer.await_args_list[0].args == ("Распознаю голосовое сообщение ⠋",)
    assert message.answer.await_args_list[1].args == ("Расшифрованный текст",)
    app._execute_prompt.assert_awaited_once_with(message, "Расшифрованный текст")
