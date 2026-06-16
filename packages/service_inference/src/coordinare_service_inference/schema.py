"""Pydantic schema for services.json — the output artifact of the inference pass.

A `ServicesManifest` is either produced by the LLM agent (Phase 2) or by an
operator-authored `.coordinare/score.json` override (Phase 1). It feeds the
templater which renders services-{start,stop,health}.sh.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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

# Kinds whose server binary the coordinare installs *from this manifest* during
# env-bootstrap (spec-091 US2/FR-006): the deb is fetched into the cache by the
# persona-driven install block, so the binary is NOT expected to be present at
# inference time. Mirrors coordinare's `_SERVICE_KIND_PACKAGES` (env_manifest.py)
# — the source of truth for which kinds coordinare knows how to install. Used by
# the agent's PATH-resolution check to skip these kinds (the chicken-and-egg:
# the inference pass that *describes* the service runs before coordinare has
# installed it).
COORDINARE_MANAGED_KINDS = frozenset({"postgres", "redis"})


class ServiceInit(BaseModel):
    """First-run initialization parameters for a stateful service (spec 091).

    Carries *parameters only* — never the init recipe (the coordinare owns that,
    keyed by `ServiceEntry.kind`) and never a literal secret. The only credential
    channel is `password_env_var`, which names an env var read at runtime; the
    secret value is never stored in the declaration, in argv, or in logs (D5, FR-011).
    """

    model_config = {"extra": "forbid"}

    superuser: str = Field(
        ...,
        description="Admin/superuser role name to create on first run (e.g. 'root'). Identifier-safe.",
    )
    databases: list[str] = Field(
        default_factory=list,
        description="Databases to create on first run; each created independently and idempotently.",
    )
    password_env_var: str | None = Field(
        default=None,
        description=(
            "Name of the env var holding the admin secret. The recipe references "
            "${<this var>}; the secret value is never stored in the declaration, "
            "argv, or logs. Must NOT be the secret value itself."
        ),
    )

    @field_validator("superuser")
    @classmethod
    def _superuser_identifier_safe(cls, value: str) -> str:
        # VR-5: superuser is interpolated into the role-creation step; restrict to
        # the same identifier-safe charset as service names so it cannot break out.
        if not _SERVICE_NAME_RE.match(value):
            raise ValueError(
                "superuser must match [a-z][a-z0-9_]* — it is interpolated into "
                "the role-creation step of the init recipe"
            )
        return value

    @field_validator("databases")
    @classmethod
    def _databases_identifier_safe(cls, value: list[str]) -> list[str]:
        # VR-6: each database name is interpolated into the create-db step.
        for db in value:
            if not db or not db.strip():
                raise ValueError("database names must be non-empty")
            if not _SERVICE_NAME_RE.match(db):
                raise ValueError(
                    f"database name {db!r} must match [a-z][a-z0-9_]* — it is "
                    "interpolated into the create-database step of the init recipe"
                )
        return value

    @field_validator("password_env_var")
    @classmethod
    def _password_env_var_safe(cls, value: str | None) -> str | None:
        # VR-7: when set, this names an env var read as ${VAR}; restrict to a
        # POSIX identifier so the recipe stays valid. It is NOT the secret value.
        if value is None:
            return value
        if not _ENV_VAR_RE.match(value):
            raise ValueError(
                f"password_env_var {value!r} must match [A-Z_][A-Z0-9_]* — it "
                "names the env var holding the secret, not the secret itself"
            )
        return value


class ServiceEntry(BaseModel):
    """One supportive service the project depends on (e.g. postgres, redis)."""

    name: str = Field(..., min_length=1, description="Logical service name, e.g. 'postgres'")
    binary: str = Field(
        ...,
        min_length=1,
        description="Executable name or absolute path. PATH-resolvable inside the performer.",
    )
    version: str | None = Field(
        default=None,
        description=(
            "Detected or required version string, when known. Optional: for a "
            "coordinare-managed stateful kind (postgres/redis) the binary is not "
            "installed at inference time, so the version cannot be probed — leave "
            "it null rather than guessing. Purely diagnostic; not consumed by the "
            "templater or the cache key."
        ),
    )
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
    kind: Literal["generic", "postgres", "redis"] = Field(
        default="generic",
        description=(
            "Selects the coordinare-owned init recipe and readiness probe. "
            "'generic' = no init (063 behavior, today's default). 'postgres' = "
            "initdb → create superuser → create databases, with a pg_isready "
            "readiness probe. 'redis' = no init, liveness/port readiness. NEW in 091."
        ),
    )
    init: ServiceInit | None = Field(
        default=None,
        description=(
            "First-run initialization parameters. Only meaningful for kinds that "
            "initialize (currently 'postgres'). When None the service starts with no "
            "init step, exactly as before 091. NEW in 091."
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

    @field_validator("version")
    @classmethod
    def _version_non_empty_when_provided(cls, value: str | None) -> str | None:
        # `version` is optional (None when unprobeable), but an explicit empty or
        # whitespace-only string is an authoring mistake — reject it rather than
        # silently rendering a blank version into diagnostics.
        if value is None:
            return value
        if not value.strip():
            raise ValueError("version, when provided, must not be empty or whitespace-only")
        if "\x00" in value:
            raise ValueError("version must not contain NUL bytes")
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

    @model_validator(mode="after")
    def _init_requires_initializing_kind(self) -> ServiceEntry:
        # VR-2: an `init` block is only meaningful for a kind that initializes.
        # Declaring `init` on 'generic'/'redis' is an authoring mistake, not a
        # silent no-op — surface it as a load-time error. (VR-1, the closed set
        # of kinds, is enforced by the Literal type.)
        if self.init is not None and self.kind != "postgres":
            raise ValueError(
                f"service '{self.name}' declares an init block but kind is "
                f"'{self.kind}'; init is only valid for an initializing kind "
                "(currently 'postgres')"
            )
        return self


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


# VR-2 expressed as a JSON-schema conditional: when `init` is present, `kind`
# must be 'postgres'. Pydantic enforces this at load time via
# `_init_requires_initializing_kind`, but the emitted schema (consumed by the
# LLM structured-output target and operator docs) must carry the constraint too,
# matching contracts/services-manifest.schema.json.
_INIT_REQUIRES_POSTGRES = {
    "$comment": "VR-2: init only valid for an initializing kind.",
    "if": {"properties": {"init": {"type": "object"}}, "required": ["init"]},
    "then": {"properties": {"kind": {"const": "postgres"}}},
}


def manifest_json_schema() -> dict[str, Any]:
    """Return the JSON schema for `ServicesManifest`, used as the LLM structured-output target.

    Pydantic emits `kind` and `init` from the field definitions automatically; the
    VR-2 `init`-requires-`kind=postgres` conditional is enforced at load time by a
    model validator (not expressible as a single field constraint), so it is grafted
    onto the emitted `ServiceEntry` schema here to match the published contract.
    """
    schema = ServicesManifest.model_json_schema()
    entry_schema = schema.get("$defs", {}).get("ServiceEntry")
    if entry_schema is not None:
        entry_schema.setdefault("allOf", []).append(_INIT_REQUIRES_POSTGRES)
    return schema


def manifest_json_schema_str(indent: int = 2) -> str:
    """Return the manifest JSON schema as a formatted string (for docs/operator UI)."""
    return json.dumps(manifest_json_schema(), indent=indent, sort_keys=True)
