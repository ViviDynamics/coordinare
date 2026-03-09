"""Performer configuration loaded from environment variables."""
from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-variable configuration for the performer."""

    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    AGENT_BACKEND: str = "opencode"
    AGENT_TIMEOUT: int = 1800  # seconds — 30 minutes


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Call ``get_settings.cache_clear()`` in tests to force reconstruction.
    """
    return Settings()
