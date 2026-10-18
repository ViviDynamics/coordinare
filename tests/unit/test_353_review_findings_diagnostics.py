"""Operator summaries expose status, never untrusted coverage finding prose."""
from __future__ import annotations

import json

from coordinare_service_inference.environment.findings import FindingLedger

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_findings import coverage_summary
from tests.unit.test_353_review_findings_contract import DIGEST, INPUTS, ledger_data


def test_finding_diagnostics_do_not_expose_model_rationale_or_credentials():
    data = ledger_data()
    data['records'][0]['proposal']['issue'] = 'PRIVATE_REPOSITORY_TEXT'
    data['reviews'][-1]['decisions'][0]['status'] = 'open'
    data['reviews'][-1]['decisions'][0]['rationale'] = 'PRIVATE_RESOLUTION_TEXT'
    ledger = FindingLedger.model_validate(data)
    budget = AttemptBudget(finding_ledger=ledger)
    summary = budget.summary()
    assert summary['finding_count'] == 1
    assert summary['open_finding_ids'] == ['F0001']
    assert summary['finding_categories'] == {'capability': 1}
    assert 'PRIVATE_' not in json.dumps(summary)
    review = {'schema_version': 2, 'policy_version': '353-findings-v1',
        'manifest_digest': DIGEST, 'source_commit': ledger.source_commit,
        'inputs': INPUTS, 'sources': ['guide'], 'rationale': 'PRIVATE_MODEL_RATIONALE',
        'verdict': 'revise', 'finding_ledger': ledger.model_dump(), 'finding_ledger_digest': ledger.digest()}
    public = coverage_summary(review)
    assert public['open_finding_ids'] == ['F0001']
    assert 'PRIVATE_' not in json.dumps(public)
    assert 'finding_ledger' not in public and 'inputs' not in public


def test_legacy_and_malformed_operator_review_never_claim_current_acceptance():
    review = {'schema_version': 1, 'policy_version': '351-v1', 'manifest_digest': DIGEST,
        'source_commit': 'a'*40, 'sources': ['guide'], 'inputs': INPUTS,
        'verdict': 'accept', 'rationale': 'PRIVATE_MODEL_RATIONALE'}
    assert coverage_summary(review)['verdict'] == 'refresh_required'
    assert coverage_summary({'rationale': 'PRIVATE'}) is None
