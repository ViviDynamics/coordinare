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

    # 063 Phase 4 (T024): set by EnvCacheService.mark_runtime_health_failed when
    # a performer reports a non-zero services-health.sh exit. Causes the next
    # check_and_trigger cycle to dispatch a forced regeneration that bypasses
    # the cache_inputs key entirely, then clears the flag.
    runtime_health_failed: bool = False

    # 063 Cross-cutting (T026d): summary of the most recent service-inference
    # outcome from the env_bootstrap performer. Populated by
    # ``EnvCacheService.record_inference_outcome`` when a bootstrap job
    # finishes (terminal status) so the dashboard can surface what the agent
    # produced. ``last_inference_skipped_reason`` is non-None when the agent
    # did not run (no env_cache_path, coordinare package missing, manual
    # override applied, no API key, unexpected error).
    last_inference_at: datetime | None = None
    last_inference_skipped_reason: str | None = None
    last_inference_agent_version: str | None = None
    last_inference_attempts: int | None = None
    last_inference_succeeded: bool | None = None
    last_inference_services: list[str] = Field(default_factory=list)


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
