from pathlib import Path

import pytest

from telecodex_bot.db import init_db
from telecodex_bot.repository import Repository


@pytest.mark.asyncio
async def test_chat_state_and_session(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    repo = Repository(db_path)

    session = await repo.save_session("12345678-1234-1234-1234-1234567890ab", "demo", "/tmp/demo")
    await repo.set_chat_state(100, "demo", session.codex_session_id)

    state = await repo.get_chat_state(100)
    assert state is not None
    assert state.project_name == "demo"
    assert state.codex_session_id == session.codex_session_id


@pytest.mark.asyncio
async def test_rename_and_touch_session(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    repo = Repository(db_path)

    session = await repo.save_session("12345678-1234-1234-1234-1234567890ab", "demo", "/tmp/demo")
    renamed = await repo.rename_session(session.codex_session_id, "Fix deploy")
    touched = await repo.touch_session(session.codex_session_id)
    reloaded = await repo.get_session(session.codex_session_id)

    assert renamed is True
    assert touched is True
    assert reloaded is not None
    assert reloaded.alias == "Fix deploy"
