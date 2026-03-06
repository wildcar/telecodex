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


@pytest.mark.asyncio
async def test_delete_session_clears_active_chat_state(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    repo = Repository(db_path)

    session = await repo.save_session("12345678-1234-1234-1234-1234567890ab", "demo", "/tmp/demo")
    await repo.set_chat_state(100, "demo", session.codex_session_id)

    deleted = await repo.delete_session(session.codex_session_id)
    state = await repo.get_chat_state(100)
    reloaded = await repo.get_session(session.codex_session_id)

    assert deleted is True
    assert state is not None
    assert state.project_name == "demo"
    assert state.codex_session_id is None
    assert reloaded is None


@pytest.mark.asyncio
async def test_save_project_persists_and_lists(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    repo = Repository(db_path)

    saved = await repo.save_project("infra", "/tmp/infra")
    items = await repo.list_projects()

    assert saved.name == "infra"
    assert saved.project_path == "/tmp/infra"
    assert [item.name for item in items] == ["infra"]


@pytest.mark.asyncio
async def test_delete_project_clears_chat_state_and_sessions(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    repo = Repository(db_path)

    await repo.save_project("demo", "/tmp/demo")
    session = await repo.save_session("12345678-1234-1234-1234-1234567890ab", "demo", "/tmp/demo")
    await repo.set_chat_state(100, "demo", session.codex_session_id)

    deleted = await repo.delete_project("demo")
    state = await repo.get_chat_state(100)
    project = await repo.get_project("demo")
    reloaded_session = await repo.get_session(session.codex_session_id)

    assert deleted is True
    assert state is not None
    assert state.project_name is None
    assert state.codex_session_id is None
    assert project is None
    assert reloaded_session is None
