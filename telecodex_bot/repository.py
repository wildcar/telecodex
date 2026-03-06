from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class SessionRecord:
    id: str
    project_name: str
    project_path: str
    alias: Optional[str]
    created_at: str
    updated_at: str
    history_log_path: str
    codex_resume_ref: Optional[str]


@dataclass(slots=True)
class ChatState:
    chat_id: int
    project_name: Optional[str]
    session_id: Optional[str]
    updated_at: str


@dataclass(slots=True)
class HistoryItem:
    role: str
    content: str
    created_at: str


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

    async def _execute_many(self, statements: list[tuple[str, tuple[object, ...]]]) -> None:
        with self._connect() as conn:
            for query, params in statements:
                conn.execute(query, params)
            conn.commit()

    async def get_chat_state(self, chat_id: int) -> Optional[ChatState]:
        row = await self._fetchone(
            "SELECT chat_id, project_name, session_id, updated_at FROM chat_state WHERE chat_id = ?",
            (chat_id,),
        )
        if not row:
            return None
        return ChatState(
            chat_id=row["chat_id"],
            project_name=row["project_name"],
            session_id=row["session_id"],
            updated_at=row["updated_at"],
        )

    async def set_chat_state(self, chat_id: int, project_name: Optional[str], session_id: Optional[str]) -> None:
        now = self._now()
        await self._execute(
            """
            INSERT INTO chat_state(chat_id, project_name, session_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                project_name = excluded.project_name,
                session_id = excluded.session_id,
                updated_at = excluded.updated_at
            """,
            (chat_id, project_name, session_id, now),
        )

    async def create_session(self, project_name: str, project_path: str, history_log_path: str) -> SessionRecord:
        now = self._now()
        session_id = str(uuid.uuid4())
        await self._execute(
            """
            INSERT INTO sessions(id, project_name, project_path, alias, created_at, updated_at, history_log_path, codex_resume_ref)
            VALUES (?, ?, ?, NULL, ?, ?, ?, NULL)
            """,
            (session_id, project_name, project_path, now, now, history_log_path),
        )
        return await self.get_session(session_id)  # type: ignore[return-value]

    async def get_session(self, session_id: str) -> Optional[SessionRecord]:
        row = await self._fetchone(
            """
            SELECT id, project_name, project_path, alias, created_at, updated_at, history_log_path, codex_resume_ref
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        )
        if not row:
            return None
        return SessionRecord(
            id=row["id"],
            project_name=row["project_name"],
            project_path=row["project_path"],
            alias=row["alias"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            history_log_path=row["history_log_path"],
            codex_resume_ref=row["codex_resume_ref"],
        )

    async def list_sessions(self, project_name: str, limit: int) -> list[SessionRecord]:
        rows = await self._fetchall(
            """
            SELECT id, project_name, project_path, alias, created_at, updated_at, history_log_path, codex_resume_ref
            FROM sessions
            WHERE project_name = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (project_name, limit),
        )
        return [
            SessionRecord(
                id=row["id"],
                project_name=row["project_name"],
                project_path=row["project_path"],
                alias=row["alias"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                history_log_path=row["history_log_path"],
                codex_resume_ref=row["codex_resume_ref"],
            )
            for row in rows
        ]

    async def rename_session(self, session_id: str, alias: str) -> bool:
        now = self._now()
        rowcount = await self._execute(
            "UPDATE sessions SET alias = ?, updated_at = ? WHERE id = ?",
            (alias, now, session_id),
        )
        return rowcount > 0

    async def set_codex_resume_ref(self, session_id: str, codex_resume_ref: str) -> bool:
        now = self._now()
        rowcount = await self._execute(
            "UPDATE sessions SET codex_resume_ref = ?, updated_at = ? WHERE id = ?",
            (codex_resume_ref, now, session_id),
        )
        return rowcount > 0

    async def add_history(self, session_id: str, role: str, content: str) -> None:
        now = self._now()
        await self._execute_many(
            [
                (
                    "INSERT INTO history(session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                    (session_id, role, content, now),
                ),
                ("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)),
            ]
        )

    async def get_recent_history(self, session_id: str, limit: int) -> list[HistoryItem]:
        rows = await self._fetchall(
            """
            SELECT role, content, created_at
            FROM history
            WHERE session_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (session_id, limit),
        )
        rows.reverse()
        return [HistoryItem(role=row["role"], content=row["content"], created_at=row["created_at"]) for row in rows]
