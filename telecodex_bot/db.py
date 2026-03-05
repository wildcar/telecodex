from __future__ import annotations

import sqlite3

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_path TEXT NOT NULL,
    alias TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    history_log_path TEXT NOT NULL,
    codex_resume_ref TEXT
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    project_name TEXT,
    session_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_updated ON sessions(project_name, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_session_created ON history(session_id, created_at DESC);
"""


def _init_db_sync(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_SQL)
        conn.commit()


async def init_db(db_path: str) -> None:
    _init_db_sync(db_path)
