"""Ecosystem-neutral runtime observations, separate from environment acceptance."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Literal, Self

from coordinare_service_inference.environment.models import (
    Command,
    ExecutionCapabilities,
    StrictModel,
)
from pydantic import Field, model_validator

from coordinare.services.environment_attempt import EnvironmentDiscoveryError


class ProbeRequest(StrictModel):
    schema_version: Literal[1]
    commands: list[Command] = Field(min_length=1, max_length=16)
    citations: dict[str, list[str]]
    questions: dict[str, str]

    @model_validator(mode="after")
    def valid_commands(self) -> Self:
        ids = {command.id for command in self.commands}
        if len(ids) != len(self.commands) or set(self.citations) != ids or set(self.questions) != ids:
            raise ValueError("probe IDs, citations and questions must match uniquely")
        for command in self.commands:
            if (not command.required or command.scope or command.depends_on or command.timeout_seconds > 30
                    or not self.citations[command.id] or not 1 <= len(self.questions[command.id].strip()) <= 512):
                raise ValueError("probe requires bounded independent commands and cited questions")
        return self


class ProbePlan(StrictModel):
    request: ProbeRequest
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: dict[str, str]
    capabilities: ExecutionCapabilities

    @model_validator(mode="after")
    def inspected_citations(self) -> Self:
        if any(path not in self.inputs for paths in self.request.citations.values() for path in paths):
            raise ValueError("probe citations must be inspected source inputs")
        if any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value) for value in self.inputs.values()):
            raise ValueError("probe input hashes must be actual SHA-256 values")
        return self

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ProbeCommandResult(StrictModel):
    id: str
    status: Literal["completed", "timed_out", "execution_error"]
    exit_code: int | None = None
    output: str
    truncated: bool

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if (self.status == "completed") != (self.exit_code is not None) or len(self.output.encode()) > 16 * 1024:
            raise ValueError("probe exit status or output bound is invalid")
        return self


class ProbeReport(StrictModel):
    schema_version: Literal[1]
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str
    image_ref: str
    platform: str
    results: list[ProbeCommandResult] = Field(min_length=1, max_length=16)
    cleanup_confirmed: bool


ProbeExecutor = Callable[[ProbePlan], Awaitable[ProbeReport]]


def validate_probe_report(plan: ProbePlan, report: ProbeReport) -> None:
    if (report.plan_digest != plan.digest() or report.source_commit != plan.source_commit
            or report.image_ref != plan.capabilities.image_ref or report.platform != plan.capabilities.platform
            or [result.id for result in report.results] != [command.id for command in plan.request.commands]
            or not report.cleanup_confirmed):
        message = "probe evidence identity, completeness or cleanup is invalid"
        raise EnvironmentDiscoveryError(message, code="probe_report_invalid")
    if any(result.status != "completed" or result.truncated for result in report.results):
        message = "probe execution is incomplete or output was truncated"
        raise EnvironmentDiscoveryError(message, code="probe_observation_incomplete")
