"""Refuse unusable runtime identities before creating guest resources."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt, RuntimeSpec
from coordinare_service_inference.environment.runtime_supervisor import open_runtime

from tests.unit.test_353_runtime_identity import policy_data


def runtime_spec():
    return RuntimeSpec.model_validate({'schema_version': 1, 'policy': policy_data(), 'endpoints': []})


@pytest.mark.asyncio
@pytest.mark.parametrize('still_listening', [False, True], ids=['closed-time-wait', 'live-listener'])
async def test_cleanup_distinguishes_closed_tcp_connections_from_live_listeners(tmp_path, monkeypatch, still_listening):
    import socket

    from coordinare_service_inference.environment import runtime_supervisor as supervisor
    from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', 0))
    port = listener.getsockname()[1]
    listener.listen()
    client = socket.create_connection(('127.0.0.1', port))
    connection, _ = listener.accept()
    connection.shutdown(socket.SHUT_WR)
    assert client.recv(1) == b''
    client.close()
    connection.close()
    if not still_listening:
        listener.close()
    try:
        # Confirm the original bind-only cleanup reports both states occupied.
        with socket.socket() as probe, pytest.raises(OSError):
            probe.bind(('127.0.0.1', port))
        spec = runtime_spec()
        spec = spec.model_copy(update={'policy': spec.policy.model_copy(update={'control_port': port})})
        work = tmp_path/'owned'
        work.mkdir()
        bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
                                    guest_image='/base', emulator='/qemu', firmware='/firmware')
        lease = supervisor.RuntimeLease(spec, bundle, 'owned', work, tmp_path, tmp_path,
                                        time.monotonic()+30, False, '1'*64, '2'*64, {}, clean_shutdown=True)
        monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
        report = await lease.close(deadline=lease.deadline)
        assert report.listeners_absent is not still_listening
    finally:
        listener.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['missing', 'manifest', 'source', 'runtime'])
async def test_consumer_refuses_missing_or_mismatched_receipt_before_side_effects(tmp_path: Path, problem):
    spec = runtime_spec()
    raw = {'schema_version': 1, 'manifest_digest': '1' * 64, 'source_digest': '2' * 64,
           'runtime_digest': spec.digest(), 'sealed': True,
           'artifacts': [{'path': 'installed.qcow2', 'sha256': '3' * 64, 'bytes': 100}]}
    if problem != 'missing':
        raw[problem + '_digest'] = '4' * 64
    receipt = None if problem == 'missing' else RuntimeArtifactReceipt.model_validate(raw)
    with pytest.raises(ValueError, match=r'receipt|identity'):
        await open_runtime(spec, artifacts=receipt, repo=tmp_path/'repo', cache=tmp_path/'cache',
                           scratch=tmp_path/'scratch', install=False, deadline=time.monotonic()+30,
                           manifest_digest='1' * 64, source_digest='2' * 64)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_expired_runtime_deadline_does_not_start_or_create_resources(tmp_path):
    with pytest.raises(TimeoutError):
        await open_runtime(runtime_spec(), artifacts=None, repo=tmp_path/'repo', cache=tmp_path/'cache',
                           scratch=tmp_path/'scratch', install=True, deadline=time.monotonic()-1,
                           manifest_digest='1'*64, source_digest='2'*64)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_artifact_read_failure_still_removes_owned_control_keys(tmp_path, monkeypatch):
    from coordinare_service_inference.environment import runtime_supervisor as supervisor
    from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs

    work = tmp_path/'owned'
    work.mkdir()
    (work/'client-key').write_text('ephemeral test material')
    artifact = tmp_path/'artifact'
    artifact.write_bytes(b'original')
    bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
                                guest_image='/base', emulator='/qemu', firmware='/firmware')
    lease = supervisor.RuntimeLease(runtime_spec(), bundle, 'owned', work, tmp_path, tmp_path,
                                    time.monotonic()+1, False, '1'*64, '2'*64, {},
                                    initial_artifacts={artifact: 'a'*64})
    def unreadable(path, **kwargs):
        raise OSError('artifact read unavailable')
    monkeypatch.setattr(supervisor, '_digest', unreadable)
    report = await lease.close(deadline=time.monotonic()+1)
    assert report.keys_absent
    assert not report.artifacts_unchanged
    assert not report.confirmed
    assert not work.exists()


@pytest.mark.asyncio
async def test_cancelled_close_still_reaps_its_real_child_and_removes_keys(tmp_path, monkeypatch):
    import asyncio
    import sys

    from coordinare_service_inference.environment import runtime_supervisor as supervisor
    from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs

    work = tmp_path/'owned'
    work.mkdir()
    (work/'client-key').write_text('ephemeral test material')
    process = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time;time.sleep(60)', start_new_session=True)
    bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
                                guest_image='/base', emulator='/qemu', firmware='/firmware')
    lease = supervisor.RuntimeLease(runtime_spec(), bundle, 'owned', work, tmp_path, tmp_path,
                                    time.monotonic()+0.2, False, '1'*64, '2'*64, {}, process=process)
    started = asyncio.Event()
    async def stuck_stop(self, *, deadline):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(supervisor.RuntimeLease, 'stop_guest', stuck_stop)
    task = asyncio.create_task(lease.close(deadline=lease.deadline))
    try:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.returncode is not None
        assert not work.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_runtime_spec_is_in_manifest_digest_without_changing_native_serialization():
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from tests.unit.env_manifest_fixture import reviewed_metadata

    raw = reviewed_metadata()['environment_manifest']
    native = EnvironmentManifest.model_validate(raw)
    assert 'runtime' not in native.model_dump()
    runtime = EnvironmentManifest.model_validate({**raw, 'schema_version': 2, 'policy_version': '353-v2',
                                                   'runtime': runtime_spec().model_dump()})
    assert runtime.runtime == runtime_spec()
    assert runtime.digest() != native.digest()
    with pytest.raises(ValueError):
        EnvironmentManifest.model_validate({**raw, 'runtime': runtime_spec().model_dump()})
    for key in ('DOCKER_HOST', 'DOCKER_CONFIG', 'DOCKER_CONTEXT'):
        bad = runtime.model_dump()
        bad['activation']['env'][key] = 'untrusted override'
        with pytest.raises(ValueError, match='runtime'):
            EnvironmentManifest.model_validate(bad)


@pytest.mark.asyncio
async def test_live_runtime_environment_reaches_real_checks_without_global_mutation(tmp_path, monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.runner import run_manifest

    from tests.unit.env_manifest_fixture import reviewed_metadata

    leased_scratch = tmp_path/'leased'
    requested_scratch = tmp_path/'requested'
    leased_scratch.mkdir()
    requested_scratch.mkdir()
    raw = reviewed_metadata()['environment_manifest']
    manifest = EnvironmentManifest.model_validate({**raw, 'schema_version': 2, 'policy_version': '353-v2',
        'runtime': runtime_spec().model_dump(), 'install': [], 'requirements': [], 'empty_reason': 'synthetic runtime fixture',
        'activation': {'path': [], 'env': {}}, 'services': [],
        'checks': [{'id': 'runtime-env', 'argv': [sys.executable, '-c',
                   'import os;from pathlib import Path; assert os.environ["DOCKER_HOST"] == "ssh://private-fixture";Path("{scratch}/marker").touch()']}],
    })
    monkeypatch.setenv('DOCKER_HOST', 'caller-original')
    lease = SimpleNamespace(spec=runtime_spec(), manifest_digest=manifest.digest(), source_digest='2'*64,
                            deadline=time.monotonic()+5, install=False, scratch=leased_scratch,
                            activation_env={'DOCKER_HOST': 'ssh://private-fixture', 'PATH': os.environ['PATH']},
                            check_bounds=lambda: None)
    result = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=requested_scratch,
                                execution_source_digest='2'*64, runtime_lease=lease)
    assert result.passed
    assert (leased_scratch/'marker').exists()
    assert not (requested_scratch/'marker').exists()
    assert result.results[0].exit_code == 0
    assert result.runtime_phase == 'live_consumer'
    assert result.runtime_cleanup is None  # Still held for the ordinary job lifetime.
    assert os.environ['DOCKER_HOST'] == 'caller-original'


@pytest.mark.asyncio
async def test_runtime_limit_failure_cancels_and_reaps_a_running_project_check(tmp_path):
    import os
    import sys
    from types import SimpleNamespace

    from coordinare_service_inference.environment.models import EnvironmentManifest
    from coordinare_service_inference.environment.runner import run_manifest
    from coordinare_service_inference.environment.runtime_execution import run_guarded

    from tests.unit.env_manifest_fixture import reviewed_metadata

    cache, repo = tmp_path/'cache', tmp_path/'repo'
    cache.mkdir()
    repo.mkdir()
    raw = reviewed_metadata()['environment_manifest']
    manifest = EnvironmentManifest.model_validate({**raw, 'requirements': [], 'install': [], 'services': [],
        'activation': {'path': [], 'env': {}}, 'checks': [{'id': 'writer', 'argv': [sys.executable, '-c',
        'import os,time;from pathlib import Path;Path("{repo}/pid").write_text(str(os.getpid()));Path("{cache}/data").write_bytes(b"x"*65536);time.sleep(60)']}]})
    def limits():
        if (cache/'data').exists():
            raise RuntimeError('private runtime cache ceiling exhausted')
    lease = SimpleNamespace(deadline=time.monotonic()+5, check_bounds=limits)
    with pytest.raises(RuntimeError, match='cache ceiling'):
        await run_guarded(lease, run_manifest(manifest, cache=cache, repo=repo, scratch=tmp_path))
    pid = int((repo/'pid').read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
