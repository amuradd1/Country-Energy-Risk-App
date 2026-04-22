import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    admin_api_key: str = "changeme"
    port: int = 8000
    scoring_cron_day_of_week: str = "mon"
    scoring_cron_hour: int = 6
    database_path: str = "energy_risk.db"
    anthropic_model: str = "claude-opus-4-7"
    scoring_batch_size: int = 1


@lru_cache
def get_settings() -> Settings:
    return Settings()
