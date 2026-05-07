"""Application configuration -- loaded once at the composition root.

CLAUDE.md hard rule §4.7: domain code never reads env vars. This
module is the single point where env values become typed Python
objects; downstream code receives them by injection.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = Field(
        default="sqlite+aiosqlite:///:memory:",
        description=(
            "SQLAlchemy async URL. Postgres in dev/prod "
            "(postgresql+asyncpg://...), aiosqlite for fast unit tests."
        ),
    )
    rate_api_key: str = Field(default="")
    admin_token: str = Field(default="")
    env: Literal["development", "test", "production"] = Field(default="development")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor used by the composition root and Alembic env."""
    return Settings()
