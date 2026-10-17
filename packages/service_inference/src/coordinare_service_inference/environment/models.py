"""Versioned, ecosystem-neutral environment execution contract."""

from __future__ import annotations

import hashlib
import json
import re
from graphlib import CycleError, TopologicalSorter
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

MAX_DOCUMENT_BYTES = 256 * 1024


def source_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or "\0" in value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
    ):
        raise ValueError("source must be a relative repository path without traversal")
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


class Command(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    argv: list[str] = Field(min_length=1)
    cwd: str = "{repo}"
    env: dict[str, str] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    required: bool = True
    scope: list[str] = Field(default_factory=list)

    @field_validator("env")
    @classmethod
    def valid_env(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in value):
            raise ValueError("invalid environment variable name")
        environment_order(value)
        for key, item in value.items():
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
        return value


class Requirement(StrictModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    source: str
    checks: list[str] = Field(min_length=1)
    required: bool = True
    _source = field_validator("source")(source_path)


class Activation(StrictModel):
    path: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
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
    source_commit: str = Field(min_length=1)
    inputs: dict[str, str]
    requirements: list[Requirement]
    install: list[Command]
    checks: list[Command] = Field(min_length=1)
    activation: Activation
    services: list[Service]
    empty_reason: str | None
    policy_version: Literal["351-v1"]

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
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
                raise ValueError("clean execution cannot depend on install-only evidence")
        graph = {command.id: list(command.depends_on) for command in commands}
        for service in self.services:
            stages = [command for command in [service.init, service.start, service.health] if command]
            for previous, current in zip(stages, stages[1:], strict=False):
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
                    raise ValueError("requirement must reference required executable checks")
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
    schema_version: Literal[1]
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(min_length=1)
    symphony_name: str = ""
    verdict: Literal["accept", "revise", "indeterminate"]
    rationale: str = Field(min_length=1)
    sources: list[str] = Field(min_length=1)
    inputs: dict[str, str] = Field(default_factory=dict)
    policy_version: Literal["351-v1"]

    @model_validator(mode="after")
    def validate_sources(self) -> Self:
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
    manifest_digest: str
    scope: str = ""
    results: list[CheckResult]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        required = [result for result in self.results if result.required]
        return bool(required) and all(result.status == "pass" for result in required)
