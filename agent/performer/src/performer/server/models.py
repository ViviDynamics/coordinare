"""Wire-protocol models for the performer-side HTTP server (spec 056).

Mirrors the coordinare-side models in ``coordinare.models.performer_endpoint``.
We duplicate the type definitions rather than import from coordinare because
the performer is a separately-installable package; the OpenAPI contract at
``specs/056-performer-containerization/contracts/performer-http.openapi.yaml``
is the source of truth that keeps the two in sync.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator

JobState = Literal["accepted", "running", "succeeded", "failed", "cancelled"]
BusyReason = Literal[
    "busy",
    "draining",
    "auth_failed",
    "capability_mismatch",
    "secret_missing",
]


class PerformerCapabilities(BaseModel):
    model_config = ConfigDict(extra="ignore")

    backends: list[str] = Field(default_factory=list)
    tool_flags: list[str] = Field(default_factory=list)


class PerformerStatus(BaseModel):
    model_config = ConfigDict(extra="ignore")

    availability: Literal["starting", "idle", "busy", "draining"]
    capabilities: PerformerCapabilities
    auth_enabled: bool
    current_job_id: str | None = None
    version: str | None = None


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


__all__ = [
    "BusyReason",
    "CancelResponse",
    "JobAcceptResponse",
    "JobBusyResponse",
    "JobInitPayload",
    "JobResult",
    "JobState",
    "JobStatus",
    "PerformerCapabilities",
    "PerformerStatus",
]
