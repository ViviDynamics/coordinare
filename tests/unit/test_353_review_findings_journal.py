"""Coverage findings cannot vanish on repair/restart or legacy migration."""
from __future__ import annotations

import copy
import time

import pytest
from coordinare_service_inference.environment.findings import FindingLedger

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    restart_environment_attempt,
    save_attempt,
)
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_review_findings_contract import ledger_data


class ReviewCheckpointCrash(BaseException):
    """Simulate process death after the durable checkpoint returned successfully."""


async def saved_review(tmp_path, *, maximum=2, manifest=None):
    from tests.unit.test_353_discovery_repair import PATH, Replies, candidate, run
    from tests.unit.test_353_review_findings_flow import finding, review
    cache = cache_fixture(tmp_path)
    now = time.time()
    budget = begin_attempt(cache, DiscoveryLimits(max_coverage_revisions=maximum), outer_seconds=3600, now=now)
    async def checkpoint():
        save_attempt(cache, budget)
        if budget.finding_ledger and budget.finding_ledger.reviews:
            raise ReviewCheckpointCrash()
    budget.checkpoint = checkpoint
    with pytest.raises(ReviewCheckpointCrash):
        await run(Replies([{'tool': 'read_file', 'path': PATH}, {'manifest': manifest or candidate()},
                          review(verdict='revise', findings=[finding()])]), budget=budget)
    return cache, now


@pytest.mark.asyncio
async def test_review_and_revision_charge_share_the_same_durable_checkpoint(tmp_path):
    cache, now = await saved_review(tmp_path)
    assert len(cache.environment_attempt['finding_ledger']['reviews']) == 1
    assert cache.environment_attempt['coverage_revisions'] == 1
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now+1)
    assert resumed.coverage_revisions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['rename', 'scope', 'optional'])
async def test_resumed_review_cannot_weaken_established_required_checks(tmp_path, mutation):
    from tests.unit.test_353_discovery_repair import Replies, candidate, run
    from tests.unit.test_353_review_findings_flow import decision, response, review
    cache, now = await saved_review(tmp_path)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now+1)
    changed = candidate()
    if mutation == 'rename':
        changed['checks'][0]['id'] = 'replacement'
        changed['requirements'][0]['checks'] = ['replacement']
    elif mutation == 'scope':
        changed['checks'][0]['scope'] = ['qa']
    else:
        changed['checks'][0]['required'] = False
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(Replies([{'manifest': changed, 'finding_responses': [response()]},
                          review(decisions=[decision()])]), budget=resumed)
    assert exc.value.code == 'obligation_removed'


@pytest.mark.asyncio
async def test_exhausted_revision_checkpoint_is_held_without_new_allowance(tmp_path):
    cache, now = await saved_review(tmp_path, maximum=0)
    assert cache.environment_attempt['coverage_revisions'] == 0
    assert cache.environment_attempt['status'] == 'held'
    assert cache.environment_attempt['last_error'] == 'attempt_coverage_limit'
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now+1)


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['remove', 'health_id', 'scope'])
async def test_resumed_review_preserves_required_service_lifecycle(tmp_path, mutation):
    from tests.unit.test_353_discovery_repair import Replies, candidate, run
    from tests.unit.test_353_review_findings_flow import response
    manifest = candidate()
    manifest['services'] = [{'id': 'backend', 'required': True,
        'start': {'id': 'service-start', 'argv': ['true']},
        'health': {'id': 'service-health', 'argv': ['true']},
        'stop': {'id': 'service-stop', 'argv': ['true']}}]
    cache, now = await saved_review(tmp_path, manifest=manifest)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=now+1)
    changed = copy.deepcopy(manifest)
    if mutation == 'remove':
        changed['services'] = []
    elif mutation == 'health_id':
        changed['services'][0]['health']['id'] = 'replacement'
    else:
        for phase in ('start', 'health', 'stop'):
            changed['services'][0][phase]['scope'] = ['qa']
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(Replies([{'manifest': changed, 'finding_responses': [response()]}]), budget=resumed)
    assert exc.value.code == 'obligation_removed'


def test_restart_preserves_findings_expiry_and_consumed_budgets(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    data = ledger_data()
    data['source_commit'] = cache.bootstrap_commit_sha
    data['records'][0]['source_commit'] = cache.bootstrap_commit_sha
    ledger = FindingLedger.model_validate(data)
    budget.finding_ledger = ledger
    from coordinare_service_inference.environment.models import EnvironmentManifest
    budget.coverage_manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
    budget.calls, budget.reads = 9, 20
    budget.schema_repairs, budget.coverage_revisions = 1, 2
    save_attempt(cache, budget)
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=200)
    assert resumed.finding_ledger.digest() == ledger.digest()
    assert (resumed.calls, resumed.reads, resumed.schema_repairs, resumed.coverage_revisions) == (9, 20, 1, 2)
    assert cache.environment_attempt['expires_at'] == 700
    assert cache.environment_attempt['version'] == 9
    resumed.finding_ledger.records[0].proposal.issue = 'Mutation'
    assert cache.environment_attempt['finding_ledger']['records'][0]['proposal']['issue'] != 'Mutation'


@pytest.mark.parametrize('version', [1, 2, 3, 4])
def test_legacy_attempt_requires_explicit_cleanup_restart(tmp_path, version):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    data = cache.environment_attempt
    data.pop('finding_ledger', None)
    data['version'] = version
    if version == 1:
        data['repairs'], data['max_repairs'] = 1, 2
        for name in ('schema_repairs', 'coverage_revisions', 'execution_repairs'):
            data.pop(name)
            data.pop('max_' + name)
    previous = copy.deepcopy(data)
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=200)
    assert exc.value.code == 'legacy_attempt_requires_restart'
    restart_environment_attempt(cache, cleanup_confirmed=True)
    assert cache.environment_attempt is None
    assert cache.environment_attempt_archive[-1] == previous


def test_ledger_cannot_be_removed_or_rewritten_at_checkpoint(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    data = ledger_data()
    data['source_commit'] = cache.bootstrap_commit_sha
    data['records'][0]['source_commit'] = cache.bootstrap_commit_sha
    budget.finding_ledger = FindingLedger.model_validate(data)
    from coordinare_service_inference.environment.models import EnvironmentManifest
    budget.coverage_manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
    save_attempt(cache, budget)
    budget.finding_ledger = None
    with pytest.raises(EnvironmentDiscoveryError):
        save_attempt(cache, budget)
