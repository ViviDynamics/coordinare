"""Source-only obligations and independently reviewed, pinned scope identity."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import Field, field_validator, model_serializer, model_validator
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from .models import EnvironmentManifest

from .findings import (
    Commit,
    Digest,
    FindingDecisionProposal,
    FindingEvidence,
    FindingId,
    FindingLedger,
    FindingProposal,
    FindingResponse,
    validate_finding_ledger,
)
from .scope_investigation import ScopeInspectionRequest, unique_requests


MAX_SCOPE_CRITERIA = 512
MAX_SCOPE_FINDINGS = 128
SCOPE_DEFINITION_MAX_BYTES = 1024*1024
SCOPE_CONTRACT_MAX_BYTES = 2*1024*1024


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
    criteria: list[ScopeCriterionProposal] = Field(min_length=1, max_length=MAX_SCOPE_CRITERIA)


class ScopeEnvelope(FindingEvidence):
    scope: ScopeProposal
    finding_responses: list[FindingResponse] = Field(max_length=MAX_SCOPE_FINDINGS)


MAX_CROSS_REFERENCES = 64
MAX_SHARD_QUESTIONS = 16


class ShardSummaryProposal(FindingEvidence):
    """One shard author's cited obligations; the daemon merges shards into one definition."""

    criteria: list[ScopeCriterionProposal] = Field(max_length=MAX_SCOPE_CRITERIA)
    cross_references: list[str] = Field(default_factory=list, max_length=MAX_CROSS_REFERENCES)
    questions: list[str] = Field(default_factory=list, max_length=MAX_SHARD_QUESTIONS)

    @field_validator('questions')
    @classmethod
    def bounded_questions(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item.encode()) > 2048 for item in value):
            raise ValueError('shard questions must be nonblank and at most 2048 UTF-8 bytes')
        return value


class ShardSummaryEnvelope(FindingEvidence):
    shard_summary: ShardSummaryProposal


class ShardSummary(ShardSummaryProposal):
    """A completed shard, bound to the exact inputs its agent inspected."""

    shard_id: str = Field(pattern=r'^shard-[0-9]{3}-[0-9a-f]{8}(?:\.r?[0-9]+)*$')
    inputs: dict[str, Digest]


class ScopeDefinition(FindingEvidence):
    schema_version: Literal[1] = 1
    requested_scope: Literal['whole_documented_development_test_environment'] = 'whole_documented_development_test_environment'
    source_commit: Commit
    inventory_digest: Digest
    inputs: dict[str, Digest] = Field(min_length=1)
    criteria: list[ScopeCriterion] = Field(min_length=1, max_length=MAX_SCOPE_CRITERIA)

    @model_validator(mode='after')
    def source_owned(self) -> Self:
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError('scope criteria must be unique')
        if any(path not in self.inputs for c in self.criteria for path in c.sources):
            raise ValueError('scope criterion cites unread source')
        if len(canonical(self.model_dump(mode='json'))) > SCOPE_DEFINITION_MAX_BYTES:
            raise ValueError('scope definition exceeds its bound')
        return self

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode='json'))).hexdigest()


SourceGapKind = Literal['missing_obligation', 'unread_source', 'source_conflict', 'unknown_applicability',
                        'incomplete_runtime_needs', 'incomplete_success_condition']


