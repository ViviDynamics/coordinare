"""Pydantic models for containerized performer registrations (spec 056)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic type resolution
from pathlib import (  # noqa: TC003 — needed at runtime for pydantic type resolution
    Path,
    PurePosixPath,
)
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)

PerformerMode = Literal["subprocess", "ephemeral", "persistent"]
Availability = Literal["unknown", "starting", "idle", "busy", "draining", "unreachable"]
ToolFlag = Literal[
    "git",
    "node",
    "python",
    "browser",
    "lint",
    "format",
    "test_runner",
    "ripgrep",
    "jq",
    "shell",
]
JobState = Literal["accepted", "running", "succeeded", "failed", "cancelled"]
BusyReason = Literal[
    "busy",
    "draining",
    "auth_failed",
    "capability_mismatch",
    "secret_missing",
]


class SecretSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_payload: bool = True
    env: bool = True
    creds_file: bool = True
    creds_file_path: PurePosixPath | None = None


class VolumeMount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_path: Path
    container_path: PurePosixPath
    mode: Literal["ro", "rw"] = "ro"


class CapabilityOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backends: list[str] = Field(default_factory=list)
    tool_flags: list[ToolFlag] = Field(default_factory=list)


class PerformerCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    backends: list[str] = Field(default_factory=list)
    tool_flags: list[str] = Field(default_factory=list)


class PerformerEndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mode: PerformerMode = "subprocess"
    roles: list[str]
    image: str | None = None
    endpoint: HttpUrl | None = None
    port: int | None = None
    auth_token: SecretStr | None = None
    readiness_timeout_s: int = 120
    failure_threshold: int = 5
    secret_sources: SecretSourceConfig = Field(default_factory=SecretSourceConfig)
    volumes: list[VolumeMount] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    capability_overrides: CapabilityOverride | None = None
    container_devenv_root: str = "/devenv"

    @field_validator("readiness_timeout_s")
    @classmethod
    def _readiness_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("readiness_timeout_s must be >= 1")
        return v

    @field_validator("failure_threshold")
    @classmethod
    def _threshold_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("failure_threshold must be >= 1")
        return v

    @model_validator(mode="after")
    def _check_mode_coupling(self) -> PerformerEndpointConfig:
        if self.mode == "subprocess":
            set_fields: list[str] = []
            if self.image is not None:
                set_fields.append("image")
            if self.endpoint is not None:
                set_fields.append("endpoint")
            if self.auth_token is not None:
                set_fields.append("auth_token")
            if self.port is not None:
                set_fields.append("port")
            if self.volumes:
                set_fields.append("volumes")
            if self.env:
                set_fields.append("env")
            if self.secret_sources != SecretSourceConfig():
                set_fields.append("secret_sources")
            if self.container_devenv_root != "/devenv":
                set_fields.append("container_devenv_root")
            if set_fields:
                raise ValueError(
                    f"subprocess performers must not set: {', '.join(set_fields)}"
                )
        elif self.mode == "persistent":
            if self.endpoint is None:
                raise ValueError("persistent performers require endpoint")
            if self.image is None:
                raise ValueError("persistent performers require image")
        elif self.mode == "ephemeral" and self.image is None:
            raise ValueError("ephemeral performers require image")
        return self


class HotReloadRejectedError(RuntimeError):
    """Raised when a performer endpoint hot-reload is attempted while a job is in flight."""


def apply_endpoint_reload(
    current: PerformerEndpointState,
    new_config: PerformerEndpointConfig,
) -> None:
    """Validate that *current* may be replaced with *new_config*.

    A performer endpoint registration must not be hot-reloaded while a job is
    in flight; the running job's outcome would be ambiguous if the endpoint
    URL or auth token changed mid-flight. Callers detect this by inspecting
    ``current.current_job_id`` and re-queue the reload for after job
    completion.
    """
    if current.current_job_id is not None:
        raise HotReloadRejectedError(
            f"performer {current.id} has in-flight job {current.current_job_id}; "
            "deferred reload required"
        )
    if current.id != new_config.id:
        raise HotReloadRejectedError(
            f"id mismatch: state={current.id} vs config={new_config.id}"
        )


def detect_duplicate_endpoints(
    configs: list[PerformerEndpointConfig],
) -> list[str]:
    """Return list of endpoint URLs that appear under more than one registration."""
    seen: dict[str, list[str]] = {}
    for cfg in configs:
        if cfg.endpoint is None:
            continue
        key = str(cfg.endpoint)
        seen.setdefault(key, []).append(cfg.id)
    return [endpoint for endpoint, ids in seen.items() if len(ids) > 1]


class PerformerEndpointState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    mode: Literal["ephemeral", "persistent"]
    endpoint: HttpUrl | None = None
    roles: list[str] = Field(default_factory=list)
    availability: Availability = "unknown"
    capabilities: PerformerCapabilities | None = None
    current_job_id: str | None = None
    last_status_at: datetime | None = None
    consecutive_failures: int = 0
    excluded_until_recovery: bool = False


# ---- Over-the-wire job protocol models ----


class JobInitPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    card_id: str
    role: str
    backend: str
    persona: str
    repo_url: HttpUrl
    branch: str
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class JobAcceptResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accepted: Literal[True] = True
    job_id: str
    started_at: datetime


class JobBusyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accepted: Literal[False] = False
    reason: BusyReason
    retry_after_s: int | None = None
    detail: str | None = None


class JobResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    success: bool
    summary: str
    diff_url: HttpUrl | None = None
    logs_excerpt: str | None = None
    error_code: str | None = None


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    state: JobState
    progress_pct: int | None = None
    last_event: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    result: JobResult | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] | None = None

    @field_validator("progress_pct")
    @classmethod
    def _progress_in_range(cls, v: int | None) -> int | None:
        if v is None:
            return v
        if v < 0 or v > 100:
            raise ValueError("progress_pct must be in [0, 100]")
        return v


class CancelResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    honored: bool
    detail: str | None = None


class PerformerStatus(BaseModel):
    """Runtime payload returned by GET /status."""

    model_config = ConfigDict(extra="ignore")

    availability: Literal["starting", "idle", "busy", "draining"]
    capabilities: PerformerCapabilities
    auth_enabled: bool
    current_job_id: str | None = None
    version: str | None = None


__all__ = [
    "Availability",
    "BusyReason",
    "CancelResponse",
    "CapabilityOverride",
    "JobAcceptResponse",
    "JobBusyResponse",
    "JobInitPayload",
    "JobResult",
    "JobState",
    "JobStatus",
    "PerformerCapabilities",
    "PerformerEndpointConfig",
    "PerformerEndpointState",
    "PerformerMode",
    "PerformerStatus",
    "SecretSourceConfig",
    "ToolFlag",
    "VolumeMount",
    "detect_duplicate_endpoints",
]
