"""Models for performer environment caching (spec 060)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic field resolution
from pathlib import Path  # noqa: TC003 — needed at runtime for pydantic field resolution
from typing import Any, Literal

from pydantic import BaseModel, Field


class EnvCacheState(BaseModel):
    """Live tracking of one symphony's env-cache bootstrapping status."""

    symphony_name: str
    sanitised_name: str
    cache_dir: Path

    readme_sha: str | None = None
    bootstrap_in_flight: bool = False
    pending_sha: str | None = None
    # 077: the most recent combined spec-file SHA observed by check_and_trigger
    # (refreshed every cycle, even when no bootstrap is needed). Consumer dispatch
    # gates on ``readme_sha == last_seen_spec_sha`` so a card never runs against a
    # cache that predates the current spec (e.g. a README that just added Chrome).
    # Live/derived — re-populated within one poll cycle, so not persisted.
    last_seen_spec_sha: str | None = None
    last_bootstrap_at: datetime | None = None
    last_bootstrap_succeeded: bool | None = None
    # 077: human-readable reason the last bootstrap FAILED (verify output, dispatch
    # error, reap reason, …). Set on failure, cleared on success. Surfaced on the
    # dashboard so an operator can see WHY a symphony's env bootstrap is failing.
    last_bootstrap_error: str | None = None
    cache_dir_ready: bool = False

    # 088 (FR-009/FR-010): bootstrap circuit breaker. ``bootstrap_attempts``
    # counts consecutive failures for the current spec SHA; reset on success
    # and on SHA change. ``bootstrap_exhausted`` is the terminal breaker state:
    # dispatch stops, consumer holds name the exhaustion, and only a SHA change
    # (spec fix) or a successful bootstrap re-arms it. Both persist in the
    # snapshot so a restart does not hand out a fresh budget.
    bootstrap_attempts: int = 0
    bootstrap_exhausted: bool = False

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
    last_failure: str | None = Field(
        default=None,
        description=(
            "077: the previous bootstrap's verification failure (verify.sh output / "
            "error reason), injected into the persona on a RETRY so the agent can "
            "fix the specific issue (e.g. a pinned Ruby version that wasn't installed) "
            "instead of repeating the same miss."
        ),
    )
    dependency_checklist: str | None = Field(
        default=None,
        description=(
            "077: coordinare-derived authoritative install checklist (from the manifest). "
            "Injected into the persona so the agent installs EXACTLY these dependencies "
            "with their pinned versions, instead of free-forming from the README."
        ),
    )
    verify_provided: bool = Field(
        default=False,
        description=(
            "077: True when coordinare has written an authoritative verify.sh into the "
            "cache from the manifest. The agent must RUN it and make it pass, and must "
            "NOT create/overwrite it (coordinare owns the verification contract)."
        ),
    )
    activate_provided: bool = Field(
        default=False,
        description=(
            "087: True when coordinare has written an authoritative, auto-discovering "
            "activate.sh into the cache from the manifest. The agent installs the pinned "
            "toolchain but must NOT create/overwrite activate.sh — coordinare owns the "
            "activation contract. Hand-written activation paths (.rbenv vs rbenv, .nvm "
            "vs nvm) were fumbled every run, leaving a built cache verify.sh couldn't see."
        ),
    )
    declared_services: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "091: durably-declared stateful services (from .coordinare/score.json), each a "
            "ServiceEntry dump. A service with a coordinare-known kind (postgres, redis) makes "
            "the bootstrap fetch its binary as a .deb into <cache>/debs/ via the existing "
            "system-package path, so the cache contains the service binary while the base "
            "image stays agnostic. Empty when no stateful service is declared (behavior "
            "unchanged)."
        ),
    )
