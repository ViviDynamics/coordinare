"""Real failed clean execution must be repaired through a new reviewed contract."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runner import run_manifest

from coordinare.services.env_cache import verify_env_cache_clean
from coordinare.services.environment_acceptance import accepted_environment
from tests.unit.scope_chat_fixture import ScopeChatFixture
from tests.unit.test_353_bootstrap_pipeline import (
    IMAGE,
    SOURCE,
    Proposal,
    local_clean_transport,
    probe_transport,
    publish,
    setup,
)


@pytest.fixture(autouse=True)
def isolated_probe_io(monkeypatch):
    monkeypatch.setattr('coordinare.services.environment_probe_service.execute_environment_probe', probe_transport)


class PortableProposal(Proposal):
    def __init__(self):
        super().__init__()
        self.review_history = []

    async def __call__(self, messages):
        reply = json.loads(await super().__call__(messages))
        context = json.loads(messages[1]['content'])
        if 'manifest' in context and len(messages) == 2:
            self.review_history.append(context.get('untrusted_execution_history', []))
        if 'manifest' in reply:
            data = reply['manifest']
            portable = 'untrusted_execution_feedback' in context
            data['install'][0]['argv'] = [sys.executable, '-c',
                "import pathlib,sys;p=pathlib.Path(sys.argv[2]);out=pathlib.Path(sys.argv[1]);target=out.with_name('cached-guide') if sys.argv[3]=='portable' else p;target.write_text(p.read_text());out.write_text(str(target.resolve()))",
                '{cache}/payload', '{repo}/guide', 'portable' if portable else 'nonportable']
            data['checks'][0]['argv'] = [sys.executable, '-c',
                "import pathlib,sys;text=pathlib.Path(sys.argv[1]).read_text();assert pathlib.Path(text).read_text()==sys.argv[2]",
                '{cache}/payload', SOURCE]
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
    assert repaired.checks == original.checks
    assert agent.review_starts == 2
    assert agent.review_history[0] == []
    historical = agent.review_history[1][0]
    assert historical['manifest_digest'] == original.digest()
    assert historical['manifest'] == original.model_dump(mode='json')
    assert historical['results'][0]['status'] == 'fail'
    assert 'output' not in historical['results'][0]
    assert cache.environment_attempt['execution_repairs'] == 1
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
    assert saves[-1]['calls'] == 8
    assert saves[-1]['reads'] == 7
    assert len(saves[-1]['probe_reports']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('terminal', 'confirmed'), [('ok', True), ('error', True), ('ok', False), ('error', False)])
async def test_daemon_only_repairs_failed_execution_after_confirmed_quiescence(tmp_path, monkeypatch, terminal, confirmed):
    from unittest.mock import AsyncMock

    from coordinare.daemon import CoordinareDaemon
    from tests.unit.env_manifest_fixture import verification_output

    service, cache, state, capability = setup(tmp_path)
    await publish(service, cache, state, capability, Proposal())
    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
    report = json.loads(verification_output(cache.environment_manifest,
        execution_source=ExecutionSourceIdentity.model_validate(cache.environment_execution_source)))
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


@pytest.mark.asyncio
async def test_real_daemon_checkpoint_propagates_disk_failure(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from coordinare.daemon import CoordinareDaemon

    service, cache, state, capability = setup(tmp_path)
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state = state
    daemon._state_store = SimpleNamespace(save=AsyncMock(side_effect=OSError('disk failure')))
    monkeypatch.setattr(daemon, '_build_snapshot', Mock(return_value=object()))
    state['snapshot_save_fn'] = daemon._flush_snapshot
    # Same strict callback production wires when it exists; fallback reproduces old behavior.
    state['snapshot_checkpoint_fn'] = getattr(daemon, '_checkpoint_snapshot', daemon._flush_snapshot)
    service.bind_kubernetes_state(state)
    model = Proposal()
    with pytest.raises(RuntimeError, match='persisted'):
        await publish(service, cache, state, capability, model)
    # Source ownership is persisted before model-call reservation or source I/O.
    assert cache.environment_attempt['calls'] == 0
    assert cache.environment_source_owner is None
    assert model.review_starts == 0
    assert cache.environment_manifest is None


@pytest.mark.asyncio
async def test_dispatch_deadline_cancels_before_poller_starts(tmp_path):
    import asyncio
    import time
    from unittest.mock import AsyncMock

    from coordinare.daemon import CoordinareDaemon

    service, cache, state, capability = setup(tmp_path)
    await publish(service, cache, state, capability, Proposal())
    cache.environment_attempt['expires_at'] = time.time()+0.02
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state = state
    cancelled = asyncio.Event()
    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    task = asyncio.create_task(daemon._finish_bootstrap_dispatch(
        SimpleNamespace(dispatch_card=blocked), 'bootstrap', 'suite', {}, {}, service))
    try:
        await asyncio.sleep(0.08)
        assert task.done(), 'expired dispatch kept running without a poller'
        assert cancelled.is_set()
        assert not daemon._bootstrap_poll_tasks
        assert not cache.cache_dir_ready
        assert cache.environment_attempt['status'] == 'held'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['start', 'ready', 'post'])
async def test_cancelled_http_dispatch_drains_creation_then_confirms_owned_cleanup(monkeypatch, phase):
    import asyncio
    from unittest.mock import AsyncMock

    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.performer_runtime import StartedPerformer
    from tests.unit.services.test_http_performer_service import _card, _ephemeral_config, _workspace

    entered, release = asyncio.Event(), asyncio.Event()
    events = []
    async def barrier(at):
        if phase == at:
            entered.set()
            await release.wait()
    async def start(*args, **kwargs):
        events.append('creating')
        await barrier('start')
        events.append('created')
        return StartedPerformer(handle='owned', endpoint='http://localhost:1')
    async def ready(*args, **kwargs):
        await barrier('ready')
        return
    async def post(*args, **kwargs):
        await barrier('post')
        return SimpleNamespace(accepted=True, job_id='job')
    runtime = SimpleNamespace(start_ephemeral=start)
    client = AsyncMock()
    client.post_job.side_effect = post
    service = HTTPPerformerService(_ephemeral_config(), client=client, runtime=runtime)
    async def stopped(session_id):
        assert 'created' in events, 'absence checked before pending create settled'
        events.append('confirmed')
        service._active_jobs.pop(session_id, None)
        return True
    monkeypatch.setattr(service, 'stop_session_confirmed', stopped)
    monkeypatch.setattr('coordinare.services.performer_lifecycle.wait_ready', ready)
    task = asyncio.create_task(service.dispatch_card(_card(), _workspace()))
    await entered.wait()
    task.cancel()
    # Late creation completes after cancellation; ownership must survive that race.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events[-1] == 'confirmed'
    assert not service._active_jobs


@pytest.mark.asyncio
async def test_post_review_preparation_shares_original_deadline(tmp_path, monkeypatch):
    import asyncio
    import time
    from unittest.mock import AsyncMock

    from tests.unit.test_353_bootstrap_pipeline import Repository

    service, cache, state, capability = setup(tmp_path)
    service.bind_kubernetes_state(state)
    original = service._build_manifest_artifacts
    marks = {}
    async def build(**kwargs):
        result = await original(**kwargs, capabilities=capability)
        cache.environment_attempt['expires_at'] = time.time()+0.02
        marks['built'] = time.monotonic()
        return result
    stopped = asyncio.Event()
    async def credentials(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            marks['stopped'] = time.monotonic()
            stopped.set()
    monkeypatch.setattr(service, '_build_manifest_artifacts', build)
    monkeypatch.setattr(service, '_load_test_env_vars', credentials)
    monkeypatch.setattr(service, '_fetch_declared_services', AsyncMock(return_value=[]))
    dispatch = AsyncMock()
    await asyncio.wait_for(service._do_dispatch(symphony_name='suite',
        symphony_config=SimpleNamespace(env_spec_files=[], env_bootstrap_performer_id='bootstrap'),
        github_service=Repository(), eff_config=SimpleNamespace(project_name='suite', github_org='org'),
        current_sha='fingerprint', cache_state=cache, dispatch_fn=dispatch, llm_chat=ScopeChatFixture(Proposal(), 'guide')), 5)
    # The shared 0.02 s deadline, not the harness ceiling, must stop post-review preparation.
    assert stopped.is_set() and marks['stopped'] - marks['built'] < 1.0
    dispatch.assert_not_awaited()
    assert cache.environment_attempt['status'] == 'held'
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
@pytest.mark.parametrize('repeat_cancel', [False, True])
@pytest.mark.parametrize('creation_fails', [False, True])
async def test_hung_creation_cancellation_is_bounded_and_late_writer_keeps_owned_cleanup(monkeypatch, repeat_cancel, creation_fails):
    import asyncio
    from unittest.mock import AsyncMock

    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.performer_runtime import StartedPerformer
    from tests.unit.services.test_http_performer_service import _ephemeral_config

    entered, release, removed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    present = False
    lookups = []
    async def create():
        nonlocal present
        entered.set()
        await release.wait()
        if creation_fails:
            raise RuntimeError('synthetic creation status is unknown')
        present = True
        return StartedPerformer(handle='owned', endpoint='http://localhost:1')
    async def find(session_id):
        lookups.append(session_id)
        return StartedPerformer(handle='owned', endpoint='http://localhost:1') if present else None
    async def stop(handle):
        nonlocal present
        assert handle == 'owned'
        present = False
        removed.set()
    service = HTTPPerformerService(_ephemeral_config(), client=AsyncMock(),
        runtime=SimpleNamespace(find_session=find, stop=stop))
    monkeypatch.setattr('coordinare.services.http_performer_service._CANCEL_CREATE_SECONDS', 0.02, raising=False)
    task = asyncio.create_task(service._run_creation('session-owned', create()))
    await entered.wait()
    task.cancel()
    if repeat_cancel:
        await asyncio.sleep(0)
        task.cancel()
    try:
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert done, 'cancellation waited indefinitely for runtime creation'
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await service.stop_session_confirmed('session-owned') is False
        assert lookups == [], 'absence cannot be confirmed while creation is pending'
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    if not creation_fails:
        await asyncio.wait_for(removed.wait(), 0.5)
    if service._teardown_tasks:
        settled = await asyncio.wait_for(asyncio.gather(*service._teardown_tasks, return_exceptions=True), 0.5)
        if not creation_fails:
            assert not any(isinstance(value, BaseException) for value in settled)
    assert not present
    if creation_fails:
        assert 'session-owned' in service._pending_creations
        assert await service.stop_session_confirmed('session-owned') is False
        assert lookups == []
    else:
        assert 'session-owned' not in service._pending_creations
