"""A runtime receipt cannot replace clean execution or waive installer checks."""
from __future__ import annotations

import copy
import json

import pytest

from coordinare.services.environment_acceptance import execution_report
from tests.unit.runtime_manifest_fixture import runtime_cache, runtime_report


@pytest.mark.parametrize('problem', ['runtime_digest', 'platform', 'phase', 'cleanup', 'receipt', 'source', 'saved_receipt', 'capability_drift'])
def test_clean_runtime_rejects_missing_or_wrong_bound_evidence(tmp_path, problem):
    cache = runtime_cache(tmp_path)
    raw = runtime_report(cache)
    cache.environment_runtime_artifacts = copy.deepcopy(raw['runtime_artifacts'])
    if problem == 'runtime_digest':
        raw['runtime_digest'] = 'e'*64
    elif problem == 'platform':
        raw['platform'] = 'linux/x86_64'
    elif problem == 'phase':
        raw['runtime_phase'] = 'installer'
    elif problem == 'cleanup':
        raw['runtime_cleanup'] = False
    elif problem == 'receipt':
        raw['runtime_artifacts'] = None
    elif problem == 'source':
        raw['runtime_artifacts']['source_digest'] = 'e'*64
    elif problem == 'capability_drift':
        cache.environment_capabilities['runtime']['identity']['emulator_sha256'] = 'e'*64
    else:
        cache.environment_runtime_artifacts = None
    with pytest.raises(ValueError, match='runtime'):
        execution_report(json.dumps(raw), cache)
    assert cache.environment_verification is None


def test_clean_runtime_requires_the_exact_installer_receipt(tmp_path):
    cache = runtime_cache(tmp_path)
    raw = runtime_report(cache)
    cache.environment_runtime_artifacts = copy.deepcopy(raw['runtime_artifacts'])
    raw['runtime_artifacts']['artifacts'][0]['sha256'] = 'e'*64
    with pytest.raises(ValueError, match='runtime'):
        execution_report(json.dumps(raw), cache)


def test_valid_clean_runtime_is_published_with_original_receipt(tmp_path):
    cache = runtime_cache(tmp_path)
    raw = runtime_report(cache)
    cache.environment_runtime_artifacts = raw['runtime_artifacts']
    assert execution_report(json.dumps(raw), cache).passed
    assert cache.environment_verification['runtime_phase'] == 'consumer'


@pytest.mark.parametrize('problem', [None, 'cleanup', 'missing_install', 'failed_install', 'unrun_install', 'stale_receipt'])
def test_only_quiesced_successful_installer_publishes_runtime_artifacts(tmp_path, problem):
    from coordinare.services.environment_runtime import admit_installer_runtime

    cache = runtime_cache(tmp_path)
    raw = runtime_report(cache, phase='installer')
    if problem == 'missing_install':
        raw['results'] = [item for item in raw['results'] if item['id'] != 'install']
    elif problem in ('failed_install', 'unrun_install'):
        raw['results'][0].update(status='fail' if problem == 'failed_install' else 'not_run', exit_code=1)
    elif problem == 'stale_receipt':
        cache.environment_runtime_artifacts = copy.deepcopy(raw['runtime_artifacts'])
        cache.environment_runtime_artifacts['artifacts'][0]['sha256'] = 'e'*64
    original = copy.deepcopy(cache.environment_runtime_artifacts)
    if problem is None:
        admit_installer_runtime(cache, raw, cleanup_confirmed=True)
        assert cache.environment_runtime_artifacts == raw['runtime_artifacts']
    else:
        with pytest.raises(ValueError):
            admit_installer_runtime(cache, raw, cleanup_confirmed=problem != 'cleanup')
        assert cache.environment_runtime_artifacts == original
    assert cache.environment_verification is None


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', [None, 'unconfirmed_stop', 'missing_receipt'])
async def test_daemon_admits_installer_receipt_before_starting_clean_verifier(tmp_path, monkeypatch, problem):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.daemon import CoordinareDaemon

    cache = runtime_cache(tmp_path)
    raw = runtime_report(cache, phase='installer')
    if problem == 'missing_receipt':
        raw['runtime_artifacts'] = None
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state = {'env_cache': {'sym': cache}}
    stopped = []
    async def stop(*args):
        assert cache.environment_runtime_artifacts is None
        stopped.append(True)
        return problem != 'unconfirmed_stop'
    async def verify(*args, **kwargs):
        assert stopped
        assert cache.environment_runtime_artifacts == raw['runtime_artifacts']
        assert cache.environment_verification is None
        return True, None, False
    monkeypatch.setattr(daemon, '_quiesce_bootstrap_container', stop)
    verifier = AsyncMock(side_effect=verify)
    monkeypatch.setattr(daemon, '_verify_bootstrap_in_clean_context', verifier)
    service = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))
    await daemon._handle_bootstrap_terminal_status(SimpleNamespace(), 'job', 'sym', service, 'owned',
        {'status': 'ok', 'report': {'environment_verification': raw}}, [])
    if problem is None:
        verifier.assert_awaited_once()
        assert service.on_bootstrap_complete.call_args.args[1] is True
    else:
        verifier.assert_not_awaited()
        assert cache.environment_runtime_artifacts is None
        assert service.on_bootstrap_complete.call_args.args[1] is False
