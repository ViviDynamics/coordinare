"""Models for performer environment caching (spec 060)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic field resolution
from pathlib import Path  # noqa: TC003 — needed at runtime for pydantic field resolution
from typing import Literal

from pydantic import BaseModel, Field


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


class BootstrapJobPayload(BaseModel):
    """Dispatch payload for an env_bootstrap performer job."""

    job_type: Literal["env_bootstrap"] = "env_bootstrap"

    symphony_name: str = Field(
        description="Human-readable symphony name (for logging/labelling inside the container)."
    )
    symphony_org: str = Field(description="GitHub organisation owning the symphony's repo.")
    symphony_repo: str = Field(description="GitHub repository name for the symphony.")

    env_spec_files: list[str] = Field(
        default_factory=lambda: ["README.md"],
        description="Relative paths of the files whose content describes the dev environment.",
    )
    env_spec_contents: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of file path → full text content at the time of the SHA change.",
    )

    cache_mount_path: str = Field(
        default="/devenv/symphony",
        description=(
            "Full in-container path to the symphony's env cache subdirectory, e.g. "
            "'/devenv/my-project-a1b2c3'. Coordinare sets this from "
            "'{container_devenv_root}/{sanitised_symphony_name}'. The default is "
            "illustrative; the actual value is always computed by coordinare from config."
        ),
    )
