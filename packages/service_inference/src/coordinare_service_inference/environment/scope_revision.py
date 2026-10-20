"""Strict bounded source-scope revisions; canonical contracts remain complete."""
from __future__ import annotations

from typing import NoReturn, Self

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from .findings import Digest, FindingEvidence, FindingResponse
from .scope import ScopeCriterion, ScopeCriterionProposal, ScopeDefinition, criterion_id

MAX_SCOPE_REVISION_CHANGES = 16


class ScopeReassessment(FindingEvidence):
    criterion_id: str = Field(pattern=r'^S[0-9a-f]{16}$')
    criterion: ScopeCriterionProposal


class ScopeRevision(FindingEvidence):
    base_digest: Digest
    add: list[ScopeCriterionProposal] = Field(max_length=MAX_SCOPE_REVISION_CHANGES)
    reassess: list[ScopeReassessment] = Field(max_length=MAX_SCOPE_REVISION_CHANGES)

    @model_validator(mode='after')
    def combined_bound(self) -> Self:
        if len(self.add) + len(self.reassess) > MAX_SCOPE_REVISION_CHANGES:
            raise PydanticCustomError('scope_revision_limit', 'scope revision exceeds {limit} changes',
                                      {'limit': MAX_SCOPE_REVISION_CHANGES, 'submitted': len(self.add) + len(self.reassess)})
        return self


class ScopeRevisionEnvelope(FindingEvidence):
    scope_revision: ScopeRevision
    finding_responses: list[FindingResponse] = Field(max_length=32)


def _invalid(code: str) -> NoReturn:
    raise PydanticCustomError(code, 'source scope revision is invalid')


def apply_scope_revision(base: ScopeDefinition, revision: ScopeRevision, *,
                         inspected_inputs: dict[str, str]) -> ScopeDefinition:
    """Build an independent complete definition, publishing nothing on failure."""
    if revision.base_digest != base.digest():
        _invalid('scope_revision_base')
    if any(inspected_inputs.get(path) != digest for path, digest in base.inputs.items()):
        _invalid('scope_source_identity')
    previous = {criterion.id: criterion for criterion in base.criteria}
    replacements: dict[str, ScopeCriterion] = {}
    for change in revision.reassess:
        retained = previous.get(change.criterion_id)
        if retained is None or retained.applicability == 'required' or change.criterion_id in replacements:
            _invalid('scope_revision_target')
        replacements[change.criterion_id] = ScopeCriterion(**change.criterion.model_dump(), id=criterion_id(change.criterion))
    criteria = [replacements.get(c.id, c).model_dump() for c in base.criteria]
    criteria.extend(ScopeCriterion(**c.model_dump(), id=criterion_id(c)).model_dump() for c in revision.add)
    return ScopeDefinition.model_validate({**base.model_dump(), 'inputs': dict(inspected_inputs), 'criteria': criteria})
