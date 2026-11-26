"""nare-native-041: two shard reviewers of a fresh scope filed the same remote_state finding (same category and
affected ids). The ledger merged them into one id (nare-native-034's fix), but the inspection binding still expected
one id per unmerged finding and raised scope_investigation_identity, holding the attempt. Duplicates are now merged in
the assessment itself, so the ledger, the bindings and the saved record agree."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.findings import FindingLedger

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import MESSAGE_DETAIL_CODES, DiscoveryLimits
from coordinare.services.environment_findings import merge_duplicate_findings
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_scope import _validate_review
from tests.unit.test_353_scope_contract import (
    COMMIT,
    SOURCES,
    Replies,
    criterion,
    proposal,
    review,
    run,
)


def _finding(issue: str, affected: list[str], path: str, source: str = 'guide') -> dict:
    return {'category': 'remote_state', 'issue': issue, 'sources': [source], 'affected_ids': affected,
            'gap_kind': 'missing_obligation', 'inspection_requests': [{'operation': 'read_file', 'path': path}]}


@pytest.mark.asyncio
async def test_duplicate_scope_findings_bind_every_request_to_one_finding_id() -> None:
    scope = await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]))
    affected = [scope.definition.criteria[0].id]
    budget = AttemptBudget(scope_findings=FindingLedger(source_commit=COMMIT))

    async def fetch(path: str) -> str | None:
        return SOURCES.get(path)

    reviewer = SourceInspection(list(SOURCES), fetch, DiscoveryLimits(), budget)
    await reviewer.read_files(['guide', 'odd/nest'])
    candidate = review('revise', findings=[_finding('remote model needed', affected, 'odd/nest'),
                                           _finding('credentials are external', affected, 'unfamiliar/entry', 'odd/nest')])
    assessment, ledger, record = _validate_review(candidate, reviewer=reviewer, definition=scope.definition,
                                                  ledger=budget.scope_findings, commit=COMMIT)
    assert ledger.reviews[-1].added_ids == ['F0001'] and len(assessment.findings) == 1
    assert len(record.assessment.findings) == 1
    assert sorted(binding.request.path for binding in budget.scope_investigations) == ['odd/nest', 'unfamiliar/entry']
    assert {binding.finding_id for binding in budget.scope_investigations} == {'F0001'}
    assert assessment.findings[0].sources == ['guide', 'odd/nest']


def test_merged_scope_findings_keep_each_request_once() -> None:
    from coordinare_service_inference.environment.scope import ScopeFindingProposal
    first = ScopeFindingProposal.model_validate(_finding('a', ['S1'], 'x'))
    second = ScopeFindingProposal.model_validate(_finding('b', ['S1'], 'x'))
    merged = merge_duplicate_findings([first, second])
    assert len(merged) == 1 and [request.path for request in merged[0].inspection_requests] == ['x']


def test_a_scope_identity_hold_names_its_check() -> None:
    assert 'scope_investigation_identity' in MESSAGE_DETAIL_CODES
