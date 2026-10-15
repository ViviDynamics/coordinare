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
from urllib.parse import urlparse

from coordinare_service_inference.cache_provenance import DEFAULT_MAX_BYTES, DEFAULT_MAX_ENTRIES
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator

# ---- Progress streaming types ----

ProgressEvent = dict[str, Any]

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
    # 234: the env-provenance capability. The daemon refuses a frozen payload to an image reporting less.
    env_provenance_version: int = 0


# 234: the version this image implements. Reported on GET /status as env_provenance_version.
ENV_PROVENANCE_CAPABILITY_VERSION = 2

_SHA256_HEX_LEN = 64
_SCRIPT_MAX_CHARS = 256 * 1024
# 265 (review): the bound a script is held to in bytes, the unit a wire size is measured in.
_SCRIPT_MAX_BYTES = 256 * 1024


class EnvProvenance(BaseModel):
    """234: the daemon-frozen env-cache steps a consumer runs. Nothing here is read from the cache.

    ``debs`` maps a cache-relative ``debs/<name>.deb`` to its sha256. The performer copies only these, and only
    when the bytes it reads hash to the listed digest.
    """

    model_config = ConfigDict(extra="forbid")

    # 234 review: the slug is derived from the cache directory name by provenance_slug (the daemon has the same
    # function). It may start with an underscore, and it is bounded to 120 characters.
    slug: str = Field(pattern=r"^[a-z0-9_][a-z0-9_-]{0,119}$")
    cache_path: str
    activate_sh: str = Field(max_length=_SCRIPT_MAX_CHARS)
    services_start_sh: str | None = Field(default=None, max_length=_SCRIPT_MAX_CHARS)
    services_health_sh: str | None = Field(default=None, max_length=_SCRIPT_MAX_CHARS)
    debs: dict[str, str] = Field(default_factory=dict)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: Literal[1, 2] = 1
    stat_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_entries: int = Field(default=DEFAULT_MAX_ENTRIES, ge=0, strict=True)
    max_bytes: int = Field(default=DEFAULT_MAX_BYTES, ge=0, strict=True)

    @field_validator("debs")
    @classmethod
    def _debs_stay_in_the_cache(cls, debs: dict[str, str]) -> dict[str, str]:
        for rel, digest in debs.items():
            parts = rel.split("/")
            if (
                not rel.startswith("debs/")
                or not rel.endswith(".deb")
                or any(p in ("", ".", "..") for p in parts)
                or len(digest) != _SHA256_HEX_LEN
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                msg = f"deb {rel!r} must be debs/<name>.deb with a sha256 digest"
                raise ValueError(msg)
        return debs


class BootstrapScripts(BaseModel):
    """265: the daemon-rendered verify.sh and activate.sh text for an env_bootstrap job.

    The bootstrap pod mounts a cache the daemon cannot read on Kubernetes, so the daemon sends the text it
    rendered. The performer writes it into the cache at job start and verifies it, never the agent's copy.
    """

    model_config = ConfigDict(extra="forbid")

    verify_sh: str = Field(min_length=1, max_length=_SCRIPT_MAX_CHARS)
    activate_sh: str = Field(min_length=1, max_length=_SCRIPT_MAX_CHARS)

    @field_validator("verify_sh", "activate_sh")
    @classmethod
    def _utf8_and_byte_bounded(cls, text: str) -> str:
        """265 (review): the bound is in bytes, and the text must encode as UTF-8 (a lone surrogate does not)."""
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            msg = "script is not valid UTF-8"
            raise ValueError(msg) from exc
        if len(data) > _SCRIPT_MAX_BYTES:
            msg = f"script is larger than {_SCRIPT_MAX_BYTES} bytes"
            raise ValueError(msg)
        return text


class JobInitPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    job_id: str
    card_id: str
    role: str
    backend: str
    persona: str
    # 151: transport-level URL. Was HttpUrl; relaxed to also carry the bench's
    # git:// loopback remote. The enforced trust boundary is Score.repo_url
    # (models.py), gated by ALLOW_INSECURE_REPO_URL — this only transports.
    repo_url: str
    branch: str
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    env_cache_path: str | None = None
    # 234: the frozen env-cache steps. None for a job with no cache mount.
    env_provenance: EnvProvenance | None = None
    # 265: the daemon-rendered bootstrap scripts. Set only on env_bootstrap jobs. An older image drops it.
    bootstrap_scripts: BootstrapScripts | None = None
    # 129: per-run transcript byte cap. None means transcripts are disabled for
    # this job, so the runner records nothing and the wire carries nothing.
    transcript_max_bytes: int | None = None

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url_scheme(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https", "git"} or not parsed.netloc:
            msg = f"repo_url must be an http(s):// or git:// URL with a host, got: {v!r}"
            raise ValueError(msg)
        return v


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
    events: list[ProgressEvent] = Field(default_factory=list)
    metrics: dict[str, Any] | None = None
    # 129: transcript delivery. ``transcript_last_seq`` is the highest seq the
    # performer holds (None when transcripts are disabled for this job).
    # ``transcript`` holds the records after the caller's cursor, bounded by
    # the request's transcript_limit. Both are empty unless requested.
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    transcript_last_seq: int | None = None
    # 129: set when the transcript batch could not be served. The status itself is
    # still returned; the batch is empty and the run carries on.
    transcript_error: str | None = None

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
    "ENV_PROVENANCE_CAPABILITY_VERSION",
    "EnvProvenance",
    "JobAcceptResponse",
    "JobBusyResponse",
    "JobInitPayload",
    "JobResult",
    "JobState",
    "JobStatus",
    "PerformerCapabilities",
    "PerformerStatus",
]
