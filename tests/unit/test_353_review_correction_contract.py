"""Reviewer corrections expose the real constraint without closing live objections."""
from __future__ import annotations

import copy
import hashlib
import json

import jsonschema
import pytest
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.models import CoverageAssessmentProposal

from coordinare.services.environment_findings import (
    FindingValidationError,
    ReviewSubject,
    apply_evidence_review,
)
from coordinare.services.environment_response_schema import response_schema

COMMIT = 'a' * 40
DIGEST = 'b' * 64
INPUTS = {'guide': hashlib.sha256(b'Pinned requirement').hexdigest()}


def ledger(count=2):
    records = [{'id': f'F{i:04}', 'proposal': {'category': 'coverage', 'issue': f'Required coverage {i}',
        'sources': ['guide'], 'affected_ids': []}, 'manifest_digest': DIGEST, 'source_commit': COMMIT,
        'inputs': INPUTS} for i in range(1, count + 1)]
    return FindingLedger.model_validate({'source_commit': COMMIT, 'records': records,
        'reviews': [{'sequence': 1, 'manifest_digest': DIGEST, 'inputs': INPUTS,
                     'decisions': [], 'added_ids': [r['id'] for r in records]}]})


def assessment(count=2):
    return {'verdict': 'revise', 'rationale': 'Original requirement remains blocking', 'sources': ['guide'],
        'capability_unavailable': False, 'findings': [], 'decisions': [
            {'finding_id': f'F{i:04}', 'status': 'open', 'rationale': 'Required source coverage is absent',
             'sources': ['guide'], 'duplicate_of': None} for i in range(1, count + 1)]}


def apply(current, data):
    return apply_evidence_review(current, subject=ReviewSubject(COMMIT, DIGEST, set()),
        proposal=CoverageAssessmentProposal.model_validate(data), reviewer_inputs=INPUTS)


def test_invalid_duplicate_reports_exact_field_and_correction_without_mutation():
    current = ledger()
    before = current.canonical()
    data = assessment()
    data['decisions'][1]['duplicate_of'] = 'F0001'
    with pytest.raises(FindingValidationError) as error:
        apply(current, data)
    assert error.value.location == ('decisions', '1', 'status')
    assert 'resolved' in str(error.value)
    assert current.canonical() == before
    data['decisions'][1]['status'] = 'resolved'
    assert apply(current, data).open_ids() == ['F0001']


@pytest.mark.parametrize('target', ['F0002', 'F0099'])
def test_duplicate_target_refusal_identifies_its_field(target):
    data = assessment()
    data['decisions'][1].update(status='resolved', duplicate_of=target)
    with pytest.raises(FindingValidationError) as error:
        apply(ledger(), data)
    assert error.value.location == ('decisions', '1', 'duplicate_of')


def _wire_assessment(phase, count=2):
    from tests.unit.test_353_scope_contract import source_decision
    data = assessment(count)
    if phase == 'scope_assessment':
        data.pop('capability_unavailable')
        data['decisions'] = [source_decision(decision) for decision in data['decisions']]
    else:
        # Strict wires require every property; with no repair scope the rulings list is empty.
        data['criterion_rulings'] = []
    return data


def _duplicate(phase, decision, target):
    if phase == 'scope_assessment':
        from tests.unit.test_353_scope_contract import source_decision
        keep = {key: decision[key] for key in ('finding_id', 'rationale', 'sources')}
        return source_decision({**keep, 'status': 'resolved', 'duplicate_of': target})
    return {**decision, 'status': 'resolved', 'duplicate_of': target}


@pytest.mark.parametrize('phase', ['scope_assessment', 'assessment'])
def test_provider_schema_refuses_open_duplicate_and_preserves_valid_open_survivor(phase):
    from coordinare.services.environment_structured_wire import decode_wire, wire_schema

    options = {'review_decision_ids': ['F0001', 'F0002']}
    schema = response_schema(phase, **options)
    data = _wire_assessment(phase)
    open_decision = data['decisions'][1]
    data['decisions'][1] = {**open_decision, 'duplicate_of': 'F0001'}
    if phase == 'scope_assessment':
        # The flat provider wire cannot express branch rules; strict local validation refuses it.
        from coordinare_service_inference.environment.scope import (
            ScopeAssessmentEnvelope,
            normalize_source_assessment,
        )
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ScopeAssessmentEnvelope.model_validate(normalize_source_assessment({phase: data}))
    else:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({'response': {phase: data}}, wire_schema(schema))
    data['decisions'][1] = _duplicate(phase, open_decision, 'F0001')
    wire = {'response': {phase: data}}
    jsonschema.validate(wire, wire_schema(schema))
    assert json.loads(decode_wire(json.dumps(wire), schema)) == {phase: data}
    data['decisions'][1]['duplicate_of'] = 'F0099'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({'response': {phase: data}}, wire_schema(schema))


@pytest.mark.parametrize('phase', ['scope_assessment', 'assessment'])
def test_review_schema_exposes_remaining_new_finding_capacity(phase):
    ids = [f'F{i:04}' for i in range(1, 121)]
    parameter = 'review_decision_ids'
    schema = response_schema(phase, **{parameter: ids})
    data = _wire_assessment(phase, 120)
    finding = {'category': 'coverage', 'issue': 'New source omission', 'sources': ['guide'], 'affected_ids': []}
    if phase == 'scope_assessment':
        finding.update(inspection_requests=[], gap_kind='missing_obligation')
    data['findings'] = [copy.deepcopy(finding) for _ in range(9)]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({phase: data}, schema)
    data['findings'].pop()
    jsonschema.validate({phase: data}, schema)
    complete = response_schema(phase, **{parameter: [f'F{i:04}' for i in range(1, 129)]})
    model = 'ScopeAssessmentProposal' if phase == 'scope_assessment' else 'CoverageAssessmentProposal'
    assert complete['$defs'][model]['properties']['findings']['maxItems'] == 0


@pytest.mark.asyncio
async def test_real_scope_phase_corrects_duplicate_once_and_retains_original_blocker():
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection
    from coordinare.services.environment_scope import _phase

    current = ledger()
    budget = AttemptBudget(scope_findings=current)
    async def fetch(path):
        return 'Pinned requirement'
    inspection = SourceInspection(['guide'], fetch, DiscoveryLimits(), budget)
    await inspection.read('guide')
    messages = [{'role': 'system', 'content': 'Independent source scope review'},
                {'role': 'user', 'content': '{}'}]
    bad = assessment()
    bad.pop('capability_unavailable')
    bad['decisions'][1]['duplicate_of'] = 'F0001'
    good = copy.deepcopy(bad)
    good['decisions'][1]['status'] = 'resolved'
    observed = []
    async def chat(history, *, response_schema):
        observed.append(copy.deepcopy(history))
        return json.dumps({'scope_assessment': bad if len(observed) == 1 else good})
    def validate(reply):
        return apply(current, {**reply['scope_assessment'], 'capability_unavailable': False})
    updated = await _phase(inspection, messages, chat=chat, envelope='scope_assessment', validate=validate)
    assert updated.open_ids() == ['F0001']
    assert current.open_ids() == ['F0001', 'F0002']
    assert budget.schema_repairs == 1 and budget.calls_completed == 2
    feedback = json.loads(observed[1][-1]['content'])['validation_errors'][0]
    assert feedback['location'] == ['decisions', '1', 'status']
    assert 'resolved' in feedback['guidance']
