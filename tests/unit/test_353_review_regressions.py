"""Independent review regressions preserve scope, cleanup and explicit restarts."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.services.env_bootstrap_control import reset_env_bootstrap_breaker
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import begin_attempt, dispatch_refusal, save_attempt
from tests.unit.test_353_discovery_repair import PATH, Replies, assessment, candidate, run
from tests.unit.test_353_execution_repair import cache_fixture


@pytest.mark.asyncio
async def test_cancelled_dispatch_holds_cleanup_even_after_source_change(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.calls = 4
    save_attempt(cache, budget, executing=True)
    entered = asyncio.Event()
    async def dispatch(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()
    daemon = CoordinareDaemon.__new__(CoordinareDaemon)
    daemon._state = {'env_cache': {'suite':cache}}
    task = asyncio.create_task(daemon._dispatch_bootstrap_with_deadline(SimpleNamespace(dispatch_card=dispatch),'suite',{},{}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cache.environment_attempt['last_error'] == 'cleanup_unconfirmed'
    assert cache.environment_attempt['status'] == 'held'
    cache.bootstrap_commit_sha = 'b'*40
    assert dispatch_refusal(cache, cache.bootstrap_commit_sha) is not None
    with pytest.raises(EnvironmentDiscoveryError):
        begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    assert cache.environment_attempt['calls'] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['scope', 'optional', 'lifecycle'])
async def test_invalid_activation_correction_cannot_weaken_valid_obligations(change):
    malformed = candidate()
    malformed['activation']['path'] = 'wrong-type'
    malformed['checks'][0]['scope'] = ['implementer','qa']
    if change == 'lifecycle':
        malformed['services'] = [{'id':'svc', 'start':{'id':'start','argv':['true'],'scope':['qa']},
            'health':{'id':'health','argv':['true']},'stop':{'id':'stop','argv':['true']}}]
    revised = copy.deepcopy(malformed)
    revised['activation']['path'] = []
    if change == 'scope':
        revised['checks'][0]['scope'] = ['qa']
    elif change == 'optional':
        revised['checks'][0]['required'] = False
    else:
        revised['services'][0]['start']['scope'] = []
    replies = Replies([{'tool':'read_file','path':PATH},{'manifest':malformed},{'manifest':revised},assessment(revised)])
    with pytest.raises(EnvironmentDiscoveryError, match='obligation'):
        await run(replies)


@pytest.mark.parametrize('legacy', [False, True])
def test_explicit_operator_reset_archives_discovery_hold_and_starts_fresh(tmp_path, legacy):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    if legacy:
        removed = {'schema_repairs','coverage_revisions','execution_repairs',
            'max_schema_repairs','max_coverage_revisions','max_execution_repairs'}
        cache.environment_attempt = {k:v for k,v in cache.environment_attempt.items() if k not in removed}
        cache.environment_attempt.update(version=1,repairs=1,max_repairs=2)
    cache.environment_attempt.update(status='held',last_error='discovery_refused')
    before = copy.deepcopy(cache.environment_attempt)
    old_path = cache.cache_dir
    reset_env_bootstrap_breaker({},'suite',cache,'test',SimpleNamespace(record=lambda **kwargs:None))
    assert cache.environment_attempt is None
    assert cache.environment_attempt_archive[-1] == before
    assert cache.cache_dir != old_path and not cache.cache_dir.exists()
    assert dispatch_refusal(cache,cache.bootstrap_commit_sha) is None
    budget = begin_attempt(cache,DiscoveryLimits(),outer_seconds=600)
    assert budget.repairs == 0
    assert cache.environment_attempt['version'] == 4


def test_operator_reset_never_clears_uncertain_execution_or_consumes_reset_cooldown(tmp_path):
    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    save_attempt(cache,budget,executing=True)
    before = copy.deepcopy(cache.environment_attempt)
    state = {}
    with pytest.raises(EnvironmentDiscoveryError):
        reset_env_bootstrap_breaker(state,'suite',cache,'test',SimpleNamespace(record=lambda **kwargs:None))
    assert cache.environment_attempt == before
    assert not state.get('env_bootstrap_manual_resets')


@pytest.mark.asyncio
async def test_preparation_window_exception_preserves_cleanup_hold(tmp_path):
    from tests.unit.test_353_bootstrap_pipeline import setup
    service, cache, _state, capability = setup(tmp_path)
    cache.environment_capabilities = capability.model_dump(mode="json")
    budget = begin_attempt(cache,DiscoveryLimits(),outer_seconds=600)
    save_attempt(cache,budget,executing=True)
    with pytest.raises(OSError):
        async with service._bootstrap_execution_window(cache):
            raise OSError('external preparation failed')
    assert cache.environment_attempt['last_error'] == 'cleanup_unconfirmed'
    cache.bootstrap_commit_sha = 'b'*40
    assert dispatch_refusal(cache,cache.bootstrap_commit_sha) is not None


def test_restart_archive_survives_snapshot_roundtrip(tmp_path):
    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
    cache = cache_fixture(tmp_path)
    begin_attempt(cache,DiscoveryLimits(),outer_seconds=600)
    cache.environment_attempt['status'] = 'held'
    reset_env_bootstrap_breaker({},'suite',cache,'test',SimpleNamespace(record=lambda **kwargs:None))
    snapshots = _persist_env_cache({'suite':cache})
    restored = cache_fixture(tmp_path)
    _rehydrate_env_cache({'env_cache':{'suite':restored}},SimpleNamespace(env_cache=snapshots))
    assert restored.environment_attempt_archive == cache.environment_attempt_archive
    assert len(restored.environment_attempt_archive) == 1


def test_operator_restart_refuses_malformed_journal_without_mutating_state(tmp_path):
    cache = cache_fixture(tmp_path)
    begin_attempt(cache,DiscoveryLimits(),outer_seconds=600)
    cache.environment_attempt['status'] = 'unsupported-state'
    before = copy.deepcopy(cache.environment_attempt)
    with pytest.raises(EnvironmentDiscoveryError):
        reset_env_bootstrap_breaker({},'suite',cache,'test',SimpleNamespace(record=lambda **kwargs:None))
    assert cache.environment_attempt == before


@pytest.mark.parametrize('confirmed', [False, True])
def test_operator_endpoint_requires_owned_runtime_cleanup_before_restart(tmp_path, confirmed):
    from unittest.mock import AsyncMock

    from tests.unit.test_270_bootstrap_control import DashboardStore, _client, _trigger_daemon
    daemon = _trigger_daemon()
    cache = cache_fixture(tmp_path)
    cache.symphony_name = 'alpha'
    daemon.state['env_cache'] = {'alpha':cache}
    budget = begin_attempt(cache,DiscoveryLimits(),outer_seconds=600)
    save_attempt(cache,budget,executing=True)
    stop = AsyncMock(return_value=confirmed)
    daemon.state['bootstrap_runtimes'] = {'alpha':SimpleNamespace(
        service=SimpleNamespace(stop_session_confirmed=stop),job_id='owned-session')}
    response = _client(daemon,DashboardStore()).post('/api/symphonies/alpha/env-bootstrap')
    assert response.status_code == (202 if confirmed else 409)
    stop.assert_awaited_once_with('owned-session')
    if confirmed:
        assert cache.environment_attempt is None
        assert len(cache.environment_attempt_archive) == 1
    else:
        assert cache.environment_attempt['status'] == 'executing'
        assert not daemon.state.get('env_bootstrap_manual_resets')
