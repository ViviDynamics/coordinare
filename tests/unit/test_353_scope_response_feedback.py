"""Rejected source revisions receive actionable evidence repair without weaker admission."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_scope_contract import criterion, proposal, review, revision
from tests.unit.test_353_scope_investigation_flow import discover, objection


@pytest.mark.asyncio
@pytest.mark.parametrize('rejected_sources', [['guide'], ['odd']])
async def test_missing_investigation_citations_are_reported_together_before_retry(rejected_sources):
    sources = {'guide': 'Inspect two operational entry points',
               'odd/one': 'First required operation', 'odd/two': 'Second required operation'}
    budget = AttemptBudget()
    observed = []

    def correct(messages):
        errors = json.loads(messages[-1]['content'])['validation_errors']
        observed.extend(errors)
        return revision(criterion('odd/one'), sources=['odd/one', 'odd/two'])(messages)

    requests = [{'operation': 'read_file', 'path': path} for path in ('odd/one', 'odd/two')]
    scope = await discover([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection(requests)]),
        revision(criterion('odd/one'), sources=rejected_sources), correct,
        review(sources=list(sources), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Inspected operations retained', 'sources': ['odd/one', 'odd/two'], 'duplicate_of': None}]),
    ], sources, budget=budget)
    assert scope.finding_ledger.open_ids() == []
    assert budget.schema_repairs == 1
    assert budget.coverage_revisions == 1
    citations = [e for e in observed if e['code'] == 'scope_investigation_response_invalid']
    assert {e['required_source'] for e in citations} == {'odd/one', 'odd/two'}
    assert {e['finding_id'] for e in citations} == {'F0001'}
    assert all(e['location'] == ['finding_responses', '0', 'sources'] for e in citations)
    assert all(e['guidance'] for e in observed)
    assert len(observed) == (3 if rejected_sources == ['odd'] else 2)


@pytest.mark.asyncio
async def test_directory_citation_reports_its_exact_response_field_without_echoing_rejected_value():
    sources = {'guide': 'Inspect nested source', 'nested/entry': 'Required operation'}
    budget = AttemptBudget()
    observed = []

    def correct(messages):
        errors = json.loads(messages[-1]['content'])['validation_errors']
        observed.extend(errors)
        return revision(sources=['guide'])(messages)

    scope = await discover([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([])]),
        revision(sources=['nested']), correct,
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Required obligation retained', 'sources': ['guide'], 'duplicate_of': None}]),
    ], sources, budget=budget)
    assert scope.finding_ledger.open_ids() == []
    assert budget.schema_repairs == 1
    assert observed[0]['location'] == ['finding_responses', '0', 'sources', '0']
    assert observed[0]['finding_id'] == 'F0001'
    assert 'inspected file' in observed[0]['guidance']
    assert 'nested' not in json.dumps(observed)


@pytest.mark.asyncio
async def test_many_invalid_citations_keep_correction_inside_original_context_budget():
    sources = {'guide': 'Required operation'}
    observed = []

    def correct(messages):
        observed.extend(json.loads(messages[-1]['content'])['validation_errors'])
        return revision(sources=['guide'])(messages)

    rejected_sources = [f'unread/{i}' for i in range(3500)]
    scope = await discover([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([])]),
        revision(sources=rejected_sources), correct,
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Source obligation retained', 'sources': ['guide'], 'duplicate_of': None}]),
    ], sources)
    assert scope.finding_ledger.open_ids() == []
    assert len(json.dumps(observed).encode()) < 8000
    assert observed[0]['invalid_source_count'] == 3500
    assert observed[0]['invalid_source_ranges'] == [[0, 3499]]
    assert observed[0]['omitted_source_range_count'] == 0


@pytest.mark.asyncio
async def test_sparse_invalid_citation_ranges_are_bounded_and_report_omitted_errors():
    from coordinare_service_inference.environment.findings import FindingResponse

    from coordinare.services.environment_findings import FindingValidationError
    from coordinare.services.environment_scope import _validate_responses

    scope = await discover([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[objection([])]), revision(sources=['guide']),
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Required operation retained', 'sources': ['guide'], 'duplicate_of': None}]),
    ], {'guide': 'Required operation'})
    inputs = {**scope.definition.inputs, **{f'known/{i}': 'a'*64 for i in range(3500)}}
    response = FindingResponse(finding_id='F0001', rationale='Inspected evidence', affected_ids=[],
        sources=[path for i in range(3500) for path in (f'unread/{i}', f'known/{i}')])
    with pytest.raises(FindingValidationError) as rejected:
        _validate_responses([response], ledger=scope.finding_ledger, inputs=inputs, definition=scope.definition)
    errors = rejected.value.errors
    assert len(json.dumps(errors).encode()) < 8000
    assert errors[0]['invalid_source_count'] == 3500
    assert errors[0]['invalid_source_ranges'] == [[2*i, 2*i] for i in range(32)]
    assert errors[0]['omitted_source_range_count'] == 3468


@pytest.mark.asyncio
@pytest.mark.parametrize('revising', [False, True])
async def test_unread_criterion_citations_request_inspection_together_before_resubmission(revising):
    sources = {'guide': 'Inspect both nested operation definitions',
               'odd/one': 'First operation', 'odd/two': 'Second operation'}
    observed = []
    budget = AttemptBudget()
    candidate = [criterion('odd/one'), criterion('odd/two', usability='Second operation succeeds')]

    def inspect(messages):
        observed.extend(json.loads(messages[-1]['content'])['validation_errors'])
        return {'tool': 'read_files', 'paths': ['odd/one', 'odd/two']}

    if revising:
        values = [{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
            review('revise', findings=[objection([])]), revision(*candidate, sources=['guide']), inspect,
            revision(*candidate, sources=['odd/one', 'odd/two']),
            review(sources=list(sources), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
                'rationale': 'Inspected operations retained', 'sources': ['odd/one', 'odd/two'], 'duplicate_of': None}])]
    else:
        values = [{'tool': 'read_file', 'path': 'guide'}, proposal(*candidate), inspect,
                  proposal(*candidate), review(sources=list(sources))]
    scope = await discover(values, sources, budget=budget)
    assert scope.finding_ledger.open_ids() == []
    assert budget.schema_repairs == 1
    prefix = ['scope_revision', 'add'] if revising else ['scope', 'criteria']
    assert [error['location'] for error in observed] == [[*prefix, '0', 'sources', '0'], [*prefix, '1', 'sources', '0']]
    assert all(error['code'] == 'scope_citations_invalid' for error in observed)
    assert all('read_file' in error['guidance'] for error in observed)


@pytest.mark.asyncio
async def test_reassessment_reports_all_unread_sources_in_one_criterion_before_inspection():
    sources = {'guide': 'Applicability requires nested evidence', 'odd/one': 'Required operation', 'odd/two': 'Operation details'}
    observed = []
    budget = AttemptBudget()
    replacement = {**criterion('odd/one'), 'sources': ['odd/one', 'odd/two']}

    def revise(messages):
        base = json.loads(messages[1]['content'])['scope_definition']
        return revision(reassess=[{'criterion_id': base['criteria'][0]['id'], 'criterion': replacement}], sources=['guide'])(messages)

    def inspect(messages):
        observed.extend(json.loads(messages[-1]['content'])['validation_errors'])
        return {'tool': 'read_files', 'paths': ['odd/one', 'odd/two']}

    scope = await discover([
        {'tool': 'read_file', 'path': 'guide'}, proposal(criterion(applicability='unknown')),
        review('revise', findings=[objection([])]), revise, inspect, revise,
        review(sources=list(sources), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Required operation established', 'sources': ['odd/one', 'odd/two'], 'duplicate_of': None}]),
    ], sources, budget=budget)
    assert scope.definition.criteria[0].applicability == 'required'
    assert scope.finding_ledger.open_ids() == []
    assert len(observed) == 1 and observed[0]['invalid_source_indices'] == ['0', '1']
    assert observed[0]['location'] == ['scope_revision', 'reassess', '0', 'criterion', 'sources', '0']
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_all_invalid_reassessment_targets_report_reasons_and_preserve_valid_operations():
    sources = {'guide': 'Required operation', 'optional/one': 'First required operation',
               'optional/two': 'Second required operation'}
    observed = []
    budget = AttemptBudget()
    extra = criterion('guide', usability='Additional required operation succeeds')

    def proposed(messages, *, invalid):
        base = json.loads(messages[1]['content'])['scope_definition']
        ids = [c['id'] for c in base['criteria']]
        valid = [{'criterion_id': ids[1], 'criterion': criterion('optional/one')},
                 {'criterion_id': ids[2], 'criterion': criterion('optional/two')}]
        changes = ([{'criterion_id': ids[0], 'criterion': criterion()},
                    {'criterion_id': 'S'+'f'*16, 'criterion': criterion()},
                    valid[0], valid[0], valid[1]] if invalid else valid)
        return revision(extra, reassess=changes, sources=list(sources))(messages)

    def bad(messages):
        return proposed(messages, invalid=True)

    def correct(messages):
        observed.extend(json.loads(messages[-1]['content'])['validation_errors'])
        return proposed(messages, invalid=False)

    scope = await discover([
        {'tool': 'read_files', 'paths': list(sources)},
        proposal(criterion(), criterion('optional/one', applicability='unknown'),
                 criterion('optional/two', applicability='not_applicable')),
        review('revise', findings=[objection([])], sources=list(sources)), bad, correct,
        review(sources=list(sources), decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'Required operations retained and additions included', 'sources': list(sources), 'duplicate_of': None}]),
    ], sources, budget=budget)
    assert len(scope.definition.criteria) == 4
    assert all(c.applicability == 'required' for c in scope.definition.criteria)
    assert budget.schema_repairs == 1
    assert [error['location'] for error in observed] == [
        ['scope_revision', 'reassess', str(i), 'criterion_id'] for i in (0, 1, 3)]
    assert [error['reason'] for error in observed] == ['required', 'unknown', 'duplicate']
    assert all(error['code'] == 'scope_revision_target' and error['guidance'] for error in observed)
    assert 'S'+'f'*16 not in json.dumps(observed)
    assert len(observed[0]['allowed_reassessment_ids']) == 2
