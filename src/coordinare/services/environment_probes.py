"""Ecosystem-neutral runtime observations, separate from environment acceptance."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Self

from coordinare_service_inference.environment.models import (
    ExecutionCapabilities,  # noqa: TC002 - pydantic resolves field types at runtime
    ProbeCommand,  # noqa: TC002 - pydantic resolves field types at runtime
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)
from pydantic_core import PydanticCustomError

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

MAX_PROBE_SECONDS = 30
MAX_QUESTION_CHARS = 512
SHA256_LENGTH = 64


class ProbeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ProbeRequest(ProbeBase):
    schema_version: Literal[1]
    execution_context: Literal['outer', 'private_service'] = Field(default='outer',
        description='Use outer tools by default; private_service explicitly requests the configured platform-only runtime, never an installed project cache.')
    commands: list[ProbeCommand] = Field(min_length=1, max_length=16)
    citations: dict[str, list[str]] = Field(description="Exactly one key per commands[].id, each with nonempty inspected-source paths.")
    questions: dict[str, str] = Field(description="Exactly one key per commands[].id, each with a nonblank question of 1 to 512 characters.")

    @model_serializer(mode='wrap')
    def native_wire_identity(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.execution_context == 'outer':
            data.pop('execution_context', None)
        return data

    @model_validator(mode="after")
    def valid_commands(self) -> Self:
        ids = {command.id for command in self.commands}
        if len(ids) != len(self.commands) or set(self.citations) != ids or set(self.questions) != ids:
            raise PydanticCustomError("probe_id_maps", "commands must have unique IDs; citations and questions must each contain exactly those keys")
        for command in self.commands:
            if not self.citations[command.id] or not 1 <= len(self.questions[command.id].strip()) <= MAX_QUESTION_CHARS:
                raise PydanticCustomError("probe_cited_question", "each command requires nonempty citations and a nonblank question of 1 to 512 characters")
        return self


class ProbeEnvelope(ProbeBase):
    probe: ProbeRequest


class ProbePlan(ProbeBase):
    execution_source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    execution_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    request: ProbeRequest
    source_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    inputs: dict[str, str]
    capabilities: ExecutionCapabilities

    @model_validator(mode="after")
    def inspected_citations(self) -> Self:
        if self.request.execution_context == 'private_service' and self.capabilities.runtime is None:
            message = 'private-service preflight requires an explicit runtime policy'
            raise ValueError(message)
        if self.request.execution_context == 'private_service' and self.execution_source_digest is None:
            message = 'private-service preflight requires complete source identity'
            raise ValueError(message)
        if any(path not in self.inputs for paths in self.request.citations.values() for path in paths):
            raise PydanticCustomError("probe_citations_uninspected", "probe citations must be inspected source inputs")
        if any(len(value) != SHA256_LENGTH or any(c not in '0123456789abcdef' for c in value) for value in self.inputs.values()):
            message = "probe input hashes must be actual SHA-256 values"
            raise ValueError(message)
        return self

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def probe_batch_timeout(plan: ProbePlan) -> int:
    """Fresh private guests share one batch allowance, bounded by the caller's attempt."""
    return 30 * 60 if plan.request.execution_context == 'private_service' else 120


class ProbeCommandResult(ProbeBase):
    id: str
    status: Literal["completed", "timed_out", "execution_error"]
    exit_code: int | None = None
    output: str
    truncated: bool

    @model_validator(mode="after")
    def consistent_result(self) -> Self:
        if (self.status == "completed") != (self.exit_code is not None) or len(self.output.encode()) > 16 * 1024:
            message = "probe exit status or output bound is invalid"
            raise ValueError(message)
        return self


class ProbeReport(ProbeBase):
    execution_source_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    schema_version: Literal[1]
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str
    image_ref: str
    platform: str
    results: list[ProbeCommandResult] = Field(min_length=1, max_length=16)
    cleanup_confirmed: bool
    plan: ProbePlan | None = None


ProbeExecutor = Callable[[ProbePlan], Awaitable[ProbeReport]]


def validate_saved_probe_reports(reports: list[dict[str, Any]], source_commit: str, capabilities: ExecutionCapabilities) -> None:
    try:
        parsed = [ProbeReport.model_validate(data) for data in reports]
    except ValidationError:
        message = "saved capability preflight evidence is malformed"
        raise EnvironmentDiscoveryError(message, code="probe_report_invalid") from None
    for report in parsed:
        if report.plan is None:
            message = "saved capability observations lack the executed plan; restart after confirmed cleanup"
            raise EnvironmentDiscoveryError(message, code="probe_plan_missing")
        if not report.execution_source_digest:
            message = "saved preflight lacks complete execution source; restart after cleanup"
            raise EnvironmentDiscoveryError(message, code="probe_source_identity")
        validate_probe_report(report.plan, report)
        if report.plan.capabilities.digest() != capabilities.digest():
            message = "saved capability observations differ from current execution capabilities"
            raise EnvironmentDiscoveryError(message, code="probe_report_invalid")
    if any(report.source_commit != source_commit or report.image_ref != capabilities.image_ref
           or report.platform != capabilities.platform or not report.cleanup_confirmed
           or any(item.status != "completed" or item.truncated for item in report.results) for report in parsed):
        message = "saved capability preflight is incomplete or differs from the current target"
        raise EnvironmentDiscoveryError(message, code="probe_report_invalid")


def validate_probe_report(plan: ProbePlan, report: ProbeReport) -> None:
    if (report.execution_source_digest != plan.execution_source_digest or report.plan_digest != plan.digest() or (report.plan is not None and report.plan.digest() != plan.digest())
            or report.source_commit != plan.source_commit
            or report.image_ref != plan.capabilities.image_ref or report.platform != plan.capabilities.platform
            or [result.id for result in report.results] != [command.id for command in plan.request.commands]
            or not report.cleanup_confirmed):
        message = "probe evidence identity, completeness or cleanup is invalid"
        raise EnvironmentDiscoveryError(message, code="probe_report_invalid")
    if any(result.status != "completed" or result.truncated for result in report.results):
        message = "probe execution is incomplete or output was truncated"
        raise EnvironmentDiscoveryError(message, code="probe_observation_incomplete")


def bind_probe_report(plan: ProbePlan, report: ProbeReport) -> ProbeReport:
    """Attach the parent-owned executed plan after checking all reported evidence."""
    validate_probe_report(plan, report)
    return report.model_copy(update={"plan": plan.model_copy(deep=True)})
