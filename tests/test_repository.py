from pathlib import Path

import pytest

from telecodex_bot.db import init_db
from telecodex_bot.repository import Repository


@pytest.mark.asyncio
async def test_chat_state_and_session(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    repo = Repository(db_path)

    session = await repo.create_session("demo", "/tmp/demo", str(tmp_path / "s.log"))
    await repo.set_chat_state(100, "demo", session.id)

    state = await repo.get_chat_state(100)
    assert state is not None
    assert state.project_name == "demo"
    assert state.session_id == session.id
