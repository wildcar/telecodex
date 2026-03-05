from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = Field(alias="TELEGRAM_BOT_TOKEN")
    telecodex_projects_json: str = Field(alias="TELECODEX_PROJECTS_JSON")
    codex_command: str = Field(default="codex exec", alias="CODEX_COMMAND")
    db_path: Path = Field(default=Path("./data/telecodex.db"), alias="DB_PATH")
    log_dir: Path = Field(default=Path("./logs"), alias="LOG_DIR")
    history_dir: Path = Field(default=Path("./history"), alias="HISTORY_DIR")
    stream_update_interval_sec: float = Field(default=1.0, alias="STREAM_UPDATE_INTERVAL_SEC")
    stream_tail_chars: int = Field(default=3500, alias="STREAM_TAIL_CHARS")
    stream_send_log_threshold: int = Field(default=6000, alias="STREAM_SEND_LOG_THRESHOLD")
    run_timeout_sec: int = Field(default=1800, alias="RUN_TIMEOUT_SEC")
    session_history_items: int = Field(default=8, alias="SESSION_HISTORY_ITEMS")
    sessions_list_limit: int = Field(default=20, alias="SESSIONS_LIST_LIMIT")

    @field_validator("stream_update_interval_sec")
    @classmethod
    def validate_stream_interval(cls, value: float) -> float:
        if value < 0.2:
            raise ValueError("STREAM_UPDATE_INTERVAL_SEC must be >= 0.2")
        return value

    @property
    def projects(self) -> Dict[str, Path]:
        try:
            raw_projects = json.loads(self.telecodex_projects_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid TELECODEX_PROJECTS_JSON: {exc}") from exc

        if not isinstance(raw_projects, dict) or not raw_projects:
            raise ValueError("TELECODEX_PROJECTS_JSON must be a non-empty JSON object")

        normalized: Dict[str, Path] = {}
        for name, path_str in raw_projects.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Project names must be non-empty strings")
            if not isinstance(path_str, str) or not path_str.strip():
                raise ValueError(f"Project path for {name!r} must be non-empty string")
            path = Path(path_str).expanduser().resolve(strict=False)
            if not path.is_absolute():
                raise ValueError(f"Project path for {name!r} must be absolute")
            normalized[name] = path
        return normalized

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.history_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
