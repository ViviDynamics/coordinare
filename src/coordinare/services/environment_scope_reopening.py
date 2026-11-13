"""Temporary, exact reassessment authority from the current independent source review."""
from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from coordinare_service_inference.environment.scope import (
    SourceGapDecision,
    validate_source_assessment,
)

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Collection

    from coordinare_service_inference.environment.scope import ScopeDefinition, ScopeFindingProposal

    from coordinare.services.environment_attempt import AttemptBudget


def _invalid() -> NoReturn:
    message = 'source conflict reassessment lacks current independent evidence'
    raise EnvironmentDiscoveryError(message, code='scope_reopening_identity') from None


def current_source_conflicts(budget: AttemptBudget, definition: ScopeDefinition, *,
                             finding_ids: Collection[str] | None = None,
                             ) -> dict[str, ScopeFindingProposal | SourceGapDecision]:
    """The current typed objections, never their superseded ledger proposals."""
    saved, ledger = budget.scope_assessment, budget.scope_findings
    if saved is None or ledger is None or saved.assessment.verdict != 'revise':
        return {}
    if saved.review_sequence != len(ledger.reviews):
        _invalid()
    try:
        validate_source_assessment(saved, ledger=ledger, definition=definition)
    except (ValueError, KeyError, IndexError):
        _invalid()
    review = ledger.reviews[saved.review_sequence - 1]
    conflicts = [(identifier, finding) for identifier, finding in
                 zip(review.added_ids, saved.assessment.findings, strict=True)
                 if finding.gap_kind == 'source_conflict']
    conflicts.extend((decision.finding_id, decision) for decision in saved.assessment.decisions
                     if isinstance(decision, SourceGapDecision) and decision.gap_kind == 'source_conflict')
    allowed = set(ledger.open_ids())
    if finding_ids is not None:
        allowed.intersection_update(finding_ids)
    return {identifier: conflict for identifier, conflict in conflicts if identifier in allowed}


def source_conflict_reopenings(budget: AttemptBudget, definition: ScopeDefinition, *,
                              finding_ids: Collection[str] | None = None) -> set[str]:
    """Only the latest, source-bound reviewer can reopen its explicitly named required criteria."""
    conflicts = current_source_conflicts(budget, definition, finding_ids=finding_ids)
    requested = {identifier for conflict in conflicts.values() for identifier in conflict.affected_ids}
    targets = [criterion for criterion in definition.criteria
               if criterion.id in requested and criterion.applicability == 'required']
    if any(budget.scope_assessment is None or budget.scope_assessment.reviewer_inputs.get(path) != definition.inputs[path]
           for criterion in targets for path in criterion.sources):
        _invalid()
    return {criterion.id for criterion in targets}
