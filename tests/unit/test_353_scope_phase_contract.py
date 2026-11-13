"""Source coverage can precede runtime proof; rejected revisions retain all diagnostics."""
from __future__ import annotations

import copy
import json

import pytest
from coordinare_service_inference.environment.scope import ScopeCriterionProposal, criterion_id

from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_scope_contract import (
    Replies,
    criterion,
    proposal,
    review,
    revision,
    run,
)


@pytest.mark.asyncio
async def test_source_review_exposes_phase_authority_and_retains_unprobed_operation():
    obligation = criterion(usability='A request is persisted by the required service')
    obligation['runtime_needs'] = ['An operational service supplied during installation']
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(obligation), review()])
    scope = await run(replies)
    context = json.loads(replies.messages[2][1]['content'])
    assert context['phase_contract'] == {
        'subject': 'source_obligations', 'runtime_evidence': 'required_after_scope_acceptance',
        'readiness_authority': False,
    }
    instructions = replies.messages[2][0]['content']
    assert 'Inspect available execution mechanisms through capability probes' not in instructions
    assert 'Uncertain feasibility requires cited blocking findings' not in instructions
    assert scope.definition.criteria[0].applicability == 'required'
    assert scope.definition.criteria[0].runtime_needs == obligation['runtime_needs']
    assert 'persisted' in scope.definition.criteria[0].usability


@pytest.mark.asyncio
async def test_all_stale_response_locations_are_reported_without_committing_revision():
    old = [criterion(applicability='unknown', usability=f'Unknown operation {i}') for i in range(2)]
    old_ids = [criterion_id(ScopeCriterionProposal.model_validate(c)) for c in old]
    updated = [criterion(usability=f'Operation {i} persists a request') for i in range(2)]
    findings = [{'category': 'coverage', 'issue': f'Operation {i} needs source investigation',
                 'sources': ['guide'], 'affected_ids': [old_ids[i]]} for i in range(2)]
    responses = [{'finding_id': f'F000{i+1}', 'rationale': 'Source establishes the operation',
                  'sources': ['guide'], 'affected_ids': [old_ids[i]]} for i in range(2)]
    changes = [{'criterion_id': identifier, 'criterion': c} for identifier, c in zip(old_ids, updated, strict=True)]
    bad = revision(reassess=changes, responses=responses)
    budget = AttemptBudget()

    def correct(messages):
        feedback = json.loads(messages[-1]['content'])
        errors = feedback['validation_errors']
        assert [e['location'] for e in errors] == [
            ['finding_responses', '0', 'affected_ids'], ['finding_responses', '1', 'affected_ids'],
        ]
        assert [e['finding_id'] for e in errors] == ['F0001', 'F0002']
        assert all(e['invalid_count'] == 1 for e in errors)
        assert 'Preserve the attempted scope_revision operations' in feedback['instruction']
        assert [c.id for c in budget.scope_definition.criteria] == old_ids
        assert budget.scope_findings.open_ids() == ['F0001', 'F0002']
        value = copy.deepcopy(json.loads(messages[-2]['content']))
        assert value['scope_revision']['reassess'] == changes
        for response in value['finding_responses']:
            response['affected_ids'] = []
        return value

    decisions = [{'finding_id': f'F000{i+1}', 'status': 'resolved', 'rationale': 'Required operation retained',
                  'sources': ['guide'], 'duplicate_of': None} for i in range(2)]
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(*old),
                       review('revise', findings=findings), bad, correct, review(decisions=decisions)])
    scope = await run(replies, budget=budget)
    assert [c.usability for c in scope.definition.criteria] == [c['usability'] for c in updated]
    assert all(c.applicability == 'required' for c in scope.definition.criteria)
    assert scope.finding_ledger.open_ids() == []
    assert budget.schema_repairs == 1 and budget.scope_revisions == 1


@pytest.mark.asyncio
async def test_reviewer_can_investigate_then_request_source_revision():
    finding = {'category': 'coverage', 'issue': 'Nested operational obligation omitted',
               'sources': ['odd/nest'], 'affected_ids': []}

    def investigate(messages):
        context = json.loads(messages[1]['content'])
        assert context['review_resolution'] == {
            'unread_available_source': 'inspect', 'actionable_findings': 'revise',
            'unable_to_judge': 'indeterminate',
        }
        return {'tool': 'read_file', 'path': 'odd/nest'}

    replies = Replies([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), investigate,
        review('revise', findings=[finding], sources=['guide', 'odd/nest']),
        revision(criterion('odd/nest', usability='Nested service persists a request'), sources=['odd/nest']),
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved',
                           'rationale': 'Nested operation now required', 'sources': ['odd/nest'],
                           'duplicate_of': None}], sources=['guide', 'odd/nest']),
    ])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert len(scope.definition.criteria) == 2
    assert scope.finding_ledger.open_ids() == []
    assert budget.scope_revisions == 1 and budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_actual_indeterminate_remains_terminal_with_unused_revision_budget():
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError

    finding = {'category': 'coverage', 'issue': 'Cannot establish coverage from available evidence',
               'sources': ['guide'], 'affected_ids': []}
    budget = AttemptBudget()
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
                       review('indeterminate', findings=[finding])])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'scope_review_indeterminate'
    assert budget.coverage_revisions == 0
    assert budget.calls == 3
    assert budget.scope_contract is None
    assert budget.scope_findings.open_ids() == ['F0001']
