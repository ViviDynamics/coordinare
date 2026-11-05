"""Reviewers cannot loop on proven-absent paths or hold unread-source gaps without an existing path."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_scope_contract import Replies, criterion, proposal, revision, run
from tests.unit.test_353_source_decision_flow import (
    READ_GUIDE,
    accept_covered,
    assess,
    feedback,
    gap,
    gap_finding,
)

NEST = {'operation': 'read_file', 'path': 'odd/nest'}
ABSENT = {'operation': 'read_file', 'path': 'nest'}


def first_review(*requests):
    return assess('revise', findings=[gap_finding(inspection_requests=list(requests))])


def second_review(*requests):
    return assess('revise', decisions=[gap(inspection_requests=list(requests))])


@pytest.mark.asyncio
async def test_first_absent_request_alongside_existing_request_admitted():
    replies = Replies([READ_GUIDE, proposal(criterion()), first_review(NEST, ABSENT),
                       revision(sources=['odd/nest']), accept_covered('F0001')])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    results = {result.status: result for result in budget.scope_investigation_results}
    assert budget.schema_repairs == 0 and results['absent'].candidates == ['odd/nest']


@pytest.mark.asyncio
@pytest.mark.parametrize('requests', [[ABSENT], [NEST, ABSENT]])
async def test_repeat_absent_request_rejected_with_candidates(requests):
    replies = Replies([READ_GUIDE, proposal(criterion()), first_review(NEST, ABSENT),
                       revision(sources=['odd/nest']), second_review(*requests), accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and len(scope.finding_ledger.reviews) == 2
    assert len(budget.scope_investigations) == 2
    error = next(item for item in feedback(replies) if item['code'] == 'scope_request_absent')
    assert error['candidates'] == ['odd/nest'] and error['location'][-2] == 'inspection_requests'


@pytest.mark.asyncio
@pytest.mark.parametrize('where', ['finding', 'decision'])
async def test_unread_source_with_only_absent_request_rejected(where):
    if where == 'finding':
        bad = first_review(ABSENT)
        replies = Replies([READ_GUIDE, proposal(criterion()), bad, assess()])
    else:
        bad = second_review(ABSENT)
        replies = Replies([READ_GUIDE, proposal(criterion()), assess('revise', findings=[gap_finding(
            gap_kind='missing_obligation')]), revision(), bad, accept_covered('F0001')])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    assert budget.schema_repairs == 1
    assert any(item['code'] == 'scope_gap_request_invalid' for item in feedback(replies))


@pytest.mark.asyncio
async def test_unread_source_with_existing_directory_request_admitted():
    listing = {'operation': 'list_files', 'path': 'odd', 'cursor': 0}
    replies = Replies([READ_GUIDE, proposal(criterion()), first_review(listing),
                       revision(), accept_covered('F0001')])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    assert budget.schema_repairs == 0
    assert [result.status for result in budget.scope_investigation_results] == ['listing']


@pytest.mark.asyncio
async def test_other_gap_kinds_may_request_absent_paths():
    replies = Replies([READ_GUIDE, proposal(criterion()),
        assess('revise', findings=[gap_finding(gap_kind='missing_obligation', inspection_requests=[ABSENT])]),
        revision(), accept_covered('F0001')])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    assert budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_reviewer_instructions_name_absent_rule():
    replies = Replies([READ_GUIDE, proposal(criterion()), assess()])
    await run(replies)
    system = replies.messages[-1][0]['content']
    assert 'An unread_source gap must request at least one file or directory present in the pinned inventory' in system
    assert 'Never re-request a path already reported absent' in system
    assert json.loads(replies.messages[-1][1]['content'])['investigation_evidence'] == []


def _reviewer_with_absence(paths, requested, stored_candidates):
    from coordinare_service_inference.environment.scope_investigation import (
        ScopeInspectionBinding,
        ScopeInspectionResult,
    )

    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection
    async def fetch(path):
        return 'content'
    budget = AttemptBudget()
    reviewer = SourceInspection(paths, fetch, DiscoveryLimits(), budget)
    binding = ScopeInspectionBinding.bind(finding_id='F0001', review_sequence=1, scope_digest='1'*64,
        source_commit='a'*40, inventory_digest=reviewer.inventory_digest, request={'operation': 'read_file', 'path': requested})
    budget.scope_investigations = [binding]
    budget.scope_investigation_results = [ScopeInspectionResult(binding_id=binding.id, status='absent',
        evidence_digest='0'*64, candidates=stored_candidates)]
    return reviewer


def _repeat(requested):
    from coordinare_service_inference.environment.scope import ScopeAssessmentProposal
    return ScopeAssessmentProposal.model_validate(assess('revise', findings=[gap_finding(
        gap_kind='missing_obligation', inspection_requests=[{'operation': 'read_file', 'path': requested}])])['scope_assessment'])


def test_repeat_feedback_recomputes_candidates_instead_of_trusting_storage():
    from coordinare.services.environment_scope import _request_rule_errors
    reviewer = _reviewer_with_absence(['pkg/scripts/gen.mjs', 'guide'], 'scripts/gen.mjs', ['guide'])
    [error] = _request_rule_errors(_repeat('scripts/gen.mjs'), reviewer=reviewer)
    assert error['candidates'] == ['pkg/scripts/gen.mjs'] and error['more_candidates'] == 0


def test_repeat_feedback_without_candidates_says_so():
    from coordinare.services.environment_scope import _request_rule_errors
    reviewer = _reviewer_with_absence(['guide'], 'scripts/gen.mjs', None)
    [error] = _request_rule_errors(_repeat('scripts/gen.mjs'), reviewer=reviewer)
    assert error['candidates'] == [] and 'no same-named' in error['guidance']


def test_candidate_entries_must_be_safe_paths():
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionResult
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ScopeInspectionResult(binding_id='I'+'0'*64, status='absent', evidence_digest='0'*64, candidates=['../escape'])
