"""Pydantic schema for services.json — the output artifact of the inference pass.

A `ServicesManifest` is either produced by the LLM agent (Phase 2) or by an
operator-authored `.coordinare/score.json` override (Phase 1). It feeds the
templater which renders services-{start,stop,health}.sh.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Service names flow into shell variable identifiers (`{{ name | upper }}_PID_FILE`)
# and into filesystem paths. Restrict to a bash-identifier charset — dashes
# would produce invalid identifiers like `REDIS-SERVER_PID_FILE`.
_SERVICE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Env-var names interpolated into shell as `${VAR:-}`; restrict to POSIX
# identifier rules so a malformed name cannot break the script.
_ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# agent_version is rendered into a comment header of every generated script
# and into the LLM system prompt; restrict to a tame charset so newlines or
# shell metacharacters cannot be smuggled in via env vars or manual override.
# Shared with render_system_prompt — see service_inference/prompt.py.
AGENT_VERSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class ServiceEntry(BaseModel):
    """One supportive service the project depends on (e.g. postgres, redis)."""

    name: str = Field(..., min_length=1, description="Logical service name, e.g. 'postgres'")
    binary: str = Field(
        ...,
        min_length=1,
        description="Executable name or absolute path. PATH-resolvable inside the performer.",
    )
    version: str = Field(..., min_length=1, description="Detected or required version string")
    data_dir: str = Field(
        ...,
        min_length=1,
        description="Filesystem path for service state, conventionally under $XDG_RUNTIME_DIR",
    )
    port: int = Field(..., ge=1, le=65535, description="TCP port the service listens on")
    why_needed: str = Field(
        ...,
        min_length=1,
        description="One-line human explanation used in diagnostics and logs",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Project paths the agent cited as evidence for this entry",
    )
    external_required: bool = Field(
        default=False,
        description="When true, this service cannot be hosted in-container; operator must supply connection env vars",
    )
    required_env_vars: list[str] = Field(
        default_factory=list,
        description="Env var names that must be set when external_required is true",
    )
    start_args: list[str] | None = Field(
        default=None,
        description=(
            "Optional argv-style override for the in-container service launch. "
            "When None the templater synthesises `<binary> --port=<port> --data-dir=<data_dir>`. "
            "When set, each element is shell-quoted before being emitted; this is a list "
            "of arguments, not a shell string — there is no way to inject shell metacharacters."
        ),
    )

    @field_validator("name", "binary", "data_dir")
    @classmethod
    def _no_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be whitespace-only")
        if "\x00" in value:
            raise ValueError("must not contain NUL bytes")
        return value

    @field_validator("name")
    @classmethod
    def _name_is_identifier_safe(cls, value: str) -> str:
        if not _SERVICE_NAME_RE.match(value):
            raise ValueError(
                "service name must match [a-z][a-z0-9_-]* — it is interpolated "
                "into shell variable names and filesystem paths"
            )
        return value

    @field_validator("required_env_vars")
    @classmethod
    def _env_vars_required_when_external(
        cls, value: list[str], info: Any
    ) -> list[str]:
        # Env var names are interpolated raw into bash as `${NAME:-}`; reject
        # anything that is not a POSIX identifier so the script stays valid
        # under all manifests.
        for v in value:
            if not v or not v.strip():
                raise ValueError("env var names must be non-empty")
            if not _ENV_VAR_RE.match(v):
                raise ValueError(
                    f"env var name {v!r} must match [A-Z_][A-Z0-9_]* — it is "
                    "interpolated verbatim into shell scripts"
                )
        return value

    @field_validator("start_args")
    @classmethod
    def _start_args_no_nulls(
        cls, value: list[str] | None
    ) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("start_args, when set, must be a non-empty argv list")
        for i, arg in enumerate(value):
            if not isinstance(arg, str):
                raise ValueError(f"start_args[{i}] must be a string")
            if "\x00" in arg:
                raise ValueError(f"start_args[{i}] contains a NUL byte")
        return value


class ServicesManifest(BaseModel):
    """Top-level services.json document."""

    services: list[ServiceEntry] = Field(default_factory=list)
    cache_inputs: list[str] = Field(
        default_factory=list,
        description="Project paths whose contents the agent read; SHA of these drives cache invalidation",
    )
    agent_version: str = Field(
        ...,
        min_length=1,
        description="Identifier for the agent/prompt revision that produced this manifest (or 'manual-override' for hand-authored)",
    )

    @field_validator("agent_version")
    @classmethod
    def _agent_version_safe(cls, value: str) -> str:
        if not AGENT_VERSION_RE.match(value):
            raise ValueError(
                "agent_version must match [A-Za-z0-9._:-]{1,64} — it is rendered "
                "into shell-script comments and into the LLM system prompt"
            )
        return value

    @field_validator("services")
    @classmethod
    def _check_external_entries_have_env_vars(
        cls, value: list[ServiceEntry]
    ) -> list[ServiceEntry]:
        for entry in value:
            if entry.external_required and not entry.required_env_vars:
                raise ValueError(
                    f"service '{entry.name}' is external_required but has no required_env_vars; "
                    "operators need to know which env vars to set"
                )
        return value

    @field_validator("services")
    @classmethod
    def _unique_service_names(
        cls, value: list[ServiceEntry]
    ) -> list[ServiceEntry]:
        names = [s.name for s in value]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate service names: {dupes}")
        return value


def manifest_json_schema() -> dict[str, Any]:
    """Return the JSON schema for `ServicesManifest`, used as the LLM structured-output target."""
    return ServicesManifest.model_json_schema()


def manifest_json_schema_str(indent: int = 2) -> str:
    """Return the manifest JSON schema as a formatted string (for docs/operator UI)."""
    return json.dumps(manifest_json_schema(), indent=indent, sort_keys=True)
