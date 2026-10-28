"""Bounded, source-bound coverage objections and explicit independent decisions."""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import PydanticCustomError

from .evidence_safety import reject_nested_credentials, source_path

if TYPE_CHECKING:
    from collections.abc import Mapping

Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Commit = Annotated[str, Field(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')]
FindingId = Annotated[str, Field(pattern=r'^F[0-9]{4}$')]
MAX_LEDGER_FINDINGS = 128
LEDGER_MAX_BYTES = 512*1024
# remote_state: an obligation only an external service's state can satisfy, outside the environment's scope.
# runtime_network: an obligation whose own documented procedure fetches from the network every time it runs,
# which an offline clean verification cannot exercise.
# deployment_image: an obligation to build or run the repository's own application images or deployment stacks,
# a deployment concern outside the development and test environment an agent needs.
Category = Literal['coverage', 'capability', 'activation', 'service_lifecycle', 'execution_definition', 'remote_state',
                   'runtime_network', 'deployment_image']


class FindingEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def safe_evidence(cls, value: object) -> object:
        reject_nested_credentials(value)
        return value

    @field_validator('issue', 'rationale', check_fields=False)
    @classmethod
    def bounded_text(cls, value: str) -> str:
        if not value.strip() or len(value.encode('utf-8')) > 2048:
            raise ValueError('finding text must contain 1 to 2048 UTF-8 bytes')
        return value

    @field_validator('sources', check_fields=False)
    @classmethod
    def source_paths(cls, value: list[str]) -> list[str]:
        if not value or len(set(value)) != len(value):
            raise PydanticCustomError('finding_citations_invalid', 'finding citations must be nonempty and unique')
        return [source_path(path) for path in value]

    @field_validator('inputs', check_fields=False)
    @classmethod
    def input_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value:
            source_path(path)
        return value


class FindingProposal(FindingEvidence):
    category: Category
    issue: str
    sources: list[str] = Field(min_length=1)
    affected_ids: list[str]


class FindingResponse(FindingEvidence):
    finding_id: FindingId
    rationale: str
    sources: list[str] = Field(min_length=1)
    affected_ids: list[str]


class FindingDecisionProposal(FindingEvidence):
    finding_id: FindingId
    status: Literal['open', 'resolved']
    rationale: str
    sources: list[str] = Field(min_length=1)
    duplicate_of: FindingId | None = None


class FindingRecord(FindingEvidence):
    id: FindingId
    proposal: FindingProposal
    manifest_digest: Digest
    source_commit: Commit
    inputs: dict[str, Digest]


class FindingReview(FindingEvidence):
    sequence: int = Field(ge=1)
    manifest_digest: Digest
    inputs: dict[str, Digest]
    decisions: list[FindingDecisionProposal]
    added_ids: list[FindingId]


class FindingLedger(FindingEvidence):
    schema_version: Literal[1] = 1
    source_commit: Commit
    records: list[FindingRecord] = Field(default_factory=list, max_length=MAX_LEDGER_FINDINGS)
    reviews: list[FindingReview] = Field(default_factory=list)

    def canonical(self) -> bytes:
        return json.dumps(self.model_dump(mode='json'), sort_keys=True,
                          separators=(',', ':'), ensure_ascii=False).encode('utf-8')

    def digest(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    @model_validator(mode='after')
    def valid_history(self) -> Self:
        if len(self.canonical()) > LEDGER_MAX_BYTES:
            raise PydanticCustomError('finding_ledger_limit', 'finding ledger exceeds its byte bound')
        if [r.id for r in self.records] != [f'F{i:04}' for i in range(1, len(self.records)+1)]:
            raise ValueError('finding identities must be monotonic and unique')
        records = {record.id: record for record in self.records}
        seen: set[str] = set()
        hashes: dict[str, str] = {}
        for sequence, review in enumerate(self.reviews, 1):
            if review.sequence != sequence:
                raise ValueError('finding review sequence is invalid')
            for path, digest in review.inputs.items():
                if hashes.get(path, digest) != digest:
                    raise ValueError('finding evidence changed within pinned source')
                hashes[path] = digest
            decisions = {decision.finding_id: decision for decision in review.decisions}
            if len(decisions) != len(review.decisions) or set(decisions) != seen:
                raise ValueError('every retained finding requires exactly one decision')
            added = set(review.added_ids)
            if len(added) != len(review.added_ids) or added & seen or not added <= records.keys():
                raise ValueError('finding additions are invalid')
            for fid in review.added_ids:
                record = records[fid]
                if (record.source_commit != self.source_commit or record.manifest_digest != review.manifest_digest
                        or set(record.inputs) != set(record.proposal.sources)
                        or any(review.inputs.get(path) != digest for path, digest in record.inputs.items())):
                    raise ValueError('finding origin evidence does not match its review')
            for decision in review.decisions:
                if any(path not in review.inputs for path in decision.sources):
                    raise ValueError('finding decision cites unread evidence')
                target = decision.duplicate_of
                if target is not None and (decision.status != 'resolved' or target not in seen or target == decision.finding_id):
                    raise ValueError('invalid duplicate finding resolution')
                visited = {decision.finding_id}
                while target is not None:
                    if target in visited or target not in decisions:
                        raise ValueError('duplicate finding cycle or unknown target')
                    visited.add(target)
                    target = decisions[target].duplicate_of
            seen.update(added)
        if seen != set(records):
            raise ValueError('finding has no originating review')
        return self

    def open_ids(self) -> list[str]:
        if not self.reviews:
            return []
        last = self.reviews[-1]
        return sorted([*last.added_ids, *(d.finding_id for d in last.decisions if d.status == 'open')])


def validate_finding_ledger(ledger: FindingLedger, *, manifest_digest: str,
                            source_commit: str, inputs: Mapping[str, str]) -> None:
    """Require complete independent closure for this exact candidate and source."""
    # Revalidate mutable model contents at each trust boundary.
    checked = FindingLedger.model_validate(ledger.model_dump(mode='json'))
    if (checked.source_commit != source_commit or not checked.reviews or checked.open_ids()
            or checked.reviews[-1].manifest_digest != manifest_digest
            or any(inputs.get(path) != digest for review in checked.reviews for path, digest in review.inputs.items())):
        raise ValueError('coverage findings lack complete current evidence and closure')


def finding_summary(ledger: FindingLedger | None) -> dict[str, object]:
    """Only validated identifiers and categories belong in operator summaries."""
    categories: dict[str, int] = {}
    for record in ledger.records if ledger is not None else []:
        category = record.proposal.category
        categories[category] = categories.get(category, 0) + 1
    return {'finding_count': len(ledger.records) if ledger is not None else 0,
            'open_finding_ids': ledger.open_ids() if ledger is not None else [],
            'finding_categories': categories}
