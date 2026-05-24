"""Performer configuration loaded from environment variables."""
from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-variable configuration for the performer."""

    model_config = SettingsConfigDict(env_ignore_empty=True, extra="ignore")

    AGENT_BACKEND: str = "opencode"
    # Supported values: "opencode" | "junie" | "cursor" | "claude_code" | "codex"
    AGENT_TIMEOUT: int = 7200  # seconds — 120 minutes

    # 036 — GitHub Enterprise: configurable REST API base URL
    GITHUB_API_URL: str = "https://api.github.com"
    CHECK_MAX_ATTEMPTS: int = 8  # max CI fix cycles before blocking the card
    # 065 Fix 14: bail after this many consecutive identical-failure attempts;
    # if the model can't fix it in 2 tries, more grinding won't help.
    CHECK_NO_PROGRESS_LIMIT: int = 2
    # 071 — CI log inlining.  Below this many chars of `output.text`, the
    # performer auto-fetches the workflow log and appends the tail to the
    # backend relay so the model never relies on a separate tool call to see
    # the failure.  Set MAX to 0 to disable inlining entirely.
    CI_LOG_INLINE_MIN_OUTPUT_CHARS: int = 200
    CI_LOG_INLINE_MAX_CHARS: int = 6000

    # 020 — Architect performer settings
    PLAN_FILE_PATH: str = "docs/coordinare-architecture.md"

    # 021 — Reviewer performer settings
    REVIEWER_MAX_CYCLES: int = 3  # max review cycles before blocking for human

    # 022 — Security performer settings
    SECURITY_MAX_CYCLES: int = 3  # max security fix cycles before blocking for human

    # 023 — QA performer settings
    QA_MAX_CYCLES: int = 3  # max QA fix cycles before blocking for human

    # 045 — Backend output parse retry budget
    # When the backend (assessor, reviewer, security, QA, docs) returns output
    # that isn't parseable as a JSON object, retry the backend run up to this
    # many times before bubbling the error to the coordinare.  Each retry is a
    # fresh LLM call (tokens, minutes), so keep this low.  0 disables retry.
    BACKEND_PARSE_RETRIES: int = 1


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton.

    Call ``get_settings.cache_clear()`` in tests to force reconstruction.
    """
    return Settings()
