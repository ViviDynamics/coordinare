"""Source-owned coverage identity cannot be weakened by an executable candidate."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits

COMMIT = 'a'*40
SOURCES = {'guide': 'Develop locally. See odd/nest for operational service requirements.',
           'odd/nest': 'A local service must accept a request and persist a value; a CLI version is insufficient.'}


def api():
    from coordinare.services.environment_scope import discover_scope
    return discover_scope


def criterion(path='guide', *, usability='The documented development command succeeds', applicability='required'):
    return {'description': 'Development environment', 'sources': [path], 'applicability': applicability,
            'rationale': 'Documented development obligation', 'usability': usability, 'runtime_needs': []}


def proposal(*criteria):
    return {'scope': {'criteria': list(criteria)}, 'finding_responses': []}


WIRE_DECISION_DEFAULTS = {'criterion_ids': [], 'gap_kind': None, 'affected_ids': [], 'inspection_requests': [],
                          'duplicate_of': None}


def source_decision(value):
    """Provider wire form of a fixture decision: one flat object, unused fields null or empty.

    'status' is shorthand for the disposition these legacy fixtures intend.
    """
    value = dict(value)
    if 'disposition' not in value:
        status, duplicate, requests = value.pop('status'), value.pop('duplicate_of', None), value.pop('inspection_requests', [])
        if status == 'open':
            value.update(disposition='source_gap', gap_kind='unread_source' if requests else 'missing_obligation',
                         inspection_requests=requests)
        else:
            assert not requests, 'resolving fixture decisions cannot request inspection'
            value.update(disposition='duplicate' if duplicate is not None else 'source_correction', duplicate_of=duplicate)
    return {**WIRE_DECISION_DEFAULTS, **value}


def source_finding(value):
    return {'gap_kind': 'missing_obligation', **value, 'inspection_requests': value.get('inspection_requests', [])}


def review(verdict='accept', *, findings=None, decisions=None, sources=None):
    return {'scope_assessment': {'verdict': verdict, 'rationale': 'Independently inspected source coverage',
        'sources': sources or ['guide'], 'findings': [source_finding(value) for value in findings or []],
        'decisions': [source_decision(value) for value in decisions or []]}}


class Replies:
    def __init__(self, values):
        self.values = iter(values)
        self.messages = []
        self.schemas = []

    async def __call__(self, messages, *, response_schema):
        self.messages.append(copy.deepcopy(messages))
        self.schemas.append(response_schema)
        value = next(self.values)
        return json.dumps(value(messages) if callable(value) else value)


async def run(replies, *, budget=None, fetch=None, paths=None, entries=None):
    async def default_fetch(path):
        return SOURCES.get(path)
    return await api()(paths=paths or list(SOURCES), fetch=fetch or default_fetch,
        chat=replies, commit=COMMIT, limits=DiscoveryLimits(), budget=budget or AttemptBudget(), entries=entries)


@pytest.mark.asyncio
async def test_scope_is_reviewed_fresh_before_any_executable_candidate():
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        {'tool': 'read_file', 'path': 'odd/nest'}, review(sources=list(SOURCES))])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert scope.definition.criteria[0].id.startswith('S')
    assert scope.all_inputs == {p: hashlib.sha256(v.encode()).hexdigest() for p, v in SOURCES.items()}
    assert scope.review_inputs == scope.all_inputs
    assert scope.definition.source_commit == COMMIT
    assert scope.definition.inventory_digest == hashlib.sha256(json.dumps(sorted(SOURCES), separators=(',', ':')).encode()).hexdigest()
    reviewer = replies.messages[2]
    assert len(reviewer) == 2 and all(m['role'] != 'assistant' for m in reviewer)
    content = json.loads(reviewer[1]['content'])
    assert 'manifest' not in content and 'execution_capabilities' not in content
    assert content['scope_definition']['criteria'][0]['id'] == scope.definition.criteria[0].id
    assert budget.calls == 4 and budget.reads == 3
    assert budget.scope_contract.digest() == scope.digest()


@pytest.mark.asyncio
async def test_nested_operational_obligation_survives_candidate_convenience():
    finding = {'category': 'coverage', 'issue': 'Nested operational service missing',
               'sources': ['odd/nest'], 'affected_ids': []}
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        {'tool': 'read_file', 'path': 'odd/nest'}, review('revise', findings=[finding], sources=list(SOURCES)),
        {'tool': 'read_file', 'path': 'odd/nest'},
        revision(criterion('odd/nest', usability='Service accepts a request and persists a value'), sources=['odd/nest']),
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'Required operation retained',
                           'sources': ['odd/nest'], 'duplicate_of': None}], sources=list(SOURCES))])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert len(scope.definition.criteria) == 2
    assert all(c.applicability == 'required' for c in scope.definition.criteria)
    assert scope.finding_ledger.open_ids() == []
    assert len(scope.finding_ledger.reviews) == 2
    assert scope.finding_ledger.records[0].id == 'F0001'
    assert budget.coverage_revisions == 1
    assert scope.definition.criteria[0].id == json.loads(replies.messages[2][1]['content'])['scope_definition']['criteria'][0]['id']


@pytest.mark.asyncio
async def test_unknown_applicability_cannot_be_accepted():
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion(applicability='unknown')), review()])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies)
    assert failure.value.code == 'scope_applicability_unknown'


@pytest.mark.asyncio
async def test_review_cannot_accept_with_an_open_finding():
    finding = {'category': 'coverage', 'issue': 'Operational evidence missing', 'sources': ['guide'], 'affected_ids': []}
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review(findings=[finding]), review('indeterminate', findings=[finding])])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies)
    assert failure.value.code == 'scope_review_indeterminate'


@pytest.mark.asyncio
async def test_schema_errors_spend_existing_allowance_without_reset():
    budget = AttemptBudget(schema_repairs=2)
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, {'scope': {'criteria': []}}])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_schema_limit'
    assert budget.calls == 2 and budget.schema_repairs == 2


@pytest.mark.asyncio
async def test_review_source_drift_holds_without_repair():
    reads = 0
    async def fetch(path):
        nonlocal reads
        reads += 1
        return SOURCES[path] + ('changed' if reads > 1 else '')
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion())])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, fetch=fetch)
    assert failure.value.code == 'source_identity'


@pytest.mark.asyncio
async def test_scope_identity_changes_with_inventory_or_new_reviewer_input():
    async def accepted(paths, reads):
        replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
                           *reads, review()])
        return await run(replies, paths=paths)
    first = await accepted(['guide'], [])
    second = await accepted(list(SOURCES), [])
    third = await accepted(list(SOURCES), [{'tool': 'read_file', 'path': 'odd/nest'}])
    assert len({s.digest() for s in (first, second, third)}) == 3
    for field, value in [('source_commit', 'main'), ('inventory_digest', 'invalid')]:
        data = first.model_dump(mode='json')
        data['definition'][field] = value
        with pytest.raises(ValidationError):
            type(first).model_validate(data)


@pytest.mark.asyncio
async def test_scope_preparation_reads_are_inside_the_original_deadline():
    import asyncio
    import time

    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion())])
    budget = AttemptBudget(timeout_seconds=0.05)
    async def fetch(path):
        await asyncio.sleep(1)
        return SOURCES[path]
    started = time.monotonic()
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget, fetch=fetch)
    assert failure.value.code == 'attempt_deadline'
    assert time.monotonic()-started < 0.5


@pytest.mark.asyncio
async def test_rejecting_scope_and_its_revision_charge_are_one_durable_checkpoint():
    class Crash(BaseException):
        pass
    finding = {'category': 'coverage', 'issue': 'Missing nested operation', 'sources': ['guide'], 'affected_ids': []}
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review('revise', findings=[finding])])
    budget = AttemptBudget()
    async def checkpoint():
        if budget.scope_findings and budget.scope_findings.reviews:
            assert budget.coverage_revisions == 1
            raise Crash
    budget.checkpoint = checkpoint
    with pytest.raises(Crash):
        await run(replies, budget=budget)


def test_native_scope_function_transfers_finding_responses_at_the_envelope_boundary():
    from coordinare.services.environment_function_wire import decode_function, functions
    from coordinare.services.environment_response_schema import response_schema

    schema = response_schema('scope')
    function = functions(schema)[-1]['function']
    assert 'finding_responses' in function['parameters']['properties']
    assert 'finding_responses' in function['parameters']['required']
    arguments = {'criteria': [criterion()], 'finding_responses': []}
    raw = json.dumps({'content': None, 'tool_calls': [{'type': 'function', 'function': {
        'name': 'submit_environment_scope', 'arguments': json.dumps(arguments)}}]})
    assert json.loads(decode_function(raw, schema)) == proposal(criterion())


@pytest.mark.asyncio
async def test_scope_keeps_the_existing_phase_call_bound_with_a_larger_shared_budget():
    async def fetch(path):
        return SOURCES[path]
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()])
    budget = AttemptBudget(max_calls=128)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await api()(paths=list(SOURCES), fetch=fetch, chat=replies, commit=COMMIT,
            limits=DiscoveryLimits(max_model_calls=1), budget=budget)
    assert failure.value.code == 'scope_phase_call_limit'
    assert budget.calls == 1


@pytest.mark.asyncio
async def test_source_failure_is_held_with_a_trusted_code_not_raw_provider_text():
    async def failed(path):
        raise OSError('credential=private-provider-value')
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(Replies([{'tool': 'read_file', 'path': 'guide'}]), budget=budget, fetch=failed)
    assert failure.value.code == 'scope_request_failed'
    assert budget.error_code == 'scope_request_failed'
    assert 'private-provider-value' not in str(failure.value)
    assert budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_scope_prompts_deliver_exact_schema_to_canonical_json_object_transport():
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()])
    await run(replies)
    assert 'ScopeEnvelope' in replies.messages[1][0]['content']
    assert 'ScopeAssessmentEnvelope' in replies.messages[2][0]['content']
    assert '"finding_responses"' in replies.messages[1][0]['content']


def test_scope_wire_schema_publishes_existing_rationale_bounds():
    from coordinare_service_inference.environment.scope import (
        ScopeAssessmentEnvelope,
        ScopeEnvelope,
    )

    for envelope, definition in ((ScopeEnvelope, 'ScopeCriterionProposal'),
                                 (ScopeAssessmentEnvelope, 'ScopeAssessmentProposal')):
        schema = envelope.model_json_schema()['$defs'][definition]['properties']['rationale']
        assert schema.get('maxLength') == 2048
        assert schema.get('minLength') == 1
        assert 'UTF-8' in schema.get('description', '')


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['author', 'reviewer'])
async def test_unread_citation_correction_identifies_exact_element_without_echoing_source(phase):
    bad_path = 'odd/private-candidate-description'
    bad = proposal(criterion(bad_path)) if phase == 'author' else review(sources=[bad_path])
    values = [{'tool': 'read_file', 'path': 'guide'}]
    if phase == 'reviewer':
        values.append(proposal(criterion()))
    values.extend([bad, proposal(criterion()) if phase == 'author' else review()])
    if phase == 'author':
        values.append(review())
    replies = Replies(values)
    budget = AttemptBudget()
    result = await run(replies, budget=budget)
    correction = next(json.loads(m['content']) for call in replies.messages for m in call
                      if m['role'] == 'user' and 'validation_errors' in json.loads(m['content']))
    expected = (['scope', 'criteria', '0', 'sources', '0'] if phase == 'author'
                else ['scope_assessment', 'sources', '0'])
    errors = correction['validation_errors']
    assert [{key: error[key] for key in ('code', 'location')} for error in errors] == [{'code': 'scope_citations_invalid', 'location': expected}]
    assert all(error['guidance'] for error in errors)
    assert bad_path not in json.dumps(correction)
    assert budget.schema_repairs == 1 and budget.coverage_revisions == 0
    assert result.definition.criteria[0].sources == ['guide']


def test_scope_rationale_feedback_reports_length_constraint_without_echoing_text():
    from coordinare_service_inference.environment.scope import ScopeAssessmentEnvelope

    from coordinare.services.environment_admission import _validation_feedback

    value = review()
    value['scope_assessment']['rationale'] = 'private-evidence-' * 200
    with pytest.raises(ValidationError) as failure:
        ScopeAssessmentEnvelope.model_validate(value)
    errors = _validation_feedback(failure.value)
    assert errors == [{'location': ['scope_assessment', 'rationale'], 'code': 'string_too_long'}]
    assert 'private-evidence' not in json.dumps(errors)



def revision(*add, reassess=None, sources=None, responses=None):
    def build(messages):
        from coordinare_service_inference.environment.scope import ScopeDefinition
        context = json.loads(messages[1]['content'])
        base = ScopeDefinition.model_validate(context['scope_definition'])
        return {'scope_revision': {'base_digest': base.digest(), 'add': list(add), 'reassess': reassess or []},
                'finding_responses': responses if responses is not None else [{'finding_id': 'F0001',
                    'rationale': 'Source-backed response to the retained finding', 'sources': sources or ['guide'], 'affected_ids': []}]}
    return build


def objection():
    return review('revise', findings=[{'category': 'coverage', 'issue': 'Documented coverage needs investigation',
        'sources': ['guide'], 'affected_ids': []}])


def closure():
    return review(decisions=[{'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'Source evidence resolves coverage',
        'sources': ['guide'], 'duplicate_of': None}])


@pytest.mark.asyncio
async def test_empty_revision_defense_requires_fresh_whole_review():
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection(), revision(), closure()])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    assert len(scope.definition.criteria) == 1 and scope.finding_ledger.open_ids() == []
    assert len(scope.finding_ledger.reviews) == 2 and budget.coverage_revisions == 1
    context = json.loads(replies.messages[-1][1]['content'])
    assert context['finding_ledger']['records'][0]['id'] == 'F0001'
    assert context['scope_definition'] == scope.definition.model_dump(mode='json')
    assert len(replies.messages[-1]) == 2
    assert budget.schema_repairs == 0
    assert 'scope_revision' in replies.schemas[3]['anyOf'][-1]['properties']


@pytest.mark.asyncio
async def test_unknown_reassessment_uses_source_then_independent_review():
    from coordinare_service_inference.environment.scope import ScopeCriterionProposal, criterion_id
    old = criterion(applicability='unknown')
    identifier = criterion_id(ScopeCriterionProposal.model_validate(old))
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(old), objection(),
        revision(reassess=[{'criterion_id': identifier, 'criterion': criterion()}]), closure()])
    scope = await run(replies)
    assert scope.definition.criteria[0].applicability == 'required'
    assert scope.definition.criteria[0].id != identifier
    assert len(scope.finding_ledger.reviews) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'duplicate', 'unknown', 'stale_id', 'unread', 'full', 'stale_base', 'late_invalid', 'citation'])
async def test_invalid_revision_cannot_publish_partial_definition_or_responses(case):
    from coordinare_service_inference.environment.scope import ScopeCriterionProposal, criterion_id
    old = criterion(applicability='unknown')
    identifier = criterion_id(ScopeCriterionProposal.model_validate(old))
    build = revision(criterion(usability='New operation'), reassess=[{'criterion_id': identifier, 'criterion': criterion()}])
    def bad(messages):
        value = build(messages)
        response = value['finding_responses'][0]
        if case == 'missing':
            value['finding_responses'] = []
        elif case == 'duplicate':
            value['finding_responses'].append(dict(response))
        elif case == 'unknown':
            response['finding_id'] = 'F9999'
        elif case == 'stale_id':
            response['affected_ids'] = [identifier]
        elif case == 'unread':
            response['sources'] = ['odd/nest']
        elif case == 'full':
            value = proposal(criterion())
        elif case == 'stale_base':
            value['scope_revision']['base_digest'] = '0'*64
        elif case == 'late_invalid':
            value['scope_revision']['reassess'].append({'criterion_id': 'S'+'0'*16, 'criterion': criterion()})
        elif case == 'citation':
            value['scope_revision']['add'][0]['sources'] = ['odd/nest']
        return value
    budget = AttemptBudget(max_schema_repairs=0)
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(old), objection(), bad])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_schema_limit'
    assert budget.scope_definition.criteria[0].id == identifier
    assert len(budget.scope_definition.criteria) == 1 and budget.scope_responses == []
    assert budget.scope_findings.open_ids() == ['F0001']
    assert budget.calls == 4 and budget.coverage_revisions == 1
    expected = {'stale_base': 'scope_revision_base', 'late_invalid': 'scope_revision_target',
                'citation': 'scope_citations_invalid', 'full': 'missing'}.get(case, 'scope_response_ids_invalid')
    assert expected in budget.validation_codes


@pytest.mark.asyncio
async def test_revision_citation_feedback_is_precise_without_echoing_evidence():
    def bad(messages):
        return revision(criterion('odd/nest'))(messages)
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection(), bad, revision(), closure()])
    budget = AttemptBudget()
    await run(replies, budget=budget)
    feedback = json.loads(replies.messages[4][-1]['content'])
    errors = feedback['validation_errors']
    assert [{key: error[key] for key in ('code', 'location')} for error in errors] == [{'location': ['scope_revision', 'add', '0', 'sources', '0'], 'code': 'scope_citations_invalid'}]
    assert errors[0]['invalid_source_indices'] == ['0']
    assert 'read_file' in errors[0]['guidance']
    assert feedback['expected_envelope'] == 'scope_revision'
    assert 'odd/nest' not in json.dumps(feedback)
    assert budget.schema_repairs == 1


@pytest.mark.asyncio
async def test_fresh_review_can_find_omission_beyond_revision():
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection(),
        revision(criterion(usability='Added operation')), review('revise', findings=[{'category': 'coverage',
        'issue': 'Another omitted operation', 'sources': ['guide'], 'affected_ids': []}],
        decisions=[{'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'First addressed', 'sources': ['guide'], 'duplicate_of': None}])])
    budget = AttemptBudget(max_coverage_revisions=1)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=budget)
    assert failure.value.code == 'attempt_coverage_limit'
    assert budget.scope_findings.open_ids() == ['F0002'] and budget.coverage_revisions == 1
    assert len(budget.scope_definition.criteria) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('count', [1, 8])
async def test_revision_reviewer_gets_exact_required_decision_ids_in_context_and_feedback(count):
    identifiers = [f'F{i+1:04}' for i in range(count)]
    first = objection()
    first['scope_assessment']['findings'] = [{**first['scope_assessment']['findings'][0], 'issue': f'Obligation {i} missing'} for i in range(count)]
    responses = [
        {'finding_id': fid, 'rationale': 'Source evidence defends the retained definition', 'sources': ['guide'], 'affected_ids': []} for fid in identifiers]
    final = closure()
    final['scope_assessment']['decisions'] = [{**final['scope_assessment']['decisions'][0], 'finding_id': fid} for fid in identifiers]
    def incomplete(messages):
        context = json.loads(messages[1]['content'])
        assert context['required_decision_ids'] == identifiers
        return review('revise', findings=[{'category': 'coverage', 'issue': 'Another uncovered operation',
            'sources': ['guide'], 'affected_ids': []}])
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), first,
        revision(responses=responses), incomplete, final])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    feedback = json.loads(replies.messages[-1][-1]['content'])
    assert feedback['validation_errors'] == [{'location': ['scope_assessment', 'decisions'],
        'code': 'review_findings_invalid', 'required_decision_ids': identifiers}]
    assert scope.finding_ledger.open_ids() == [] and len(scope.finding_ledger.records) == count
    assert budget.schema_repairs == 1 and len(scope.finding_ledger.reviews) == 2


@pytest.mark.asyncio
async def test_scope_review_wire_cannot_adjudicate_findings_before_daemon_assigns_ids():
    import jsonschema
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()])
    await run(replies)
    schema = replies.schemas[-1]
    jsonschema.validate(objection(), schema)
    invented = objection()
    invented['scope_assessment']['decisions'] = closure()['scope_assessment']['decisions']
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invented, schema)


@pytest.mark.asyncio
async def test_retained_scope_review_wire_requires_daemon_assigned_finding_ids():
    import jsonschema
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), objection(), revision(), closure()])
    await run(replies)
    schema = replies.schemas[-1]
    jsonschema.validate(closure(), schema)
    for invalid in [review(), closure()]:
        if invalid['scope_assessment']['decisions']:
            invalid['scope_assessment']['decisions'][0]['finding_id'] = 'F9999'
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)


@pytest.mark.asyncio
async def test_reassessed_response_feedback_exposes_current_ids_without_accepting_stale_ids():
    from coordinare_service_inference.environment.scope import ScopeCriterionProposal, criterion_id
    old = criterion(applicability='unknown')
    old_id = criterion_id(ScopeCriterionProposal.model_validate(old))
    new_id = criterion_id(ScopeCriterionProposal.model_validate(criterion()))
    good = revision(reassess=[{'criterion_id': old_id, 'criterion': criterion()}])
    def bad(messages):
        value = good(messages)
        value['finding_responses'][0]['affected_ids'] = [old_id]
        return value
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(old), objection(), bad, good, closure()])
    budget = AttemptBudget()
    result = await run(replies, budget=budget)
    feedback = json.loads(replies.messages[4][-1]['content'])['validation_errors'][0]
    assert feedback['location'] == ['finding_responses', '0', 'affected_ids']
    assert feedback['code'] == 'scope_response_ids_invalid'
    assert new_id in feedback['guidance'] and old_id not in feedback['guidance']
    assert '[]' in feedback['guidance']
    assert budget.schema_repairs == 1 and len(result.finding_ledger.reviews) == 2
    assert result.definition.criteria[0].id == new_id


@pytest.mark.asyncio
@pytest.mark.parametrize('omit_resolved', [False, True])
async def test_scope_review_readjudicates_resolved_findings_after_another_revision(omit_resolved):
    """A later candidate must allow fresh decisions on every retained objection."""
    import jsonschema

    first = objection()
    first['scope_assessment']['findings'].append({
        'category': 'coverage', 'issue': 'Second required operation missing',
        'sources': ['guide'], 'affected_ids': [], 'gap_kind': 'missing_obligation', 'inspection_requests': []})
    resolved = closure()['scope_assessment']['decisions'][0]
    partial = review('revise', decisions=[resolved, {**resolved, 'finding_id': 'F0002', 'disposition': 'source_gap',
        'gap_kind': 'missing_obligation', 'affected_ids': [], 'inspection_requests': []}])
    responses = [{'finding_id': fid, 'rationale': 'Source evidence retained',
                  'sources': ['guide'], 'affected_ids': []} for fid in ['F0001', 'F0002']]
    final = review(decisions=[resolved, {**resolved, 'finding_id': 'F0002'}])

    def final_review(messages):
        context = json.loads(messages[1]['content'])
        assert context['required_decision_ids'] == ['F0001', 'F0002']
        return final

    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), first,
        revision(responses=responses), partial, revision(responses=responses),
        *([review(decisions=[{**resolved, 'finding_id': 'F0002'}])] if omit_resolved else []), final_review])
    budget = AttemptBudget()
    scope = await run(replies, budget=budget)
    jsonschema.validate(final, replies.schemas[-1])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(review(decisions=[{**resolved, 'finding_id': 'F0002'}]), replies.schemas[-1])
    assert scope.finding_ledger.open_ids() == []
    assert len(scope.finding_ledger.reviews) == 3
    assert budget.coverage_revisions == 2 and budget.schema_repairs == int(omit_resolved)
    if omit_resolved:
        feedback = json.loads(replies.messages[-1][-1]['content'])['validation_errors'][0]
        assert feedback['required_decision_ids'] == ['F0001', 'F0002']
