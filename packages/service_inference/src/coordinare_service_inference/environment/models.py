"""Versioned, ecosystem-neutral environment execution contract."""

from __future__ import annotations

import hashlib
import json
import platform as runtime_platform
import re
from graphlib import CycleError, TopologicalSorter
from itertools import pairwise
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError

from .evidence_safety import reject_literal_credentials, reject_nested_credentials, source_path
from .findings import (
    MAX_LEDGER_FINDINGS,
    FindingDecisionProposal,
    FindingLedger,
    FindingProposal,
    validate_finding_ledger,
)
from .runtime import (
    RUNTIME_OWNED_ENV,
    RuntimeArtifactReceipt,
    RuntimeEndpoint,
    RuntimePolicy,
    RuntimeSpec,
)
from .scope import ScopeContract, validate_scope_mapping

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
EXECUTION_PATH_PATTERN = r"^(?:/|\{(?:cache|repo|scratch)\}|\$\{[A-Za-z_][A-Za-z0-9_]*\})(?:[^\x00-\x1f\x7f\\]*)$"


def execution_path(value: str) -> str:
    """Execution roots are explicit; never normalize traversal into a new context."""
    root = re.match(r'^(?:/(?!/)|\{(?:cache|repo|scratch)\}|\$\{[A-Za-z_][A-Za-z0-9_]*\})', value)
    remaining = re.sub(r'\$\{[A-Za-z_][A-Za-z0-9_]*\}|\{(?:cache|repo|scratch)\}', '', value)
    if (not root or any(part in {'.', '..'} for part in value.split('/'))
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or '{' in remaining or '}' in remaining or '\\' in value):
        raise ValueError('execution path needs an absolute or declared root without traversal')
    return value


