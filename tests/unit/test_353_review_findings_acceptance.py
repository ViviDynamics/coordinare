"""Historical or incomplete finding evidence cannot admit execution or reuse."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.findings import FindingLedger, FindingReview
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_acceptance import accepted_environment, execution_report
from tests.unit.env_manifest_fixture import verification_output
from tests.unit.test_353_execution_repair import cache_fixture


def current_review(cache):
    manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
    ledger = FindingLedger(source_commit=manifest.source_commit, reviews=[FindingReview(sequence=1,
        manifest_digest=manifest.digest(), inputs=dict(manifest.inputs), decisions=[], added_ids=[])])
    cache.environment_assessment.update(schema_version=2, policy_version='353-findings-v1',
        finding_ledger=ledger.model_dump(), finding_ledger_digest=ledger.digest())
    return cache


def test_old_ready_contract_cannot_bypass_findings_policy(tmp_path):
    cache = cache_fixture(tmp_path)
    cache.cache_dir_ready = True
    cache.environment_assessment.update(schema_version=1, policy_version='351-v1')
    cache.environment_assessment.pop('finding_ledger', None)
    cache.environment_assessment.pop('finding_ledger_digest', None)
    assert accepted_environment(cache) is None


def test_current_finding_review_accepts_exact_manifest_only(tmp_path):
    cache = current_review(cache_fixture(tmp_path))
    assert accepted_environment(cache) is not None
    cache.environment_assessment['finding_ledger']['reviews'][0]['manifest_digest'] = 'd'*64
    assert accepted_environment(cache) is None


@pytest.mark.parametrize('status', ['fail', 'not_run', 'indeterminate', 'missing'])
def test_accepted_findings_never_waive_failed_or_unrun_checks(tmp_path, status):
    cache = current_review(cache_fixture(tmp_path))
    report = json.loads(verification_output(cache.environment_manifest))
    if status == 'missing':
        report['results'] = []
    else:
        report['results'][0].update(status=status, exit_code=1 if status == 'fail' else None)
    if status == 'missing':
        with pytest.raises(ValueError):
            execution_report(json.dumps(report), cache)
    else:
        assert not execution_report(json.dumps(report), cache).passed
