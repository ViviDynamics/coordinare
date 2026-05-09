"""Contract: EnvCacheState — per-symphony in-memory tracking model.

Added to CoordinareState as ``env_cache: dict[str, EnvCacheState]``
(keyed by symphony name). Resets on coordinare restart.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003
from pathlib import Path  # noqa: TC003

from pydantic import BaseModel


class EnvCacheState(BaseModel):
    """Live tracking of one symphony's env-cache bootstrapping status."""

    symphony_name: str
    sanitised_name: str
    cache_dir: Path

    readme_sha: str | None = None
    bootstrap_in_flight: bool = False
    pending_sha: str | None = None
    last_bootstrap_at: datetime | None = None
    last_bootstrap_succeeded: bool | None = None
    cache_dir_ready: bool = False