def environment_order(values: dict[str, str]) -> list[str]:
    """Deterministic ordering makes canonical mapping order execution-neutral."""
    pending = {
        key: set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)) & values.keys() - {key}
        for key, value in values.items()
    }
    ordered: list[str] = []
    while pending:
        ready = sorted(
            key for key, dependencies in pending.items() if not dependencies & pending.keys()
        )
        if not ready:
            raise ValueError("cyclic environment references")
        ordered.extend(ready)
        for key in ready:
            del pending[key]
    return ordered


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExecutionCapabilities(StrictModel):
    """Actual target identity and generic installer/consumer constraints."""

    schema_version: Literal[1, 2]
    runtime: RuntimePolicy | None = None
    image_ref: str = Field(pattern=r"^[^\s@]+@sha256:[0-9a-f]{64}$")
    platform: str = Field(pattern=r"^[a-z0-9_]+/[a-z0-9_]+$")
    cache_mount_path: str
    install_network: Literal["enabled", "disabled"]
    verify_network: Literal["disabled"]

    @model_validator(mode="after")
    def runtime_binding(self) -> Self:
        if self.schema_version == 1 and self.runtime is not None:
            raise ValueError("native capability version cannot declare a private runtime")
        if self.schema_version == 2:
            if self.runtime is None:
                raise ValueError("private runtime capability requires explicit runtime policy")
            identity = self.runtime.identity
            if (identity.carrier_image, identity.carrier_platform) != (self.image_ref, self.platform):
                raise ValueError("runtime carrier identity differs from capability target")
        return self

    @model_serializer(mode="wrap")
    def serialize_native_compatibly(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.schema_version == 1:
            data.pop("runtime", None)
        return data

    @field_validator("cache_mount_path")
    @classmethod
    def valid_cache_mount(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("cache mount must be absolute")
        return execution_path(value)

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Command(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    argv: list[str] = Field(min_length=1)
    cwd: str = Field(default="{repo}", description="Execution directory, normally {repo}; absolute or declared root required.",
                     json_schema_extra={"pattern": EXECUTION_PATH_PATTERN})
    env: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    required: bool = True
    scope: list[str] = Field(default_factory=list)

    _cwd = field_validator("cwd")(execution_path)

    @field_validator("env")
    @classmethod
    def valid_env(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in value):
            raise ValueError("invalid environment variable name")
        if any(key.startswith("COORDINARE_ENV_") for key in value):
            raise ValueError("secret-name policy is reserved for trusted orchestration")
        if {"BASH_ENV", "ENV"} & value.keys():
            raise ValueError("shell startup hooks are reserved for trusted orchestration")
        environment_order(value)
        for key, item in value.items():
            reject_literal_credentials(item)
            if (
                re.search(r"token|secret|password|credential|api.?key", key, re.IGNORECASE)
                and item != "${" + key + "}"
            ):
                raise PydanticCustomError("runtime_credential_reference", "secret variables must use named runtime references")
        return value

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: list[str]) -> list[str]:
        if not value[0] or any("\0" in item for item in value):
            raise ValueError("invalid executable or NUL in argument")
        for item in value:
            reject_literal_credentials(item)
        return value


class ProbeCommand(Command):
    """The command vocabulary restricted to independent required observations."""

    timeout_seconds: float = Field(default=30.0, gt=0, le=30)
    required: bool = Field(default=True, json_schema_extra={"const": True},
        description="Must be true even when the observed prerequisite may be absent.")
    scope: list[str] = Field(default_factory=list, max_length=0, description="Must be empty; probes are not scoped readiness checks.")
    depends_on: list[str] = Field(default_factory=list, max_length=0, description="Must be empty; each observation is independent.")

    @field_validator("required")
    @classmethod
    def observation_required(cls, value: bool) -> bool:
        if not value:
            raise PydanticCustomError("probe_required_true", "every requested observation must run; required must be true")
        return value


class Requirement(StrictModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source: str = Field(description="One exact repository file path already read and present in inputs; never comma-joined paths.")
    checks: list[str] = Field(min_length=1, description="IDs from checks[].id of required executable checks, never command strings or install-only IDs.")
    required: bool = True
    _source = field_validator("source")(source_path)


class Activation(StrictModel):
    path: list[str] = Field(default_factory=list, json_schema_extra={"items": {"type": "string", "pattern": EXECUTION_PATH_PATTERN}})
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator('path')
    @classmethod
    def valid_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            execution_path(path)
        return value

    @field_validator("env")
    @classmethod
    def valid_env(cls, value: dict[str, str]) -> dict[str, str]:
        checked = Command.valid_env(value)
        for key, item in checked.items():
            if '${' + key + '}' in item and item != '${' + key + '}':
                raise ValueError('activation self references must be idempotent')
        return checked


class Service(StrictModel):
    id: str = Field(min_length=1)
    start: Command
    health: Command
    stop: Command
    init: Command | None = None
    required: bool = True


RESERVED_CREDENTIAL_NAMES = frozenset({"BASH_ENV", "ENV", "PATH", "COORDINARE_REPO_DIR", "COORDINARE_SCRATCH_DIR"})
_CREDENTIAL_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class LocalCredential(StrictModel):
    """A credential for a service the manifest starts; the trusted runner generates its value per run."""

    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128)
    service: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=400)

    @field_validator("name")
    @classmethod
    def unreserved(cls, value: str) -> str:
        from .runtime import RUNTIME_OWNED_ENV

        if value in RESERVED_CREDENTIAL_NAMES or value in RUNTIME_OWNED_ENV or value.startswith("COORDINARE_ENV_"):
            raise ValueError("local credential name is reserved for trusted orchestration")
        return value

    @field_validator("purpose")
    @classmethod
    def no_literal(cls, value: str) -> str:
        reject_literal_credentials(value)
        # Purpose names what the credential is for; it never states a value.
        if re.search(r"(?:password|passwd|secret|token|key)\s*(?:=|:|\bis\s+(?!required\b|needed\b|generated\b))\s*\S"
                     r"|[\"`]|'[^']*'", value, re.IGNORECASE):
            raise PydanticCustomError("runtime_credential_reference", "purpose must describe the credential without a value")
        return value


def _references(command: Command) -> set[str]:
    return {name for text in [*command.argv, command.cwd, *command.env.values()]
            for name in _CREDENTIAL_REFERENCE.findall(text)}


class EnvironmentManifest(StrictModel):
    schema_version: Literal[1, 2, 3]
    scope_contract: ScopeContract | None = None
    scope_contract_digest: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    criterion_checks: dict[str, list[str]] = Field(default_factory=dict)
    runtime: RuntimeSpec | None = None
    symphony_name: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    inputs: dict[str, str]
    inventory_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capabilities_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requirements: list[Requirement]
    install: list[Command]
    checks: list[Command] = Field(min_length=1)
    activation: Activation
    services: list[Service]
    empty_reason: str | None
    policy_version: Literal["351-v1", "353-v2", "353-scope-v1"]
    local_credentials: list[LocalCredential] = Field(default_factory=list, max_length=32)

    @model_serializer(mode="wrap")
    def serialize_native_compatibly(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.schema_version == 1:
            data.pop("runtime", None)
        if self.schema_version < 3:
            for key in ('scope_contract', 'scope_contract_digest', 'criterion_checks'):
                data.pop(key, None)
        if not self.local_credentials:
            data.pop('local_credentials', None)
        return data

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.schema_version == 1 and (self.runtime is not None or self.policy_version != "351-v1"):
            raise ValueError("native manifest cannot declare private runtime")
        if self.schema_version == 2 and (self.runtime is None or self.policy_version != "353-v2"):
            raise ValueError("current manifest requires explicit runtime contract")
        if self.schema_version < 3 and (self.scope_contract is not None or self.scope_contract_digest is not None or self.criterion_checks):
            raise ValueError('legacy manifest cannot carry reviewed scope')
        if self.schema_version == 3:
            if self.policy_version != '353-scope-v1' or self.scope_contract is None:
                raise ValueError('current manifest requires independently reviewed source scope')
            validate_scope_mapping(self.scope_contract, self)
        reject_nested_credentials(self.model_dump())
        if len(self.model_dump_json().encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"manifest exceeds {MAX_DOCUMENT_BYTES // 1024 // 1024} MiB")
        for path, digest in self.inputs.items():
            source_path(path)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("input identity must be SHA256")
        commands = [*self.install, *self.checks]
        for service in self.services:
            commands.extend([service.start, service.health, service.stop])
            if service.init:
                commands.append(service.init)
        validate_records(self.install, self.checks, self.services, self.requirements, self.activation.env,
                         runtime=self.runtime is not None, reviewed_scope=self.schema_version == 3)
        validate_execution_graph(self.install, self.checks, self.services)
        checks = {check.id: check for check in self.checks}
        if not any(check.required for check in self.checks):
            raise PydanticCustomError("required_check_missing", "at least one required baseline check is mandatory")
        for requirement in self.requirements:
            if requirement.source not in self.inputs:
                raise _graph_error("requirement_source_uninspected", "requirement source was not inspected",
                                   requirement.id, set())
            for check_id in requirement.checks:
                if check_id not in checks or (
                    requirement.required and not checks[check_id].required
                ):
                    raise PydanticCustomError("requirement_check_reference", "requirement must reference required executable checks")
        if not self.requirements and not (self.empty_reason and self.empty_reason.strip()):
            raise PydanticCustomError("empty_reason_missing", "empty environment requires justification")
        self._validate_local_credentials(commands)
        return self

    def _validate_local_credentials(self, commands: list[Command]) -> None:
        names = [credential.name for credential in self.local_credentials]
        service_ids = {service.id for service in self.services}
        if len(names) != len(set(names)) or any(c.service not in service_ids for c in self.local_credentials):
            raise PydanticCustomError("local_credential_invalid",
                                      "local credentials need unique names and a service this manifest starts")
        if not names:
            return
        # Activation also applies to install commands, so it cannot carry generated credentials either.
        installed = {name for command in self.install for name in _references(command)}
        installed |= {name for text in self.activation.env.values() for name in _CREDENTIAL_REFERENCE.findall(text)}
        if set(names) & installed:
            raise PydanticCustomError("local_credential_in_install",
                                      "install commands and activation cannot use generated credentials")
        consumers = commands[len(self.install):]
        used = {name for command in consumers for name in _references(command)}
        # Data that stores a per-run value lives in {scratch}; {cache} reaches tools only through activation PATH.
        # A lexical guard: it also follows activation variables that point into {cache}, but not scripts' own writes.
        cached = [key for key, value in self.activation.env.items() if key != "PATH" and "{cache}" in value]
        for command in consumers:
            texts = [*command.argv, command.cwd, *(value for key, value in command.env.items() if key != "PATH")]
            via_activation = any(re.search(r"\$(?:\{" + re.escape(key) + r"\}|" + re.escape(key) + r"(?![A-Za-z0-9_]))", text)
                                 for key in cached for text in texts)
            if set(names) & _references(command) and (via_activation or any("{cache}" in text for text in texts)):
                raise PydanticCustomError("local_credential_cache_data",
                                          "commands using generated credentials cannot reference {cache}")
        if set(names) - used:
            raise PydanticCustomError("local_credential_unused", "every local credential must be referenced as ${NAME}")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )


class CoverageAssessment(StrictModel):
    finding_ledger: FindingLedger | None = None
    finding_ledger_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    inventory_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    capabilities_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal[1, 2]
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    symphony_name: str = ""
    verdict: Literal["accept", "revise", "indeterminate"]
    rationale: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1)
    inputs: dict[str, str] = Field(default_factory=dict)
    policy_version: Literal["351-v1", "353-findings-v1"]

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
        if self.schema_version == 1:
            if self.policy_version != '351-v1' or self.finding_ledger is not None or self.finding_ledger_digest is not None:
                raise ValueError('legacy review cannot carry current findings')
        else:
            if (self.policy_version != '353-findings-v1' or self.finding_ledger is None
                    or self.finding_ledger_digest != self.finding_ledger.digest()):
                raise ValueError('current coverage review requires matching finding evidence')
            if self.verdict == 'accept':
                validate_finding_ledger(self.finding_ledger, manifest_digest=self.manifest_digest,
                                       source_commit=self.source_commit, inputs=self.inputs)
        reject_nested_credentials(self.model_dump())
        for path in [*self.sources, *self.inputs]:
            source_path(path)
        if len(self.model_dump_json().encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError(f"assessment exceeds {MAX_DOCUMENT_BYTES // 1024 // 1024} MiB")
        return self


class CheckResult(StrictModel):
    id: str
    status: Literal["pass", "fail", "indeterminate", "not_run"]
    required: bool
    exit_code: int | None = None
    duration_seconds: float = 0
    output: str = ""
    output_truncated: bool = False
    manifest_digest: str = ""
    context: str = ""


class VerificationReport(StrictModel):
    runtime_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_phase: Literal["installer", "consumer", "live_consumer"] | None = None
    runtime_artifacts: RuntimeArtifactReceipt | None = None
    runtime_cleanup: bool | None = None

    @model_serializer(mode="wrap")
    def serialize_native_compatibly(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.runtime_digest is None:
            for key in ("runtime_digest", "runtime_phase", "runtime_artifacts", "runtime_cleanup"):
                data.pop(key, None)
        return data

    execution_source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    platform: str = Field(default_factory=lambda: runtime_platform.system().lower() + "/" + runtime_platform.machine().lower())
    manifest_digest: str
    scope: str = ""
    results: list[CheckResult]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        required = [result for result in self.results if result.required]
        return bool(required) and all(result.status == "pass" for result in required)


GRAPH_IDS = 8
MAX_COMMANDS = 4096


def _graph_error(code: str, message: str, command_id: str, unavailable: set[str]) -> PydanticCustomError:
    """Names only schema-validated command ids, so a correction can say which edge to remove."""
    return PydanticCustomError(code, message, {'command_id': command_id, 'unavailable': sorted(unavailable)[:GRAPH_IDS]})


def validate_records(install: list[Command], checks: list[Command], services: list[Service],
                     requirements: list[Requirement], activation_env: dict[str, str], *, runtime: bool,
                     reviewed_scope: bool = True) -> None:
    """Rules on records and their ids that one author can break alone; each names the records it rejects."""
    commands = [*install, *checks, *(command for service in services
                                     for command in [service.start, service.health, service.stop, service.init] if command)]
    if runtime:
        for command in commands:
            if reserved := RUNTIME_OWNED_ENV & command.env.keys():
                raise _graph_error('runtime_env_reserved', 'private runtime control environment cannot be overridden',
                                   command.id, set(reserved))
        if reserved := RUNTIME_OWNED_ENV & activation_env.keys():
            raise _graph_error('runtime_env_reserved', 'private runtime control environment cannot be overridden',
                               'activation', set(reserved))
    ids = [command.id for command in commands]
    if (repeated := {identifier for identifier in ids if ids.count(identifier) > 1}) or len(commands) > MAX_COMMANDS:
        raise _graph_error('duplicate_command_ids', 'duplicate command IDs or excessive commands', '', repeated)
    names = [requirement.id for requirement in requirements]
    if repeated := {identifier for identifier in names if names.count(identifier) > 1}:
        raise _graph_error('duplicate_requirement_ids', 'duplicate requirement IDs', '', repeated)
    for service in services:
        lifecycle = [command for command in [service.init, service.start, service.health, service.stop] if command]
        if differing := {command.id for command in lifecycle if command.scope != service.start.scope}:
            raise _graph_error('service_scope_mismatch', 'service lifecycle commands must share applicability scope',
                               service.id, differing)
        if reviewed_scope and service.required and (optional := {command.id for command in lifecycle if not command.required}):
            raise _graph_error('scope_required_service', 'required service lifecycle cannot be optional',
                               service.id, optional)


def validate_execution_graph(install: list[Command], checks: list[Command], services: list[Service]) -> None:
    """Prerequisite rules per execution phase; raises a correctable error naming the offending command."""
    if len({service.id for service in services}) != len(services):
        raise PydanticCustomError('duplicate_service_ids', 'duplicate service IDs')
    install_ids = {command.id for command in install}
    lifecycle = [[command for command in [service.init, service.start, service.health] if command] for service in services]
    readiness_ids = {command.id for own in lifecycle for command in own}
    check_ids = {command.id for command in checks}
    clean = [*checks, *(command for service in services
                        for command in [service.start, service.health, service.stop, service.init] if command)]
    for command in clean:
        if install_ids.intersection(command.depends_on):
            raise _graph_error('clean_dependency_install_only', 'clean execution cannot depend on install-only evidence',
                               command.id, install_ids.intersection(command.depends_on))
    graph = {command.id: list(command.depends_on) for command in [*install, *clean]}
    for own in lifecycle:
        for previous, current in pairwise(own):
            graph[current.id].append(previous.id)
    for command_id, dependencies in graph.items():
        if unknown := set(dependencies) - graph.keys():
            raise _graph_error('missing_prerequisite', 'missing prerequisite', command_id, unknown)
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        cycle = sorted({str(node) for node in exc.args[1]}) if len(exc.args) > 1 else []
        raise _graph_error('cyclic_prerequisites', 'cyclic prerequisites', cycle[0] if cycle else '', set(cycle)) from exc
    for command in install:
        if outside := set(command.depends_on) - install_ids:
            raise _graph_error('install_dependency_phase', 'install actions may only depend on install actions',
                               command.id, outside)
    for command in checks:
        if outside := set(command.depends_on) - readiness_ids - check_ids:
            raise _graph_error('check_dependency_phase', 'checks require consumer-executable prerequisites',
                               command.id, outside)
    for own in lifecycle:
        earlier = readiness_ids - {command.id for command in own}
        for command in own:
            if outside := set(command.depends_on) - earlier:
                raise _graph_error('service_dependency_phase', 'service lifecycle depends on unavailable execution phase',
                                   command.id, outside)
            earlier.add(command.id)


class EnvironmentManifestProposal(StrictModel):
    """Model-owned behavior only; immutable provenance is assembled by the daemon."""

    runtime_endpoints: list[RuntimeEndpoint] = Field(default_factory=list, max_length=32)
    criterion_checks: dict[str, list[str]] = Field(default_factory=dict)
    requirements: list[Requirement]
    install: list[Command]
    checks: list[Command] = Field(min_length=1)
    activation: Activation
    services: list[Service]
    empty_reason: str | None
    local_credentials: list[LocalCredential] = Field(default_factory=list, max_length=32)


class ShardChecks(StrictModel):
    """One check author's required checks and criterion mappings, merged by the daemon into one manifest."""

    checks: list[Command] = Field(max_length=1024)
    criterion_checks: dict[str, list[str]] = Field(default_factory=dict)


class ShardChecksEnvelope(StrictModel):
    shard_checks: ShardChecks


class CoverageAssessmentProposal(StrictModel):
    """A judgment about the exact manifest supplied in this isolated conversation."""

    findings: list[FindingProposal] = Field(max_length=MAX_LEDGER_FINDINGS)
    decisions: list[FindingDecisionProposal] = Field(max_length=MAX_LEDGER_FINDINGS)
    capability_unavailable: bool
    verdict: Literal["accept", "revise", "indeterminate"]
    rationale: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1)


def validate_current_coverage(manifest: EnvironmentManifest, assessment: CoverageAssessment) -> None:
    """One policy gate shared by installer admission and persisted readiness."""
    manifest = EnvironmentManifest.model_validate(manifest.model_dump(mode='json'))
    if manifest.schema_version != 3 or manifest.policy_version != '353-scope-v1':
        raise ValueError('current independent source scope requires refresh')
    review = CoverageAssessment.model_validate(assessment.model_dump(mode='json'))
    if (review.schema_version != 2 or review.policy_version != '353-findings-v1'
            or review.verdict != 'accept' or review.manifest_digest != manifest.digest()
            or review.source_commit != manifest.source_commit
            or (review.symphony_name and review.symphony_name != manifest.symphony_name)
            or not manifest.inventory_digest or review.inventory_digest != manifest.inventory_digest
            or not manifest.capabilities_digest or review.capabilities_digest != manifest.capabilities_digest
            or not review.inputs or any(review.inputs.get(path) != digest for path, digest in manifest.inputs.items())
            or any(path not in review.inputs for path in review.sources)
            or review.finding_ledger is None or review.finding_ledger_digest != review.finding_ledger.digest()):
        raise ValueError('current independent coverage findings require refresh')
    validate_finding_ledger(review.finding_ledger, manifest_digest=manifest.digest(),
                            source_commit=manifest.source_commit, inputs=review.inputs)
