from pathlib import Path

from aiogram import Bot, Dispatcher

from telecodex_bot.bot import TelecodexApplication
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
