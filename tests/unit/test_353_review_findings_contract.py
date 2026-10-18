"""A forgotten or stale coverage objection must never establish readiness."""
from __future__ import annotations

import copy
import json

import pytest
from coordinare_service_inference.environment import findings
from coordinare_service_inference.environment.models import CoverageAssessment
from pydantic import ValidationError

COMMIT = 'a' * 40
DIGEST = 'b' * 64
INPUTS = {'guide': 'c' * 64}


def ledger_data():
    return {'schema_version': 1, 'source_commit': COMMIT, 'records': [{
        'id': 'F0001', 'proposal': {'category': 'capability', 'issue': 'Required service absent',
        'sources': ['guide'], 'affected_ids': []}, 'manifest_digest': DIGEST,
        'source_commit': COMMIT, 'inputs': INPUTS}], 'reviews': [
        {'sequence': 1, 'manifest_digest': DIGEST, 'inputs': INPUTS,
         'decisions': [], 'added_ids': ['F0001']},
        {'sequence': 2, 'manifest_digest': DIGEST, 'inputs': INPUTS, 'added_ids': [],
         'decisions': [{'finding_id': 'F0001', 'status': 'resolved', 'rationale': 'Source establishes no service need',
                        'sources': ['guide'], 'duplicate_of': None}]}]}


def validate(data):
    ledger = findings.FindingLedger.model_validate(data)
    findings.validate_finding_ledger(ledger, manifest_digest=DIGEST, source_commit=COMMIT, inputs=INPUTS)
    return ledger


def test_review_ledger_requires_current_complete_closure():
    ledger = validate(ledger_data())
    assert validate(json.loads(ledger.model_dump_json())).digest() == ledger.digest()
    for change in ['missing', 'open', 'candidate', 'source', 'hash']:
        data = ledger_data()
        if change == 'missing':
            data['reviews'][-1]['decisions'] = []
        elif change == 'open':
            data['reviews'][-1]['decisions'][0]['status'] = 'open'
        elif change == 'candidate':
            data['reviews'][-1]['manifest_digest'] = 'd' * 64
        elif change == 'source':
            data['source_commit'] = 'd' * 40
        else:
            data['reviews'][-1]['inputs'] = {'guide': 'd' * 64}
        with pytest.raises(ValueError):
            validate(data)


@pytest.mark.parametrize('change', ['unknown', 'duplicate', 'reordered', 'origin', 'path', 'secret'])
def test_finding_identity_and_duplicate_history_cannot_be_forged(change):
    data = copy.deepcopy(ledger_data())
    if change == 'unknown':
        data['reviews'][-1]['decisions'][0]['finding_id'] = 'F0002'
    elif change == 'duplicate':
        data['reviews'][-1]['decisions'] *= 2
    elif change == 'reordered':
        data['records'][0]['id'] = 'F0002'
    elif change == 'origin':
        data['records'][0]['manifest_digest'] = 'd' * 64
    elif change == 'path':
        data['records'][0]['proposal']['sources'] = ['../outside']
    else:
        data['records'][0]['proposal']['issue'] = 'https://user:secret@example.org/tool'
    with pytest.raises(ValueError):
        validate(data)


def test_finding_field_limits_count_utf8_bytes():
    data = ledger_data()
    data['records'][0]['proposal']['issue'] = 'é' * 1024
    validate(data)
    data['records'][0]['proposal']['issue'] += 'x'
    with pytest.raises(ValueError):
        validate(data)


def test_finding_count_and_serialized_bounds():
    data = ledger_data()
    record = data['records'][0]
    data['records'] = [dict(record, id=f'F{i:04}') for i in range(1, 33)]
    data['reviews'] = [{'sequence': 1, 'manifest_digest': DIGEST, 'inputs': INPUTS,
                       'decisions': [], 'added_ids': [r['id'] for r in data['records']]}]
    findings.FindingLedger.model_validate(data)
    data['records'].append(dict(record, id='F0033'))
    with pytest.raises(ValueError):
        findings.FindingLedger.model_validate(data)
    data = {'schema_version': 1, 'source_commit': COMMIT, 'records': [], 'reviews': [
        {'sequence': 1, 'manifest_digest': DIGEST, 'inputs': {'p': 'c'*64}, 'decisions': [], 'added_ids': []}]}
    size = len(json.dumps(data, sort_keys=True, separators=(',', ':')).encode())
    data['reviews'][0]['inputs'] = {'p' * (131072-size+1): 'c'*64}
    findings.FindingLedger.model_validate(data)
    data['reviews'][0]['inputs'] = {'p' * (131072-size+2): 'c'*64}
    with pytest.raises(ValueError):
        findings.FindingLedger.model_validate(data)


def test_duplicate_resolution_cannot_hide_an_open_survivor_or_cycle():
    data = ledger_data()
    data['records'].append(dict(data['records'][0], id='F0002'))
    data['reviews'][0]['added_ids'].append('F0002')
    first = data['reviews'][-1]['decisions'][0]
    data['reviews'][-1]['decisions'].append(dict(first, finding_id='F0002', duplicate_of='F0001'))
    validate(data)
    first['status'] = 'open'
    with pytest.raises(ValueError):
        validate(data)

    first.update(status='resolved', duplicate_of='F0002')
    with pytest.raises(ValueError):
        validate(data)


def test_duplicate_chain_to_unknown_finding_is_a_validation_error():
    data = ledger_data()
    data['records'].append(dict(data['records'][0], id='F0002'))
    data['reviews'][0]['added_ids'].append('F0002')
    first = data['reviews'][-1]['decisions'][0]
    first['duplicate_of'] = 'F0002'
    data['reviews'][-1]['decisions'].append(dict(first, finding_id='F0002', duplicate_of='F0099'))
    with pytest.raises(ValueError):
        validate(data)


def test_current_assessment_binds_ledger_and_historical_contract_stays_readable():
    ledger = validate(ledger_data())
    data = {'schema_version': 2, 'policy_version': '353-findings-v1', 'manifest_digest': DIGEST,
            'source_commit': COMMIT, 'inputs': INPUTS, 'sources': ['guide'], 'verdict': 'accept',
            'rationale': 'Independent review', 'finding_ledger': ledger.model_dump(),
            'finding_ledger_digest': ledger.digest()}
    CoverageAssessment.model_validate(data)
    data['finding_ledger']['reviews'][-1]['decisions'][0]['rationale'] = 'Changed'
    with pytest.raises(ValidationError):
        CoverageAssessment.model_validate(data)
    data.pop('finding_ledger')
    data.pop('finding_ledger_digest')
    data.update(schema_version=1, policy_version='351-v1')
    CoverageAssessment.model_validate(data)