def _unique_ids(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        raise ValueError('referenced IDs must be unique')
    return value


class ScopeFindingProposal(FindingProposal):
    gap_kind: SourceGapKind
    inspection_requests: list[ScopeInspectionRequest] = Field(max_length=8)

    @field_validator('inspection_requests')
    @classmethod
    def distinct_requests(cls, value: list[ScopeInspectionRequest]) -> list[ScopeInspectionRequest]:
        return unique_requests(value)


class _SourceDecision(FindingEvidence):
    finding_id: FindingId
    rationale: str
    sources: list[str] = Field(min_length=1)


class CoveredDecision(_SourceDecision):
    disposition: Literal['covered']
    criterion_ids: list[str] = Field(min_length=1, max_length=MAX_SCOPE_CRITERIA)

    @field_validator('criterion_ids')
    @classmethod
    def unique_references(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)


class SourceCorrectionDecision(_SourceDecision):
    disposition: Literal['source_correction']


class SourceGapDecision(_SourceDecision):
    disposition: Literal['source_gap']
    gap_kind: SourceGapKind
    affected_ids: list[str] = Field(max_length=MAX_SCOPE_CRITERIA)
    inspection_requests: list[ScopeInspectionRequest] = Field(max_length=8)

    @field_validator('affected_ids')
    @classmethod
    def unique_references(cls, value: list[str]) -> list[str]:
        return _unique_ids(value)

    @field_validator('inspection_requests')
    @classmethod
    def distinct_requests(cls, value: list[ScopeInspectionRequest]) -> list[ScopeInspectionRequest]:
        return unique_requests(value)


class DuplicateDecision(_SourceDecision):
    disposition: Literal['duplicate']
    duplicate_of: FindingId


SourceFindingDecision = Annotated[CoveredDecision | SourceCorrectionDecision | SourceGapDecision | DuplicateDecision,
                                  Field(discriminator='disposition')]


_DECISION_BRANCHES: dict[str, type[_SourceDecision]] = {'covered': CoveredDecision,
    'source_correction': SourceCorrectionDecision, 'source_gap': SourceGapDecision, 'duplicate': DuplicateDecision}


def normalize_source_assessment(data: object) -> object:
    """Map the flat provider wire decision onto its strict branch.

    Only fields outside the chosen disposition whose value is null or empty are dropped;
    any populated inapplicable field is kept so strict validation rejects it.
    """
    assessment = data.get('scope_assessment') if isinstance(data, dict) else None
    decisions = assessment.get('decisions') if isinstance(assessment, dict) else None
    if not isinstance(data, dict) or not isinstance(assessment, dict) or not isinstance(decisions, list):
        return data
    normalized = []
    for decision in decisions:
        disposition = decision.get('disposition') if isinstance(decision, dict) else None
        branch = _DECISION_BRANCHES.get(disposition) if isinstance(disposition, str) else None
        if isinstance(decision, dict) and branch is not None:
            decision = {key: value for key, value in decision.items()
                        if key in branch.model_fields or value not in (None, [])}
        normalized.append(decision)
    return {**data, 'scope_assessment': {**assessment, 'decisions': normalized}}


def derive_finding_decision(decision: SourceFindingDecision) -> FindingDecisionProposal:
    """The disposition alone determines generic ledger status; prose is never inspected."""
    return FindingDecisionProposal(finding_id=decision.finding_id, rationale=decision.rationale,
        sources=list(decision.sources), status='open' if decision.disposition == 'source_gap' else 'resolved',
        duplicate_of=decision.duplicate_of if isinstance(decision, DuplicateDecision) else None)


class ScopeAssessmentProposal(FindingEvidence):
    verdict: Literal['accept', 'revise', 'indeterminate']
    rationale: str = Field(min_length=1, max_length=2048, description='Nonblank evidence summary, at most 2048 UTF-8 bytes.')
    sources: list[str] = Field(min_length=1)
    findings: list[ScopeFindingProposal] = Field(max_length=MAX_SCOPE_FINDINGS)
    decisions: list[SourceFindingDecision] = Field(max_length=MAX_SCOPE_FINDINGS)

    @model_validator(mode='after')
    def no_accepted_requests(self) -> Self:
        gaps = [item for item in self.decisions if isinstance(item, SourceGapDecision)]
        if self.verdict == 'accept' and any(item.inspection_requests for item in [*self.findings, *gaps]):
            raise ValueError('accepting judgment cannot require investigation')
        return self


class ScopeAssessmentEnvelope(FindingEvidence):
    scope_assessment: ScopeAssessmentProposal


class SourceAssessmentRecord(FindingEvidence):
    """The typed source judgment bound to the ledger review derived from it."""

    schema_version: Literal[1] = 1
    review_sequence: int = Field(ge=1)
    scope_digest: Digest
    source_commit: Commit
    inventory_digest: Digest
    reviewer_inputs: dict[str, Digest] = Field(min_length=1)
    assessment: ScopeAssessmentProposal


def _criterion_references(assessment: ScopeAssessmentProposal) -> set[str]:
    named = {identifier for finding in assessment.findings for identifier in finding.affected_ids}
    for decision in assessment.decisions:
        if isinstance(decision, CoveredDecision):
            named.update(decision.criterion_ids)
        elif isinstance(decision, SourceGapDecision):
            named.update(decision.affected_ids)
    return named


def validate_source_assessment(record: SourceAssessmentRecord, *, ledger: FindingLedger,
                               definition: ScopeDefinition | None = None) -> None:
    """Require the saved typed assessment to reproduce its ledger review exactly.

    When the reviewed definition is available, the typed criterion references and
    pinned identity must also belong to it, so recovery repeats admission checks.
    """
    if definition is not None and (
            (record.scope_digest, record.source_commit, record.inventory_digest)
                != (definition.digest(), definition.source_commit, definition.inventory_digest)
            or not _criterion_references(record.assessment) <= {c.id for c in definition.criteria}):
        raise PydanticCustomError('source_assessment_binding', 'source assessment differs from its reviewed definition')
    reviews = ledger.reviews
    review = reviews[record.review_sequence-1] if record.review_sequence <= len(reviews) else None
    assessment = record.assessment
    added = {item.id: item.proposal for item in ledger.records}
    if (review is None or ledger.source_commit != record.source_commit
            or review.manifest_digest != record.scope_digest or review.inputs != record.reviewer_inputs
            or [derive_finding_decision(d) for d in assessment.decisions] != review.decisions
            or len(review.added_ids) != len(assessment.findings)
            or any(added[fid] != FindingProposal.model_validate(finding.model_dump(exclude={'inspection_requests', 'gap_kind'}))
                   for fid, finding in zip(review.added_ids, assessment.findings, strict=True))
            or any(path not in record.reviewer_inputs for path in assessment.sources)):
        raise PydanticCustomError('source_assessment_binding', 'source assessment differs from its ledger review')


class ScopeContract(FindingEvidence):
    schema_version: Literal[1, 2] = 2
    definition: ScopeDefinition
    review_inputs: dict[str, Digest] = Field(min_length=1)
    sources: list[str] = Field(min_length=1)
    finding_ledger: FindingLedger
    finding_responses: list[FindingResponse] = Field(max_length=MAX_SCOPE_FINDINGS)
    source_assessment: SourceAssessmentRecord | None = None

    @model_serializer(mode='wrap')
    def serialize_legacy_compatibly(self, handler: Any) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.schema_version == 1:
            data.pop('source_assessment', None)
        return data

    @property
    def current(self) -> bool:
        return self.schema_version == 2

    @property
    def all_inputs(self) -> dict[str, str]:
        return {**self.definition.inputs, **self.review_inputs}

    @model_validator(mode='after')
    def independent_closure(self) -> Self:
        if len(canonical(self.model_dump(mode='json'))) > SCOPE_CONTRACT_MAX_BYTES:
            raise PydanticCustomError('scope_contract_limit', 'scope contract exceeds its bound')
        if any(c.applicability == 'unknown' for c in self.definition.criteria):
            raise ValueError('scope applicability remains unknown')
        if any(self.review_inputs.get(path) != digest for path, digest in self.definition.inputs.items()):
            raise ValueError('scope reviewer must independently read every author input')
        if any(path not in self.review_inputs for path in self.sources):
            raise ValueError('scope judgment cites unread evidence')
        validate_finding_ledger(self.finding_ledger, manifest_digest=self.definition.digest(),
            source_commit=self.definition.source_commit, inputs=self.review_inputs)
        self._bound_assessment()
        return self

    def _bound_assessment(self) -> None:
        record, definition = self.source_assessment, self.definition
        if self.schema_version == 1:
            if record is not None:
                raise ValueError('legacy scope contract cannot carry a typed source assessment')
            return
        if (record is None or record.review_sequence != len(self.finding_ledger.reviews)
                or record.assessment.verdict != 'accept' or record.assessment.sources != self.sources
                or record.reviewer_inputs != self.review_inputs):
            raise ValueError('scope contract requires its accepting typed source assessment')
        validate_source_assessment(record, ledger=self.finding_ledger, definition=definition)

    def digest(self) -> str:
        return hashlib.sha256(canonical(self.model_dump(mode='json'))).hexdigest()


def validate_scope_mapping(scope: ScopeContract, manifest: EnvironmentManifest) -> None:
    """Bind every required criterion to real, required consumer checks."""
    checked = ScopeContract.model_validate(scope.model_dump(mode='json'))
    if not checked.current:
        raise PydanticCustomError('scope_contract_refresh_required', 'legacy scope contract requires fresh source review')
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
