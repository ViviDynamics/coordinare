"""Source review admission by explicit disposition, without prose-based status rewriting."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_scope_contract import SOURCES, Replies, criterion, proposal, revision, run

READ_GUIDE = {'tool': 'read_file', 'path': 'guide'}
UNKNOWN_ID = 'S' + '0'*16


def ids(messages):
    return [c['id'] for c in json.loads(messages[1]['content'])['scope_definition']['criteria']]


def assess(verdict='accept', *, findings=(), decisions=(), sources=None):
    return {'scope_assessment': {'verdict': verdict, 'rationale': 'Independent source judgment',
        'sources': sources or ['guide'], 'findings': list(findings), 'decisions': list(decisions)}}


def fixture_kind(value):
    """An unread_source gap must request an existing path; generic fixture gaps are missing obligations."""
    return 'unread_source' if value.get('inspection_requests') else 'missing_obligation'


def gap_finding(issue='Nested service obligation is unread', **value):
    return {'category': 'coverage', 'issue': issue, 'sources': ['guide'], 'affected_ids': [],
            'gap_kind': fixture_kind(value), 'inspection_requests': [], **value}


def gap(fid='F0001', kind=None, **value):
    return {'finding_id': fid, 'disposition': 'source_gap', 'gap_kind': kind or fixture_kind(value), 'affected_ids': [],
            'inspection_requests': [], 'rationale': 'A concrete source defect remains', 'sources': ['guide'], **value}


def covered(fid, criterion_ids):
    return {'finding_id': fid, 'disposition': 'covered', 'criterion_ids': list(criterion_ids),
            'rationale': 'The criterion states the obligation and its observable success condition', 'sources': ['guide']}


def accept_covered(*fids):
    return lambda messages: assess(decisions=[covered(fid, ids(messages)[:1]) for fid in fids])


def objection(**value):
    return assess('revise', findings=[gap_finding(**value)])


def feedback(replies):
    return json.loads(replies.messages[-1][-1]['content'])['validation_errors']


@pytest.mark.asyncio
async def test_covered_resolves_runtime_only_objection_without_execution():
    replies = Replies([READ_GUIDE, proposal(criterion()),
        objection(issue='Runtime success is not yet proven', gap_kind='incomplete_success_condition'),
        revision(), accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert scope.current and scope.finding_ledger.open_ids() == []
    decision = scope.source_assessment.assessment.decisions[0]
    assert decision.disposition == 'covered' and decision.criterion_ids == [scope.definition.criteria[0].id]
    assert scope.source_assessment.review_sequence == 2 and budget.scope_assessment == scope.source_assessment
    assert budget.probe_batches == 0 and budget.coverage_manifest is None


@pytest.mark.asyncio
async def test_unread_source_gap_holds_with_requests():
    request = {'operation': 'read_file', 'path': 'odd/nest'}
    replies = Replies([READ_GUIDE, proposal(criterion()),
        assess('revise', findings=[gap_finding(inspection_requests=[request])]),
        revision(sources=['odd/nest']),
        assess('revise', decisions=[gap(inspection_requests=[request], sources=['guide'])])])
    budget = AttemptBudget(max_scope_revisions=1)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_scope_revision_limit'
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_contract is None
    assert [binding.finding_id for binding in budget.scope_investigations] == ['F0001', 'F0001']
    assert budget.scope_assessment.assessment.decisions[0].gap_kind == 'unread_source'


@pytest.mark.asyncio
async def test_mixed_gap_stays_open_and_runtime_obligation_remains_required():
    replies = Replies([READ_GUIDE, proposal(criterion()),
        objection(issue='Nested service is omitted and its runtime is unproven', gap_kind='missing_obligation'),
        {'tool': 'read_file', 'path': 'odd/nest'},
        revision(criterion('odd/nest', usability='Service accepts a request and persists a value'), sources=['odd/nest']),
        lambda messages: assess('revise', decisions=[gap(kind='incomplete_success_condition', affected_ids=ids(messages)[:1])],
                                sources=list(SOURCES))])
    budget = AttemptBudget(max_scope_revisions=1)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_scope_revision_limit'
    assert budget.scope_findings.open_ids() == ['F0001']
    assert [c.applicability for c in budget.scope_definition.criteria] == ['required', 'required']


@pytest.mark.asyncio
async def test_old_status_decision_rejected_with_disposition_guidance():
    from coordinare.services.environment_admission import SOURCE_DISPOSITION_GUIDANCE
    old = assess(decisions=[{'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'Claimed resolved',
                             'sources': ['guide'], 'duplicate_of': None, 'inspection_requests': []}])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), old, accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and len(scope.finding_ledger.reviews) == 2
    errors = feedback(replies)
    assert any(error.get('guidance') == SOURCE_DISPOSITION_GUIDANCE for error in errors)
    assert 'Claimed resolved' not in json.dumps(errors)


@pytest.mark.asyncio
async def test_disposition_with_stray_status_gets_disposition_guidance():
    from coordinare.services.environment_admission import SOURCE_DISPOSITION_GUIDANCE
    def stray(messages):
        return assess(decisions=[{**covered('F0001', ids(messages)), 'status': 'resolved'}])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), stray, accept_covered('F0001')])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    assert budget.schema_repairs == 1
    errors = feedback(replies)
    assert any(error['code'] == 'extra_forbidden' and error.get('guidance') == SOURCE_DISPOSITION_GUIDANCE
               for error in errors)


@pytest.mark.asyncio
async def test_stale_criterion_id_rejected_with_current_ids():
    stale = assess(decisions=[covered('F0001', [UNKNOWN_ID])])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), stale, accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and len(scope.finding_ledger.reviews) == 2
    error = feedback(replies)[0]
    assert error['code'] == 'scope_decision_criteria_invalid'
    assert error['location'] == ['scope_assessment', 'decisions', '0', 'criterion_ids']
    assert error['current_criterion_ids'] == [scope.definition.criteria[0].id]


@pytest.mark.asyncio
async def test_resolved_finding_reopened_as_gap_blocks_acceptance():
    def second(messages):
        return assess('revise', findings=[gap_finding(issue='Second source omission')],
                      decisions=[covered('F0001', ids(messages))])

    def reopened(messages):
        return assess('revise', decisions=[gap('F0001', kind='source_conflict'), covered('F0002', ids(messages))])
    response = [{'finding_id': 'F0002', 'rationale': 'Source evidence retained', 'sources': ['guide'], 'affected_ids': []}]
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), second, revision(responses=response), reopened])
    budget = AttemptBudget(max_scope_revisions=2)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_scope_revision_limit'
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_contract is None


@pytest.mark.asyncio
async def test_duplicate_cycle_rejected_atomically():
    two = assess('revise', findings=[gap_finding(), gap_finding(issue='Same nested service under new wording')])
    responses = [{'finding_id': fid, 'rationale': 'Source evidence retained', 'sources': ['guide'], 'affected_ids': []}
                 for fid in ['F0001', 'F0002']]
    def duplicate(fid, target):
        return {'finding_id': fid, 'disposition': 'duplicate', 'duplicate_of': target,
                'rationale': 'Same objection', 'sources': ['guide']}
    cycle = assess(decisions=[duplicate('F0001', 'F0002'), duplicate('F0002', 'F0001')])
    replies = Replies([READ_GUIDE, proposal(criterion()), two, revision(responses=responses), cycle,
                       accept_covered('F0001', 'F0002')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and len(scope.finding_ledger.reviews) == 2


@pytest.mark.asyncio
async def test_accept_with_gap_is_verdict_contradiction():
    replies = Replies([READ_GUIDE, proposal(criterion()), assess(findings=[gap_finding()]), assess()])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1
    assert len(scope.finding_ledger.reviews) == 1 and scope.finding_ledger.records == []


@pytest.mark.asyncio
async def test_source_correction_keeps_required_criteria():
    correction = assess(decisions=[{'finding_id': 'F0001', 'disposition': 'source_correction',
        'rationale': 'The cited guide shows the objection misread the obligation', 'sources': ['guide']}])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), correction])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    reviewed = json.loads(replies.messages[-1][1]['content'])['scope_definition']['criteria']
    assert [c.id for c in scope.definition.criteria] == [c['id'] for c in reviewed]
    assert all(c.applicability == 'required' for c in scope.definition.criteria)
    assert scope.finding_ledger.open_ids() == []


@pytest.mark.asyncio
async def test_missing_decision_holds_within_budget():
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), assess()])
    budget = AttemptBudget(max_schema_repairs=0)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_schema_limit'
    assert budget.scope_contract is None and len(budget.scope_findings.reviews) == 1


FLAT_DEFAULTS = {'criterion_ids': [], 'gap_kind': None, 'affected_ids': [], 'inspection_requests': [], 'duplicate_of': None}


def flat(decision):
    """The provider wire form: every decision field present, unused ones null or empty."""
    return {**FLAT_DEFAULTS, **decision}


def test_scope_schema_flat_decision_wire():
    from coordinare.services.environment_response_schema import (
        constrain_source_criteria,
        response_schema,
    )
    criterion_ids = ['S' + '1'*16, 'S' + '2'*16]
    schema = constrain_source_criteria(response_schema('scope_assessment', review_decision_ids=['F0001', 'F0002']),
                                       criterion_ids)
    definitions = schema['$defs']
    assert not {'CoveredDecision', 'SourceCorrectionDecision', 'SourceGapDecision', 'DuplicateDecision'} & set(definitions)
    decisions = definitions['ScopeAssessmentProposal']['properties']['decisions']
    assert decisions['items'] == {'$ref': '#/$defs/SourceDecisionWire'}
    assert decisions['minItems'] == decisions['maxItems'] == 2
    wire = definitions['SourceDecisionWire']
    assert wire['additionalProperties'] is False and 'status' not in wire['properties']
    assert sorted(wire['required']) == sorted(['finding_id', 'disposition', 'rationale', 'sources', *FLAT_DEFAULTS])
    properties = wire['properties']
    assert properties['disposition']['enum'] == ['covered', 'source_correction', 'source_gap', 'duplicate']
    assert properties['finding_id']['enum'] == ['F0001', 'F0002']
    assert properties['duplicate_of']['anyOf'] == [{'type': 'string', 'enum': ['F0001', 'F0002']}, {'type': 'null'}]
    assert properties['criterion_ids']['items']['enum'] == properties['affected_ids']['items']['enum'] == criterion_ids
    assert properties['sources']['minItems'] == 1
    kinds = next(branch['enum'] for branch in properties['gap_kind']['anyOf'] if branch.get('type') == 'string')
    assert sorted(kinds) == sorted(['missing_obligation', 'unread_source', 'source_conflict', 'unknown_applicability',
                                    'incomplete_runtime_needs', 'incomplete_success_condition', 'remote_state',
                                    'runtime_network'])
    executable = response_schema('assessment', review_decision_ids=['F0001'])
    assert 'disposition' not in json.dumps(executable)
    statuses = {branch['properties']['status']['const'] for branch in executable['$defs']['FindingDecisionProposal']['anyOf']}
    assert statuses == {'open', 'resolved'}


def test_flat_decision_survives_strict_wire_encoding():
    import jsonschema

    from coordinare.services.environment_response_schema import response_schema
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema
    schema = response_schema('scope_assessment', review_decision_ids=['F0001'])
    decision = flat(covered('F0001', ['S' + '1'*16]))
    payload = {'response': assess(decisions=[decision])}
    jsonschema.validate(payload, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(payload), schema)) == assess(decisions=[decision])


@pytest.mark.asyncio
async def test_flat_wire_decisions_admit_through_strict_branches():
    def flat_close(messages):
        return assess(decisions=[flat(covered('F0001', ids(messages)))])
    replies = Replies([READ_GUIDE, proposal(criterion()), assess('revise', findings=[gap_finding()]), revision(), flat_close])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 0 and scope.finding_ledger.open_ids() == []
    assert scope.source_assessment.assessment.decisions[0].disposition == 'covered'


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{'inspection_requests': [{'operation': 'read_file', 'path': 'odd/nest'}]},
                                   {'gap_kind': 'unread_source'}, {'duplicate_of': 'F0001'}])
async def test_flat_wire_keeps_inapplicable_values_invalid(extra):
    def bad(messages):
        return assess(decisions=[{**flat(covered('F0001', ids(messages))), **extra}])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), bad, accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and len(scope.finding_ledger.reviews) == 2


@pytest.mark.asyncio
async def test_flat_gap_without_kind_is_rejected():
    def bad(messages):
        return assess('revise', decisions=[flat({'finding_id': 'F0001', 'disposition': 'source_gap',
            'rationale': 'A concrete source defect remains', 'sources': ['guide']})])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), bad, accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and scope.finding_ledger.open_ids() == []


@pytest.mark.asyncio
async def test_populated_inapplicable_field_gets_disposition_guidance():
    from coordinare.services.environment_admission import SOURCE_DISPOSITION_GUIDANCE

    def bad(messages):
        return assess('revise', decisions=[flat({**gap('F0001'), 'criterion_ids': ids(messages)})])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), bad, accept_covered('F0001')])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    assert budget.schema_repairs == 1
    errors = feedback(replies)
    assert any(error['code'] == 'extra_forbidden' and error['location'][-1] == 'criterion_ids'
               and error.get('guidance') == SOURCE_DISPOSITION_GUIDANCE for error in errors)
    assert 'null or []' in SOURCE_DISPOSITION_GUIDANCE


@pytest.mark.asyncio
async def test_review_instructions_explain_flat_decision_fields():
    replies = Replies([READ_GUIDE, proposal(criterion()), accept_covered()])
    await run(replies)
    system = replies.messages[-1][0]['content']
    assert 'criterion_ids only for covered' in system
    assert 'affected_ids, gap_kind and inspection_requests only for source_gap' in system
    assert 'duplicate_of only for duplicate' in system
    assert 'Every other decision field must be null or []' in system


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition', [['covered'], {'kind': 'covered'}, 7])
async def test_non_string_disposition_is_repairable(disposition):
    def bad(messages):
        return assess(decisions=[{**flat(covered('F0001', ids(messages))), 'disposition': disposition}])
    replies = Replies([READ_GUIDE, proposal(criterion()), objection(), revision(), bad, accept_covered('F0001')])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert budget.schema_repairs == 1 and scope.finding_ledger.open_ids() == []
