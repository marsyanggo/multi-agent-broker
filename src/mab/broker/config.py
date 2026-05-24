from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAB_", env_file=".env", extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8420
    db_path: Path = Path.home() / ".multi-agent-broker" / "db.sqlite"
    message_ttl_days: int = 7
    heartbeat_interval_seconds: int = 30
    # Stuck-task reaper: how often to scan, and how long an assignee's
    # heartbeat can be silent before its in-flight tasks are considered
    # orphaned. The threshold defaults to 3x heartbeat_interval (matches
    # the AgentSnapshot.is_stale rule).
    task_reap_interval_seconds: int = 60
    task_reap_stale_multiplier: int = 3


settings = Settings()
