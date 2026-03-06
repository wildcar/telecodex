from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class SessionRecord:
    codex_session_id: str
    project_name: str
    project_path: str
    alias: Optional[str]
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ProjectRecord:
    name: str
    project_path: str
    created_at: str
    updated_at: str


@dataclass(slots=True)
class ChatState:
    chat_id: int
    project_name: Optional[str]
    codex_session_id: Optional[str]
    updated_at: str


class Repository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def _fetchone(self, query: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchone()

    async def _fetchall(self, query: str, params: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            return list(cursor.fetchall())

    async def _execute(self, query: str, params: tuple[object, ...]) -> int:
        with self._connect() as conn:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor.rowcount

    async def get_chat_state(self, chat_id: int) -> Optional[ChatState]:
        row = await self._fetchone(
            "SELECT chat_id, project_name, codex_session_id, updated_at FROM chat_state WHERE chat_id = ?",
            (chat_id,),
        )
        if not row:
            return None
        return ChatState(
            chat_id=row["chat_id"],
            project_name=row["project_name"],
            codex_session_id=row["codex_session_id"],
            updated_at=row["updated_at"],
        )

    async def set_chat_state(self, chat_id: int, project_name: Optional[str], codex_session_id: Optional[str]) -> None:
        now = self._now()
        await self._execute(
            """
            INSERT INTO chat_state(chat_id, project_name, codex_session_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                project_name = excluded.project_name,
                codex_session_id = excluded.codex_session_id,
                updated_at = excluded.updated_at
            """,
            (chat_id, project_name, codex_session_id, now),
        )

    async def save_project(self, name: str, project_path: str) -> ProjectRecord:
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM projects WHERE name = ?",
                (name,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE projects
                    SET project_path = ?, updated_at = ?
                    WHERE name = ?
                    """,
                    (project_path, now, name),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO projects(name, project_path, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (name, project_path, now, now),
                )
            conn.commit()
        return await self.get_project(name)  # type: ignore[return-value]

    async def get_project(self, name: str) -> Optional[ProjectRecord]:
        row = await self._fetchone(
            "SELECT name, project_path, created_at, updated_at FROM projects WHERE name = ?",
            (name,),
        )
        if not row:
            return None
        return ProjectRecord(
            name=row["name"],
            project_path=row["project_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_projects(self) -> list[ProjectRecord]:
        rows = await self._fetchall(
            """
            SELECT name, project_path, created_at, updated_at
            FROM projects
            ORDER BY name COLLATE NOCASE
            """,
            (),
        )
        return [
            ProjectRecord(
                name=row["name"],
                project_path=row["project_path"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def delete_project(self, name: str) -> bool:
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone()
            if not existing:
                return False
            conn.execute(
                """
                UPDATE chat_state
                SET project_name = NULL, codex_session_id = NULL, updated_at = ?
                WHERE project_name = ?
                """,
                (now, name),
            )
            conn.execute("DELETE FROM sessions WHERE project_name = ?", (name,))
            conn.execute("DELETE FROM projects WHERE name = ?", (name,))
            conn.commit()
        return True

    async def save_session(self, codex_session_id: str, project_name: str, project_path: str) -> SessionRecord:
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT alias, created_at
                FROM sessions
                WHERE codex_session_id = ?
                """,
                (codex_session_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE sessions
                    SET project_name = ?, project_path = ?, updated_at = ?
                    WHERE codex_session_id = ?
                    """,
                    (project_name, project_path, now, codex_session_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO sessions(codex_session_id, project_name, project_path, alias, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, ?, ?)
                    """,
                    (codex_session_id, project_name, project_path, now, now),
                )
            conn.commit()
        return await self.get_session(codex_session_id)  # type: ignore[return-value]

    async def get_session(self, codex_session_id: str) -> Optional[SessionRecord]:
        row = await self._fetchone(
            """
            SELECT codex_session_id, project_name, project_path, alias, created_at, updated_at
            FROM sessions
            WHERE codex_session_id = ?
            """,
            (codex_session_id,),
        )
        if not row:
            return None
        return SessionRecord(
            codex_session_id=row["codex_session_id"],
            project_name=row["project_name"],
            project_path=row["project_path"],
            alias=row["alias"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list_sessions(self, project_name: str, limit: int) -> list[SessionRecord]:
        rows = await self._fetchall(
            """
            SELECT codex_session_id, project_name, project_path, alias, created_at, updated_at
            FROM sessions
            WHERE project_name = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (project_name, limit),
        )
        return [
            SessionRecord(
                codex_session_id=row["codex_session_id"],
                project_name=row["project_name"],
                project_path=row["project_path"],
                alias=row["alias"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    async def rename_session(self, codex_session_id: str, alias: str) -> bool:
        now = self._now()
        rowcount = await self._execute(
            "UPDATE sessions SET alias = ?, updated_at = ? WHERE codex_session_id = ?",
            (alias, now, codex_session_id),
        )
        return rowcount > 0

    async def touch_session(self, codex_session_id: str) -> bool:
        now = self._now()
        rowcount = await self._execute(
            "UPDATE sessions SET updated_at = ? WHERE codex_session_id = ?",
            (now, codex_session_id),
        )
        return rowcount > 0

    async def delete_session(self, codex_session_id: str) -> bool:
        now = self._now()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE codex_session_id = ?",
                (codex_session_id,),
            ).fetchone()
            if not existing:
                return False
            conn.execute(
                "UPDATE chat_state SET codex_session_id = NULL, updated_at = ? WHERE codex_session_id = ?",
                (now, codex_session_id),
            )
            conn.execute(
                "DELETE FROM sessions WHERE codex_session_id = ?",
                (codex_session_id,),
            )
            conn.commit()
        return True
