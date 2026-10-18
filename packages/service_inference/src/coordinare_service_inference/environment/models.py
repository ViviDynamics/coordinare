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
    FindingDecisionProposal,
    FindingLedger,
    FindingProposal,
    validate_finding_ledger,
)
from .runtime import RuntimePolicy  # noqa: TC001 - pydantic resolves this field at runtime

MAX_DOCUMENT_BYTES = 256 * 1024
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
                raise ValueError("secret variables must use named runtime references")
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


class EnvironmentManifest(StrictModel):
    schema_version: Literal[1]
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
    policy_version: Literal["351-v1"]

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        reject_nested_credentials(self.model_dump())
        if len(self.model_dump_json().encode()) > MAX_DOCUMENT_BYTES:
            raise ValueError("manifest exceeds 256 KiB")
        for path, digest in self.inputs.items():
            source_path(path)
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("input identity must be SHA256")
        commands = [*self.install, *self.checks]
        for service in self.services:
            commands.extend([service.start, service.health, service.stop])
            if service.init:
                commands.append(service.init)
        ids = [command.id for command in commands]
        if len(ids) != len(set(ids)) or len(commands) > 4096:
            raise ValueError("duplicate command IDs or excessive commands")
        if len({r.id for r in self.requirements}) != len(self.requirements):
            raise ValueError("duplicate requirement IDs")
        if len({s.id for s in self.services}) != len(self.services):
            raise ValueError("duplicate service IDs")
        install_ids = {command.id for command in self.install}
        for command in commands[len(self.install) :]:
            if install_ids.intersection(command.depends_on):
                raise PydanticCustomError("clean_dependency_install_only", "clean execution cannot depend on install-only evidence")
        graph = {command.id: list(command.depends_on) for command in commands}
        for service in self.services:
            stages = [command for command in [service.init, service.start, service.health] if command]
            for previous, current in pairwise(stages):
                graph[current.id].append(previous.id)
        if any(dependency not in graph for deps in graph.values() for dependency in deps):
            raise ValueError('missing prerequisite')
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise ValueError('cyclic prerequisites') from exc
        readiness_ids = {command.id for service in self.services
                         for command in [service.init, service.start, service.health] if command}
        check_ids = {command.id for command in self.checks}
        for command in self.install:
            if set(command.depends_on) - install_ids:
                raise ValueError('install actions may only depend on install actions')
        for command in self.checks:
            if set(command.depends_on) - readiness_ids - check_ids:
                raise ValueError('checks require consumer-executable prerequisites')
        for service in self.services:
            earlier = set(readiness_ids)
            own = [command for command in [service.init, service.start, service.health] if command]
            earlier.difference_update(command.id for command in own)
            for command in own:
                if set(command.depends_on) - earlier:
                    raise ValueError('service lifecycle depends on unavailable execution phase')
                earlier.add(command.id)
            if any(command.scope != service.start.scope for command in [*own, service.stop]):
                raise ValueError('service lifecycle commands must share applicability scope')
        checks = {check.id: check for check in self.checks}
        if not any(check.required for check in self.checks):
            raise ValueError("at least one required baseline check is mandatory")
        for requirement in self.requirements:
            if requirement.source not in self.inputs:
                raise ValueError("requirement source was not inspected")
            for check_id in requirement.checks:
                if check_id not in checks or (
                    requirement.required and not checks[check_id].required
                ):
                    raise PydanticCustomError("requirement_check_reference", "requirement must reference required executable checks")
        if not self.requirements and not (self.empty_reason and self.empty_reason.strip()):
            raise ValueError("empty environment requires justification")
        return self

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
            raise ValueError("assessment exceeds 256 KiB")
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


class EnvironmentManifestProposal(StrictModel):
    """Model-owned behavior only; immutable provenance is assembled by the daemon."""

    requirements: list[Requirement]
    install: list[Command]
    checks: list[Command] = Field(min_length=1)
    activation: Activation
    services: list[Service]
    empty_reason: str | None


class CoverageAssessmentProposal(StrictModel):
    """A judgment about the exact manifest supplied in this isolated conversation."""

    findings: list[FindingProposal] = Field(max_length=32)
    decisions: list[FindingDecisionProposal] = Field(max_length=32)
    capability_unavailable: bool
    verdict: Literal["accept", "revise", "indeterminate"]
    rationale: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1)


def validate_current_coverage(manifest: EnvironmentManifest, assessment: CoverageAssessment) -> None:
    """One policy gate shared by installer admission and persisted readiness."""
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
