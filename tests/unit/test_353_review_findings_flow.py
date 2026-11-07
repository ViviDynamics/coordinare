"""Independent review must explicitly resolve every retained objection."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_discovery_repair import PATH, Replies, candidate, run


def review(*, verdict='accept', findings=None, decisions=None, unavailable=False):
    return {'assessment': {'verdict': verdict, 'sources': [PATH], 'rationale': 'Independent source review',
            'findings': findings or [], 'decisions': decisions or [], 'capability_unavailable': unavailable}}


def finding(category='capability'):
    return {'category': category, 'issue': 'Required service needs coverage', 'sources': [PATH], 'affected_ids': []}


def decision(status='resolved'):
    return {'finding_id': 'F0001', 'status': status, 'rationale': 'Pinned source establishes this resolution',
            'sources': [PATH], 'duplicate_of': None}


def response():
    return {'finding_id': 'F0001', 'rationale': 'Source evidence addresses this objection', 'sources': [PATH], 'affected_ids': []}


@pytest.mark.asyncio
async def test_sampledb_objection_survives_nonblocking_review():
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
        review(verdict='revise', findings=[finding()]),
        {'manifest': candidate(), 'finding_responses': [response()]},
        review(),  # Overall accept cannot erase the retained objection.
        review(decisions=[decision()])])
    manifest, assessment = await run(replies, budget=budget)
    assert assessment.finding_ledger.records[0].id == 'F0001'
    assert assessment.manifest_digest == manifest.digest()
    assert budget.schema_repairs == 1 and budget.coverage_revisions == 1
    context = json.loads(replies.messages[4][1]['content'])
    assert context['finding_ledger']['records'][0]['proposal']['issue'] == 'Required service needs coverage'
    assert context['finding_responses'] == [response()]
    assert not any(message['role'] == 'assistant' for message in replies.messages[4])


@pytest.mark.asyncio
async def test_missing_discovery_response_cannot_close_a_finding():
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
        review(verdict='revise', findings=[finding()]), {'manifest': candidate()},
        {'manifest': candidate(), 'finding_responses': [response()]}, review(decisions=[decision()])])
    _, assessment = await run(replies, budget=budget)
    assert assessment.verdict == 'accept'
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_unavailable_required_service_holds_without_privilege_changes():
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
                       review(verdict='indeterminate', findings=[finding()], unavailable=True)])
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(replies, budget=budget)
    assert exc.value.code == 'required_capability_unavailable'
    assert budget.finding_ledger.open_ids() == ['F0001']
    assert budget.coverage_revisions == 0


@pytest.mark.asyncio
async def test_capability_revision_requires_independent_resolution_before_acceptance():
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
        review(verdict='revise', findings=[finding()], unavailable=True),
        {'manifest': candidate(), 'finding_responses': [response()]},
        review(),  # An overall accept still cannot erase the capability finding.
        review(decisions=[decision()])])
    _, assessment = await run(replies, budget=budget)
    assert budget.coverage_revisions == 1 and budget.schema_repairs == 1
    assert assessment.finding_ledger.records[0].proposal.category == 'capability'
    assert assessment.finding_ledger.open_ids() == []
    context = json.loads(replies.messages[3][-1]['content'])
    assert context['coverage_revision']['finding_ledger']['records'][0]['id'] == 'F0001'


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 1, 2])
async def test_unresolved_capability_revisions_exhaust_original_coverage_budget(limit):
    values = [{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
              review(verdict='revise', findings=[finding()], unavailable=True)]
    for _ in range(limit):
        values.extend([{'manifest': candidate(), 'finding_responses': [response()]},
                       review(verdict='revise', decisions=[decision('open')], unavailable=True)])
    replies = Replies(values)
    budget = AttemptBudget(max_coverage_revisions=limit)
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(replies, budget=budget)
    assert exc.value.code == 'attempt_coverage_limit'
    assert budget.coverage_revisions == limit
    assert budget.finding_ledger.open_ids() == ['F0001']
    assert len(replies.messages) == 3 + 2 * limit


@pytest.mark.asyncio
async def test_capability_flag_correction_identifies_actual_error_and_preserves_objection():
    budget = AttemptBudget()
    async def chat(messages):
        if len(replies.messages) == 3:
            error = json.loads(messages[-1]['content'])['validation_errors'][0]
            assert error['location'] == ['assessment', 'capability_unavailable']
            assert 'open cited capability finding' in error['guidance']
            assert 'allowed_sources' not in error
        return await replies(messages)
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
        review(verdict='revise', findings=[finding('coverage')], unavailable=True),
        {'tool': 'repair_proposal', 'changes': [
            {'path': ['assessment', 'capability_unavailable'], 'value_json': 'false'}]},
        {'manifest': candidate(), 'finding_responses': [response()]},
        review(decisions=[decision()])])
    _, assessment = await run(chat, budget=budget)
    assert budget.schema_repairs == 1 and budget.coverage_revisions == 1
    assert assessment.finding_ledger.records[0].proposal.category == 'coverage'
    assert len(assessment.finding_ledger.reviews) == 2


@pytest.mark.asyncio
async def test_changed_candidate_rechecks_closed_findings():
    import hashlib

    from coordinare_service_inference.environment.findings import FindingLedger

    from tests.unit.test_353_discovery_repair import COMMIT, SOURCE
    from tests.unit.test_353_review_findings_contract import ledger_data

    data = ledger_data()
    data['source_commit'] = COMMIT
    data['records'][0]['source_commit'] = COMMIT
    data['records'][0]['proposal']['sources'] = [PATH]
    data['records'][0]['inputs'] = {PATH: hashlib.sha256(SOURCE.encode()).hexdigest()}
    for previous in data['reviews']:
        previous['inputs'] = data['records'][0]['inputs']
        for previous_decision in previous['decisions']:
            previous_decision['sources'] = [PATH]
    budget = AttemptBudget(finding_ledger=FindingLedger.model_validate(data))
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()}, review(),
                       review(decisions=[decision()])])
    _, assessment = await run(replies, budget=budget)
    assert budget.schema_repairs == 1
    assert len(assessment.finding_ledger.reviews) == 3


@pytest.mark.asyncio
async def test_checkpoint_failure_after_review_prevents_next_model_request():
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
                       review(verdict='revise', findings=[finding()])])
    async def checkpoint():
        if budget.finding_ledger is not None and budget.finding_ledger.records:
            raise EnvironmentDiscoveryError('checkpoint refused', code='attempt_checkpoint_failed')
    budget.checkpoint = checkpoint
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(replies, budget=budget)
    assert exc.value.code == 'attempt_checkpoint_failed'
    assert len(replies.messages) == 3


@pytest.mark.asyncio
async def test_false_positive_closes_only_with_cited_independent_decision():
    invalid = decision()
    invalid['sources'] = ['unread']
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
        review(verdict='revise', findings=[finding()]),
        {'manifest': candidate(), 'finding_responses': [response()]},
        review(decisions=[invalid]), review(decisions=[decision()])])
    _, assessment = await run(replies, budget=budget)
    assert assessment.finding_ledger.open_ids() == []
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['unknown', 'duplicate', 'open_accept', 'new_accept', 'capability_accept'])
async def test_invalid_adjudication_cannot_mutate_retained_history(bad):
    value = review(decisions=[decision()])
    if bad == 'unknown':
        value['assessment']['decisions'][0]['finding_id'] = 'F0099'
    elif bad == 'duplicate':
        value['assessment']['decisions'] *= 2
    elif bad == 'open_accept':
        value['assessment']['decisions'][0]['status'] = 'open'
    elif bad == 'new_accept':
        value['assessment']['findings'] = [finding()]
    else:
        value['assessment']['capability_unavailable'] = True
    budget = AttemptBudget(max_schema_repairs=0)
    replies = Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': candidate()},
        review(verdict='revise', findings=[finding()]),
        {'manifest': candidate(), 'finding_responses': [response()]}, value])
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(replies, budget=budget)
    assert exc.value.code == 'attempt_schema_limit'
    assert budget.finding_ledger.open_ids() == ['F0001']
    assert len(budget.finding_ledger.reviews) == 1
