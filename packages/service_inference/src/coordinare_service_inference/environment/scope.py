"""Source-only obligations and independently reviewed, pinned scope identity."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from .models import EnvironmentManifest

from .findings import (
    Commit,
    Digest,
    FindingDecisionProposal,
    FindingEvidence,
    FindingLedger,
    FindingProposal,
    FindingResponse,
    validate_finding_ledger,
)
from .scope_investigation import ScopeInspectionRequest, unique_requests


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class ScopeCriterionProposal(FindingEvidence):
    description: str = Field(min_length=1, max_length=2048)
    sources: list[str] = Field(min_length=1, max_length=32)
    applicability: Literal['required', 'not_applicable', 'unknown']
    rationale: str = Field(min_length=1, max_length=2048, description='Nonblank evidence summary, at most 2048 UTF-8 bytes.')
    usability: str = Field(min_length=1, max_length=2048)
    runtime_needs: list[str] = Field(max_length=32)

    @field_validator('description', 'usability')
    @classmethod
    def substantive_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('scope criterion must contain observable source-backed meaning')
        return value

    @field_validator('sources')
    @classmethod
    def canonical_sources(cls, value: list[str]) -> list[str]:
        return sorted(value)

    @field_validator('runtime_needs')
    @classmethod
    def bounded_needs(cls, value: list[str]) -> list[str]:
        if any(not need.strip() or len(need.encode()) > 2048 for need in value) or len(set(value)) != len(value):
            raise ValueError('runtime needs must be bounded, nonempty and unique')
        return sorted(value)


def criterion_id(criterion: ScopeCriterionProposal) -> str:
    return 'S' + hashlib.sha256(canonical(criterion.model_dump(exclude={'id', 'rationale'}))).hexdigest()[:16]


class ScopeCriterion(ScopeCriterionProposal):
    id: str = Field(pattern=r'^S[0-9a-f]{16}$')

    @model_validator(mode='after')
    def daemon_identity(self) -> Self:
        if self.id != criterion_id(self):
            raise ValueError('scope criterion identity differs from its complete definition')
        return self


class ScopeProposal(FindingEvidence):
    criteria: list[ScopeCriterionProposal] = Field(min_length=1, max_length=128)


class ScopeEnvelope(FindingEvidence):
    scope: ScopeProposal
    finding_responses: list[FindingResponse] = Field(max_length=32)


class ScopeDefinition(FindingEvidence):
    schema_version: Literal[1] = 1
    requested_scope: Literal['whole_documented_development_test_environment'] = 'whole_documented_development_test_environment'
    source_commit: Commit
    inventory_digest: Digest
    inputs: dict[str, Digest] = Field(min_length=1)
    criteria: list[ScopeCriterion] = Field(min_length=1, max_length=128)

    @model_validator(mode='after')
    def source_owned(self) -> Self:
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError('scope criteria must be unique')
        if any(path not in self.inputs for c in self.criteria for path in c.sources):
            raise ValueError('scope criterion cites unread source')
        if len(canonical(self.model_dump(mode='json'))) > 256*1024:
            raise ValueError('scope definition exceeds its bound')
        return self

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode='json'))).hexdigest()


class ScopeContract(FindingEvidence):
    schema_version: Literal[1] = 1
    definition: ScopeDefinition
    review_inputs: dict[str, Digest] = Field(min_length=1)
    sources: list[str]
    finding_ledger: FindingLedger
    finding_responses: list[FindingResponse] = Field(max_length=32)

    @property
    def all_inputs(self) -> dict[str, str]:
        return {**self.definition.inputs, **self.review_inputs}

    @model_validator(mode='after')
    def independent_closure(self) -> Self:
        if any(c.applicability == 'unknown' for c in self.definition.criteria):
            raise ValueError('scope applicability remains unknown')
        if any(self.review_inputs.get(path) != digest for path, digest in self.definition.inputs.items()):
            raise ValueError('scope reviewer must independently read every author input')
        if any(path not in self.review_inputs for path in self.sources):
            raise ValueError('scope judgment cites unread evidence')
        validate_finding_ledger(self.finding_ledger, manifest_digest=self.definition.digest(),
            source_commit=self.definition.source_commit, inputs=self.review_inputs)
        if len(canonical(self.model_dump(mode='json'))) > 256*1024:
            raise ValueError('scope contract exceeds its bound')
        return self

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode='json'))).hexdigest()


class ScopeFindingProposal(FindingProposal):
    inspection_requests: list[ScopeInspectionRequest] = Field(max_length=8)

    @field_validator('inspection_requests')
    @classmethod
    def distinct_requests(cls, value: list[ScopeInspectionRequest]) -> list[ScopeInspectionRequest]:
        return unique_requests(value)


class ScopeFindingDecisionProposal(FindingDecisionProposal):
    inspection_requests: list[ScopeInspectionRequest] = Field(max_length=8)

    @field_validator('inspection_requests')
    @classmethod
    def distinct_requests(cls, value: list[ScopeInspectionRequest]) -> list[ScopeInspectionRequest]:
        return unique_requests(value)

    @model_validator(mode='after')
    def no_resolved_requests(self) -> Self:
        if self.status == 'resolved' and self.inspection_requests:
            raise ValueError('resolved finding cannot require investigation')
        return self


class ScopeAssessmentProposal(FindingEvidence):
    verdict: Literal['accept', 'revise', 'indeterminate']
    rationale: str = Field(min_length=1, max_length=2048, description='Nonblank evidence summary, at most 2048 UTF-8 bytes.')
    sources: list[str]
    findings: list[ScopeFindingProposal] = Field(max_length=32)
    decisions: list[ScopeFindingDecisionProposal] = Field(max_length=32)


    @model_validator(mode='after')
    def no_accepted_requests(self) -> Self:
        if self.verdict == 'accept' and any(item.inspection_requests for item in [*self.findings, *self.decisions]):
            raise ValueError('accepting judgment cannot require investigation')
        return self


class ScopeAssessmentEnvelope(FindingEvidence):
    scope_assessment: ScopeAssessmentProposal


def validate_scope_mapping(scope: ScopeContract, manifest: EnvironmentManifest) -> None:
    """Bind every required criterion to real, required consumer checks."""
    checked = ScopeContract.model_validate(scope.model_dump(mode='json'))
    if (manifest.scope_contract_digest != checked.digest()
            or manifest.source_commit != checked.definition.source_commit
            or manifest.inventory_digest != checked.definition.inventory_digest
            or any(manifest.inputs.get(path) != digest for path, digest in checked.all_inputs.items())):
        raise PydanticCustomError('scope_identity_mismatch', 'reviewed scope identity differs from manifest')
    commands = [*manifest.install, *manifest.checks]
    for service in manifest.services:
        lifecycle = [c for c in (service.init, service.start, service.health, service.stop) if c is not None]
        if service.required and any(not c.required for c in lifecycle):
            raise PydanticCustomError('scope_required_service', 'required service lifecycle cannot be optional')
        commands.extend(lifecycle)
    identifiers = [*(c.id for c in checked.definition.criteria), *(r.id for r in manifest.requirements),
                   *(s.id for s in manifest.services), *(c.id for c in commands)]
    if len(identifiers) != len(set(identifiers)):
        raise PydanticCustomError('scope_id_collision', 'scope, obligation, service and command IDs must be unambiguous')
    required = {c.id for c in checked.definition.criteria if c.applicability == 'required'}
    checks = {c.id: c for c in manifest.checks}
    if set(manifest.criterion_checks) != required or any(
            not ids or len(ids) != len(set(ids)) or any(identifier not in checks or not checks[identifier].required
                or checks[identifier].scope for identifier in ids) for ids in manifest.criterion_checks.values()):
        raise PydanticCustomError('scope_required_check_mapping', 'every required scope criterion needs required unscoped consumer checks')
