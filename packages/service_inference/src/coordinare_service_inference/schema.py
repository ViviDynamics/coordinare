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
# env-bootstrap (spec-091 US2/FR-006, widened by issue 413): the deb is fetched
# into the cache by the persona-driven install block, so the binary is NOT
# expected to be present at inference time. Mirrors coordinare's
# `_SERVICE_KIND_PACKAGES` (env_manifest.py) — the source of truth for which
# kinds coordinare knows how to install. Used by the agent's PATH-resolution
# check to skip these kinds (the chicken-and-egg: the inference pass that
# *describes* the service runs before coordinare has installed it).
# Only 'generic' is NOT here: a generic service brings its own binary via the
# project spec files, and an external_required service lives elsewhere entirely.
COORDINARE_MANAGED_KINDS = frozenset({
    "postgres",
    "redis",
    "mysql",
    "mongodb",
    "rabbitmq",
    "elasticsearch",
    "memcached",
    "minio",
})

# The managed `kind` set: every kind with a coordinare-owned launch template and
# a protocol-level readiness probe. 'generic' is the escape hatch — it launches
# the manifest's own start_args and is probed via its health_command, so an
# in-container generic service MUST carry both (see the model validator below).
MANAGED_SERVICE_KINDS = tuple(sorted(COORDINARE_MANAGED_KINDS))


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
            "Argv-style launch for the in-container service. REQUIRED for an "
            "in-container generic service (issue 413: the redis-shaped "
            "`<binary> --port=<port> --data-dir=<data_dir>` default is removed "
            "— every managed kind has its own coordinare-owned launch template, "
            "and generic must say exactly how to launch it). When set, each "
            "element is shell-quoted before being emitted; this is a list of "
            "arguments, not a shell string — there is no way to inject shell "
            "metacharacters."
        ),
    )
    health_command: list[str] | None = Field(
        default=None,
        description=(
            "Argv-style readiness probe for the service, exit 0 = ready. "
            "REQUIRED for an in-container generic service (issue 413): the "
            "coordinare-owned kinds have protocol-level probes built in "
            "(pg_isready, redis-cli PING, mariadb-admin ping, mongosh, "
            "rabbitmq-diagnostics, curl against the health endpoint), so "
            'generic is the only kind that needs to spell its own. Rendered '
            "into services-health.sh and polled during start readiness."
        ),
    )
    kind: Literal[
        "generic",
        "postgres",
        "redis",
        "mysql",
        "mongodb",
        "rabbitmq",
        "elasticsearch",
        "memcached",
        "minio",
    ] = Field(
        default="generic",
        description=(
            "Selects the coordinare-owned init recipe, launch template and "
            "protocol-level readiness probe (issue 413 widened the set beyond "
            "postgres/redis). 'generic' = no coordinare recipe: the launch is "
            "the manifest's own start_args and readiness is its health_command "
            "(both REQUIRED in-container). 'postgres' = initdb → create "
            "superuser → create databases, with a pg_isready probe. "
            "'mysql' covers mariadb (same protocol). 'elasticsearch' covers "
            "opensearch."
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

    @field_validator("start_args", "health_command")
    @classmethod
    def _start_args_no_nulls(
        cls, value: list[str] | None
    ) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("when set, must be a non-empty argv list")
        for i, arg in enumerate(value):
            if not isinstance(arg, str):
                raise ValueError(f"element {i} must be a string")
            if "\x00" in arg:
                raise ValueError(f"element {i} contains a NUL byte")
        return value

    @model_validator(mode="after")
    def _init_requires_initializing_kind(self) -> ServiceEntry:
        # VR-2: an `init` block is only meaningful for a kind that initializes.
        # Declaring `init` on anything but 'postgres' is an authoring mistake,
        # not a silent no-op — surface it as a load-time error. (VR-1, the
        # closed set of kinds, is enforced by the Literal type.)
        if self.init is not None and self.kind != "postgres":
            raise ValueError(
                f"service '{self.name}' declares an init block but kind is "
                f"'{self.kind}'; init is only valid for an initializing kind "
                "(currently 'postgres')"
            )
        return self

    @model_validator(mode="after")
    def _generic_requires_launch_and_probe(self) -> ServiceEntry:
        # Issue 413: the redis-shaped default launch
        # (`<binary> --port=<port> --data-dir=<data_dir>`) is removed. Every
        # managed kind has a coordinare-owned launch template and protocol-level
        # probe, so an in-container generic service must declare its own
        # start_args AND health_command — an external_required service launches
        # nothing, so the contract does not apply to it.
        if self.kind == "generic" and not self.external_required:
            missing = [
                field
                for field, value in (
                    ("start_args", self.start_args),
                    ("health_command", self.health_command),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    f"in-container generic service '{self.name}' is missing "
                    f"{', '.join(missing)}; the templater no longer synthesises "
                    "a default launch — emit start_args (how to launch the "
                    "daemon) and health_command (a protocol-level readiness "
                    "probe, exit 0 = ready), or let coordinare manage it with "
                    "a managed kind"
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
    test_env_source: str | None = Field(
        default=None,
        description=(
            "spec-092 fallback: repo-relative PATH to a dotenv-style test-env file "
            "the agent discovered (e.g. '.coordinare/test.env'). PATH ONLY — never "
            "literal KEY=VALUE secrets. Coordinare parses it through the same loader "
            "(containment-checked at read time) when no config-level test_env block "
            "is set. Omit when none is found or when config provides the file."
        ),
    )

    @field_validator("test_env_source")
    @classmethod
    def _test_env_source_no_nulls(cls, value: str | None) -> str | None:
        # Parity with ServiceEntry.data_dir: this path flows into filesystem
        # resolution, where an embedded NUL would truncate silently.
        if value is not None and "\x00" in value:
            raise ValueError("test_env_source must not contain NUL bytes")
        return value

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


def services_requiring_inference_validation(
    manifest: ServicesManifest,
) -> list[ServiceEntry]:
    """Return the services that should be exercised by inference-time
    run-validation (start → health → stop).

    Spec 104: a coordinare-managed stateful service (``kind`` in
    :data:`COORDINARE_MANAGED_KINDS`) has its server binary installed by
    env-bootstrap *from this manifest* (spec 091/102) and its readiness verified
    by spec-101's gate at bootstrap — so it is NOT present at inference time and
    must not be started here (doing so spins until the validator's subprocess
    timeout, the chicken-and-egg that returned ``services=[]``). Only these
    coordinare-managed kinds are excluded.

    External-required services ARE still validated: their rendered start script
    only asserts the declared ``required_env_vars`` are present (it never invokes
    the external binary), so the check is cheap, meaningful (it surfaces missing
    operator config), and cannot hang. Generic in-container services are likewise
    validated as before — and per issue 413 they now REQUIRE ``start_args`` +
    ``health_command`` at load time (no synthesised default launch), so the
    validator exercises the launch the templater actually emitted.

    Issue 413 widened the managed set beyond postgres/redis (see
    :data:`COORDINARE_MANAGED_KINDS`); the exclusion above is kind-driven, so the
    newly added managed kinds are excluded by the same rule without special
    casing.
    """
    return [svc for svc in manifest.services if svc.kind not in COORDINARE_MANAGED_KINDS]
