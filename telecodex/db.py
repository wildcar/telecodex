from __future__ import annotations

import sqlite3

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
    codex_session_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    project_path TEXT NOT NULL,
    alias TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    project_name TEXT,
    codex_session_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(codex_session_id) REFERENCES sessions(codex_session_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_updated ON sessions(project_name, updated_at DESC);

CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _migrate_sessions_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "sessions")
    if not columns or "codex_session_id" in columns:
        return
    conn.execute(
        """
        INSERT INTO legacy_session_map(bot_session_id, codex_session_id)
        SELECT id AS bot_session_id, codex_resume_ref AS codex_session_id
        FROM sessions
        WHERE codex_resume_ref IS NOT NULL AND codex_resume_ref != ''
        """
    )
    conn.execute(
        """
        CREATE TABLE sessions_new (
            codex_session_id TEXT PRIMARY KEY,
            project_name TEXT NOT NULL,
            project_path TEXT NOT NULL,
            alias TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions_new(codex_session_id, project_name, project_path, alias, created_at, updated_at)
        SELECT codex_resume_ref, project_name, project_path, alias, created_at, updated_at
        FROM sessions
        WHERE codex_resume_ref IS NOT NULL AND codex_resume_ref != ''
        """
    )
    conn.execute("DROP TABLE sessions")
    conn.execute("ALTER TABLE sessions_new RENAME TO sessions")


def _migrate_chat_state_table(conn: sqlite3.Connection) -> None:
    columns = _table_columns(conn, "chat_state")
    if not columns or "codex_session_id" in columns:
        return
    conn.execute(
        """
        CREATE TABLE chat_state_new (
            chat_id INTEGER PRIMARY KEY,
            project_name TEXT,
            codex_session_id TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(codex_session_id) REFERENCES sessions(codex_session_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO chat_state_new(chat_id, project_name, codex_session_id, updated_at)
        SELECT cs.chat_id, cs.project_name, m.codex_session_id, cs.updated_at
        FROM chat_state cs
        LEFT JOIN legacy_session_map m ON m.bot_session_id = cs.session_id
        """
    )
    conn.execute("DROP TABLE chat_state")
    conn.execute("ALTER TABLE chat_state_new RENAME TO chat_state")


def _drop_legacy_history(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS history")
    conn.execute("DROP INDEX IF EXISTS idx_history_session_created")
    conn.execute("DROP TABLE IF EXISTS legacy_session_map")


def _init_db_sync(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TABLE IF EXISTS legacy_session_map")
        conn.execute(
            "CREATE TEMP TABLE legacy_session_map(bot_session_id TEXT PRIMARY KEY, codex_session_id TEXT NOT NULL)"
        )
        _migrate_sessions_table(conn)
        _migrate_chat_state_table(conn)
        _drop_legacy_history(conn)
        conn.executescript(SCHEMA_SQL)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()


async def init_db(db_path: str) -> None:
    _init_db_sync(db_path)
