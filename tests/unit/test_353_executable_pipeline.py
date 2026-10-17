"""Real failed clean execution must be repaired through a new reviewed contract."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runner import run_manifest

from coordinare.services.env_cache import verify_env_cache_clean
from coordinare.services.environment_acceptance import accepted_environment
from tests.unit.test_353_bootstrap_pipeline import (
    IMAGE,
    SOURCE,
    Proposal,
    local_clean_transport,
    publish,
    setup,
)


class PortableProposal(Proposal):
    async def __call__(self, messages):
        reply = json.loads(await super().__call__(messages))
        context = json.loads(messages[1]['content'])
        if 'manifest' in reply:
            data = reply['manifest']
            portable = 'untrusted_execution_feedback' in context
            data['install'][0]['argv'] = [sys.executable, '-c',
                "import pathlib,sys;p=pathlib.Path(sys.argv[2]);pathlib.Path(sys.argv[1]).write_text(p.read_text() if sys.argv[3]=='portable' else str(p.resolve()))",
                '{cache}/payload', '{repo}/guide', 'portable' if portable else 'nonportable']
            data['checks'][0]['argv'] = [sys.executable, '-c',
                "import pathlib,sys;text=pathlib.Path(sys.argv[1]).read_text();assert (text if sys.argv[3]=='portable' else pathlib.Path(text).read_text())==sys.argv[2]",
                '{cache}/payload', SOURCE, 'portable' if portable else 'nonportable']
        return json.dumps(reply)


@pytest.mark.asyncio
async def test_failed_clean_execution_gets_new_installation_and_fresh_independent_review(tmp_path, monkeypatch):
    service, cache, state, capability = setup(tmp_path)
    agent = PortableProposal()
    await publish(service, cache, state, capability, agent)
    assert cache.environment_attempt['status'] == 'executing'
    original = accepted_environment(cache)
    repo, scratch = tmp_path/'installer', tmp_path/'scratch'
    repo.mkdir()
    scratch.mkdir()
    (repo/'guide').write_text(SOURCE)
    assert (await run_manifest(original, cache=cache.cache_dir, repo=repo, scratch=scratch, install=True)).passed
    repo.rename(tmp_path/'disposable-installer-is-gone')
    await local_clean_transport(monkeypatch, capability, cache)
    ok, reason = await verify_env_cache_clean(state, 'suite', SimpleNamespace(), performer_mounts=(cache.cache_dir.parent,))
    assert ok is False
    assert cache.environment_verification['results'][0]['status'] == 'fail'
    old_dir = cache.cache_dir
    service.record_bootstrap_execution('suite', state, cache.environment_verification, cleanup_confirmed=True)
    service.on_bootstrap_complete('suite', False, state, error=reason)
    assert cache.environment_attempt['status'] == 'repair_pending'
    assert not cache.cache_dir_ready
    service.prepare_bootstrap_generation(cache)
    assert cache.cache_dir != old_dir and not cache.cache_dir.exists()
    capability = capability.model_copy(update={'cache_mount_path': '/devenv/'+cache.sanitised_name})
    await publish(service, cache, state, capability, agent)
    repaired = accepted_environment(cache)
    assert repaired.digest() != original.digest()
    assert agent.review_starts == 2
    assert cache.environment_attempt['repairs'] == 1
    repo.mkdir()
    (repo/'guide').write_text(SOURCE)
    assert (await run_manifest(repaired, cache=cache.cache_dir, repo=repo, scratch=scratch, install=True)).passed
    await local_clean_transport(monkeypatch, capability, cache)
    ok, reason = await verify_env_cache_clean(state, 'suite', SimpleNamespace(), performer_mounts=(cache.cache_dir.parent,))
    assert ok is True, reason
    frozen, reason = await service.freeze_provenance('suite', state)
    assert frozen, reason
    service.on_bootstrap_complete('suite', True, state)
    assert cache.cache_dir_ready
    assert cache.environment_attempt['status'] == 'ready'
    assert cache.environment_attempt['rejected'][0]['manifest_digest'] == original.digest()
    assert cache.environment_attempt['rejected'][0]['report']['results'][0]['status'] == 'fail'


@pytest.mark.asyncio
async def test_failed_durable_checkpoint_stops_before_model_or_project_execution(tmp_path):
    service, cache, state, capability = setup(tmp_path)
    calls = []
    async def refusing_snapshot():
        raise OSError('state store unavailable')
    async def model(messages):
        calls.append(messages)
        return '{}'
    state['snapshot_save_fn'] = refusing_snapshot
    service.bind_kubernetes_state(state)
    with pytest.raises((OSError, RuntimeError)):
        await publish(service, cache, state, capability, model)
    assert calls == []
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
async def test_reservations_are_durable_before_model_and_source_requests(tmp_path):
    service, cache, state, capability = setup(tmp_path)
    saves = []
    async def save():
        saves.append(dict(cache.environment_attempt))
    state['snapshot_save_fn'] = save
    service.bind_kubernetes_state(state)
    original = Proposal()
    async def model(messages):
        assert saves and saves[-1]['calls'] > 0
        return await original(messages)
    await publish(service, cache, state, capability, model)
    assert saves[-1]['status'] == 'executing'
    assert saves[-1]['calls'] == 4
    assert saves[-1]['reads'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(('terminal', 'confirmed'), [('ok', True), ('error', True), ('ok', False), ('error', False)])
async def test_daemon_only_repairs_failed_execution_after_confirmed_quiescence(tmp_path, monkeypatch, terminal, confirmed):
    from unittest.mock import AsyncMock

    from coordinare.daemon import CoordinareDaemon
    from tests.unit.env_manifest_fixture import verification_output

    service, cache, state, capability = setup(tmp_path)
    await publish(service, cache, state, capability, Proposal())
    report = json.loads(verification_output(cache.environment_manifest))
    report['platform'] = capability.platform
    report['image_ref'] = IMAGE
    report['results'][0].update(status='fail', exit_code=2, output='required artifact unavailable')
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    monkeypatch.setattr('coordinare.daemon._bootstrap_failure_logs', AsyncMock(return_value=[]))
    daemon._state = state
    monkeypatch.setattr(daemon, '_quiesce_bootstrap_container', AsyncMock(return_value=confirmed))
    async def verify(*args, **kwargs):
        cache.environment_verification = report
        return False, 'required artifact unavailable', False
    monkeypatch.setattr(daemon, '_verify_bootstrap_in_clean_context', verify)
    await daemon._handle_bootstrap_terminal_status(SimpleNamespace(), 'job', 'suite', service, 'owned-container',
        {'status': terminal, 'report': {'environment_verification': report}}, [])
    assert cache.environment_attempt['status'] == ('repair_pending' if confirmed else 'held')
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
async def test_clean_verifier_deadline_cancels_before_readiness(tmp_path, monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock

    from coordinare.daemon import CoordinareDaemon

    service, cache, state, capability = setup(tmp_path)
    await publish(service, cache, state, capability, Proposal())
    cache.environment_attempt['expires_at'] = time.time() + 0.02
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state = state
    stopped = asyncio.Event()
    async def blocked(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(daemon, '_verify_bootstrap_context', blocked)
    ok, reason, infra = await daemon._verify_bootstrap_in_clean_context('suite', object(), 'job')
    assert ok is False and infra is True
    assert stopped.is_set()
    assert 'deadline' in reason
    assert not cache.cache_dir_ready
