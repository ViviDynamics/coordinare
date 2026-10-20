"""Typed source assessments persist with their review binding; legacy state fails closed."""
from __future__ import annotations

import contextlib
import copy

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from pydantic import ValidationError

from coordinare.services.environment_acceptance import accepted_environment
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import (
    begin_attempt,
    journal_for,
    restart_environment_attempt,
    save_attempt,
)
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_353_scope_contract import (
    COMMIT,
    SOURCES,
    Replies,
    criterion,
    proposal,
    revision,
)
from tests.unit.test_353_source_decision_flow import READ_GUIDE, accept_covered, objection

CLOSE = [READ_GUIDE, proposal(criterion()), objection(), revision(), accept_covered('F0001')]


class Interrupted(BaseException):
    pass


async def saved_flow(tmp_path, values, *, stop_phase=None):
    """Run discovery with a real journal, keeping a copy of every published checkpoint."""
    from coordinare.services.environment_scope import discover_scope

    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    snapshots = []
    async def checkpoint():
        save_attempt(cache, budget)
        snapshots.append(copy.deepcopy(cache.environment_attempt))
        if stop_phase is not None and budget.scope_phase == stop_phase:
            raise Interrupted
    budget.checkpoint = checkpoint
    async def fetch(path):
        return SOURCES.get(path)
    with contextlib.suppress(Interrupted):
        await discover_scope(paths=list(SOURCES), fetch=fetch, chat=Replies(values), commit=COMMIT,
                             limits=DiscoveryLimits(), budget=budget)
    return cache, budget, snapshots


def refused(cache):
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        journal_for(cache)
    return failure.value.code


@pytest.mark.asyncio
async def test_roundtrip_preserves_assessment_and_spent_counters(tmp_path):
    cache, budget, _ = await saved_flow(tmp_path, CLOSE[:3], stop_phase='revision')
    saved = copy.deepcopy(cache.environment_attempt)
    assert saved['version'] == 9 and saved['scope_author_protocol'] == 4
    assert saved['scope_assessment']['review_sequence'] == 1
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert resumed.scope_assessment == budget.scope_assessment
    assert (resumed.calls, resumed.reads, resumed.coverage_revisions, resumed.schema_repairs) == (
        saved['calls'], saved['reads'], saved['coverage_revisions'], saved['schema_repairs'])
    assert cache.environment_attempt['started_at'] == saved['started_at']
    assert cache.environment_attempt['expires_at'] == saved['expires_at']


@pytest.mark.asyncio
@pytest.mark.parametrize('corrupt', ['scope_digest', 'reviewer_input', 'source_commit', 'disposition'])
async def test_recovery_rejects_changed_bindings(tmp_path, corrupt):
    cache, _, _ = await saved_flow(tmp_path, CLOSE[:3], stop_phase='revision')
    record = cache.environment_attempt['scope_assessment']
    if corrupt == 'scope_digest':
        record['scope_digest'] = 'e'*64
    elif corrupt == 'reviewer_input':
        record['reviewer_inputs']['guide'] = 'f'*64
    elif corrupt == 'source_commit':
        record['source_commit'] = 'b'*40
    else:
        record['assessment']['findings'][0]['issue'] = 'A different objection'
    assert refused(cache) in {'scope_protocol_invalid', 'scope_source_identity'}


@pytest.mark.asyncio
async def test_recovery_rejects_stale_assessment_sequence(tmp_path):
    cache, _, snapshots = await saved_flow(tmp_path, CLOSE)
    first = next(s['scope_assessment'] for s in snapshots if s.get('scope_assessment', {}) and
                 s['scope_assessment']['review_sequence'] == 1)
    assert cache.environment_attempt['scope_assessment']['review_sequence'] == 2
    cache.environment_attempt['scope_assessment'] = copy.deepcopy(first)
    assert refused(cache) == 'scope_protocol_invalid'


@pytest.mark.asyncio
async def test_reviews_without_assessment_hold(tmp_path):
    cache, _, _ = await saved_flow(tmp_path, CLOSE[:3], stop_phase='revision')
    cache.environment_attempt['scope_assessment'] = None
    assert refused(cache) == 'scope_protocol_invalid'


@pytest.mark.asyncio
async def test_accepted_journal_binds_contract_to_assessment(tmp_path):
    cache, budget, _ = await saved_flow(tmp_path, CLOSE)
    journal = journal_for(cache)
    assert journal.scope_phase == 'accepted' and journal.scope_contract.current
    assert journal.scope_contract.source_assessment == journal.scope_assessment == budget.scope_assessment


@pytest.mark.asyncio
async def test_save_refuses_assessment_regression(tmp_path):
    cache, budget, snapshots = await saved_flow(tmp_path, CLOSE[:5])
    first = next(s['scope_assessment'] for s in snapshots if s.get('scope_assessment', {}) and
                 s['scope_assessment']['review_sequence'] == 1)
    from coordinare_service_inference.environment.scope import SourceAssessmentRecord
    budget.scope_assessment = SourceAssessmentRecord.model_validate(first)
    with pytest.raises(EnvironmentDiscoveryError):
        save_attempt(cache, budget)


def legacy(journal):
    data = copy.deepcopy(journal)
    data.update(version=8, scope_author_protocol=3)
    data.pop('scope_assessment', None)
    if data.get('scope_contract') is not None:
        data['scope_contract']['schema_version'] = 1
        data['scope_contract'].pop('source_assessment', None)
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['pending', 'held', 'accepted'])
async def test_v8_journal_requires_restart(tmp_path, kind):
    values = CLOSE if kind == 'accepted' else CLOSE[:3]
    cache, _, _ = await saved_flow(tmp_path, values, stop_phase=None if kind == 'accepted' else 'revision')
    data = legacy(cache.environment_attempt)
    if kind == 'held':
        data.update(status='held', last_error='attempt_coverage_limit')
    cache.environment_attempt = copy.deepcopy(data)
    assert refused(cache) == 'legacy_attempt_requires_restart'
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600)
    assert cache.environment_attempt == data


@pytest.mark.asyncio
async def test_restart_archives_v8_unchanged(tmp_path):
    cache, _, _ = await saved_flow(tmp_path, CLOSE[:3], stop_phase='revision')
    data = legacy(cache.environment_attempt)
    data.update(status='held', last_error='attempt_coverage_limit')
    cache.environment_attempt = copy.deepcopy(data)
    old_dir = cache.cache_dir
    restart_environment_attempt(cache, cleanup_confirmed=True)
    assert cache.environment_attempt_archive[-1] == data
    assert cache.environment_attempt is None and cache.cache_dir != old_dir


def test_legacy_v1_contract_manifest_is_not_ready(tmp_path):
    cache = cache_fixture(tmp_path)
    assert accepted_environment(cache) is not None
    manifest = copy.deepcopy(cache.environment_manifest)
    contract = manifest['scope_contract']
    contract['schema_version'] = 1
    contract.pop('source_assessment')
    from coordinare_service_inference.environment.scope import ScopeContract
    manifest['scope_contract_digest'] = ScopeContract.model_validate(contract).digest()
    with pytest.raises(ValidationError, match='scope_contract_refresh_required|fresh source review'):
        EnvironmentManifest.model_validate(manifest)
    cache.environment_manifest = manifest
    assert accepted_environment(cache) is None
