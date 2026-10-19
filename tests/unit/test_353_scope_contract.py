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


def review(verdict='accept', *, findings=None, decisions=None, sources=None):
    return {'scope_assessment': {'verdict': verdict, 'rationale': 'Independently inspected source coverage',
        'sources': sources or ['guide'], 'findings': findings or [], 'decisions': decisions or []}}


class Replies:
    def __init__(self, values):
        self.values = iter(values)
        self.messages = []
        self.schemas = []

    async def __call__(self, messages, *, response_schema):
        self.messages.append(copy.deepcopy(messages))
        self.schemas.append(response_schema)
        return json.dumps(next(self.values))


async def run(replies, *, budget=None, fetch=None, paths=None):
    async def default_fetch(path):
        return SOURCES.get(path)
    return await api()(paths=paths or list(SOURCES), fetch=fetch or default_fetch,
        chat=replies, commit=COMMIT, limits=DiscoveryLimits(), budget=budget or AttemptBudget())


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
        {**proposal(criterion(), criterion('odd/nest', usability='Service accepts a request and persists a value')),
         'finding_responses': [{'finding_id': 'F0001', 'rationale': 'Added source-backed operational obligation',
                               'sources': ['odd/nest'], 'affected_ids': []}]},
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
    assert correction['validation_errors'] == [{'code': 'scope_citations_invalid', 'location': expected}]
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
