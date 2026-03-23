"""Performer configuration loaded from environment variables."""
from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-variable configuration for the performer."""

    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    AGENT_BACKEND: str = "opencode"
    # Supported values: "opencode" | "claude_code" | "codex" (stub)
    AGENT_TIMEOUT: int = 1800  # seconds — 30 minutes
    CHECK_MAX_ATTEMPTS: int = 3  # max CI fix cycles before blocking the card

    # 020 — Architect performer settings
    PLAN_FILE_PATH: str = "docs/coordinare-architecture.md"

    # 021 — Reviewer performer settings
    REVIEWER_MAX_CYCLES: int = 3  # max review cycles before blocking for human

    # 022 — Security performer settings
    SECURITY_MAX_CYCLES: int = 3  # max security fix cycles before blocking for human

    # 023 — QA performer settings
    QA_MAX_CYCLES: int = 3  # max QA fix cycles before blocking for human


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Call ``get_settings.cache_clear()`` in tests to force reconstruction.
    """
    return Settings()
