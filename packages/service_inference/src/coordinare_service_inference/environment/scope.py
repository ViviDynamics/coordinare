"""Source-only obligations and independently reviewed, pinned scope identity."""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

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


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


class ScopeCriterionProposal(FindingEvidence):
    description: str = Field(min_length=1, max_length=2048)
    sources: list[str] = Field(min_length=1, max_length=32)
    applicability: Literal['required', 'not_applicable', 'unknown']
    rationale: str
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


class ScopeAssessmentProposal(FindingEvidence):
    verdict: Literal['accept', 'revise', 'indeterminate']
    rationale: str
    sources: list[str]
    findings: list[FindingProposal] = Field(max_length=32)
    decisions: list[FindingDecisionProposal] = Field(max_length=32)


class ScopeAssessmentEnvelope(FindingEvidence):
    scope_assessment: ScopeAssessmentProposal
