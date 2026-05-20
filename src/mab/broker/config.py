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


settings = Settings()
