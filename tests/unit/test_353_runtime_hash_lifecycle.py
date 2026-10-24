"""Hashing shares the original deadline and never escapes owned cleanup."""
from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import runtime_supervisor as supervisor
from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs

from tests.unit.test_353_runtime_supervisor import runtime_spec


def lease_fixture(tmp_path, *, expired=False):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    spec = runtime_spec()
    spec = spec.model_copy(update={'policy': spec.policy.model_copy(update={'control_port': port})})
    work = tmp_path/'owned'
    work.mkdir()
    (work/'client-key').write_text('ephemeral material')
    artifact = tmp_path/'base'
    artifact.write_bytes(b'base image')
    bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
        guest_image=str(artifact), emulator='/qemu', firmware='/firmware')
    return supervisor.RuntimeLease(spec, bundle, 'owned', work, tmp_path, tmp_path,
        time.monotonic()+(-1 if expired else 5), False, '1'*64, '2'*64, {},
        initial_artifacts={artifact: hashlib.sha256(artifact.read_bytes()).hexdigest()}, clean_shutdown=True)


@pytest.mark.asyncio
async def test_expired_cleanup_cannot_claim_successful_artifact_verification(tmp_path):
    lease = lease_fixture(tmp_path, expired=True)
    report = await lease.close(deadline=lease.deadline)
    assert report.keys_absent
    assert not report.artifacts_unchanged, 'expired hashing was accepted as verified'
    assert not report.confirmed
    assert not lease.work_root.exists()


@pytest.mark.asyncio
async def test_malformed_artifact_cannot_prevent_owned_key_cleanup(tmp_path, monkeypatch):
    lease = lease_fixture(tmp_path)
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    def malformed(path, **kwargs):
        raise ValueError('artifact stopped being a regular file')
    monkeypatch.setattr(supervisor, '_digest', malformed)
    report = await lease.close(deadline=lease.deadline)
    assert report.keys_absent and not report.artifacts_unchanged
    assert not lease.work_root.exists()


def test_hash_checks_deadline_between_actual_reads(tmp_path, monkeypatch):
    from coordinare_service_inference.environment import runtime_io

    path = tmp_path/'large'
    path.write_bytes(b'x'*(2*1024*1024))
    clock = [0.0]
    read = runtime_io.os.read
    reads = []
    def advancing_read(descriptor, count):
        reads.append(count)
        block = read(descriptor, count)
        clock[0] = 2.0
        return block
    monkeypatch.setattr(runtime_io, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runtime_io.os, 'read', advancing_read)
    with pytest.raises(TimeoutError, match='deadline'):
        runtime_io.digest_file(path, deadline=1.0)
    assert len(reads) == 1


@pytest.mark.asyncio
async def test_cancelled_io_joins_worker_even_after_repeated_cancellation():
    from coordinare_service_inference.environment.runtime_io import run_io

    started, stopping, finished, release = (threading.Event() for _ in range(4))
    def operation(*, deadline, stop):
        started.set()
        if stop.wait(2):
            stopping.set()
            release.wait(2)
        finished.set()
    async def wait(event):
        async with asyncio.timeout(2):
            while not event.is_set():
                await asyncio.sleep(0)
    task = asyncio.create_task(run_io(operation, deadline=time.monotonic()+5))
    try:
        await wait(started)
        task.cancel()
        await wait(stopping)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), 'worker was detached before it finished'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_bounds_failure_still_removes_owned_keys(tmp_path, monkeypatch):
    lease = lease_fixture(tmp_path)
    def unknown(**kwargs):
        raise TypeError('measurement unavailable')
    monkeypatch.setattr(lease, 'check_bounds', unknown)
    try:
        with pytest.raises(TypeError, match='measurement unavailable'):
            await lease.close(deadline=lease.deadline)
    finally:
        assert not lease.work_root.exists(), 'unknown bounds failure bypassed key cleanup'


@pytest.mark.asyncio
async def test_repeated_close_cancellation_waits_for_artifact_worker_and_key_removal(tmp_path, monkeypatch):
    lease = lease_fixture(tmp_path)
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    started, finished, release = (threading.Event() for _ in range(3))
    digest = supervisor._digest
    def held(path, **kwargs):
        started.set()
        try:
            release.wait(2)
            return digest(path, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(supervisor, '_digest', held)
    task = asyncio.create_task(lease.close(deadline=lease.deadline))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), 'close released ownership while artifact verification was still active'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and not lease.work_root.exists()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if lease.close_task is not None:
            await asyncio.gather(lease.close_task, return_exceptions=True)


@pytest.mark.parametrize('content', [b'', b'payload'*300000])
def test_chunked_hash_retains_exact_sha256(tmp_path, content):
    from coordinare_service_inference.environment.runtime_io import digest_file

    path = tmp_path/'artifact'
    path.write_bytes(content)
    assert digest_file(path, deadline=time.monotonic()+2) == hashlib.sha256(content).hexdigest()


def test_hash_rejects_source_mutation_during_read(tmp_path, monkeypatch):
    from coordinare_service_inference.environment import runtime_io

    path = tmp_path/'artifact'
    path.write_bytes(b'initial')
    read = runtime_io.os.read
    def changing_read(descriptor, count):
        block = read(descriptor, count)
        if block:
            path.write_bytes(b'changed bytes')
        return block
    monkeypatch.setattr(runtime_io.os, 'read', changing_read)
    with pytest.raises(ValueError, match='changed while hashing'):
        runtime_io.digest_file(path, deadline=time.monotonic()+2)


@pytest.mark.asyncio
async def test_cancelled_seal_drains_hashing_and_removes_unpublished_artifact(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    lease = lease_fixture(tmp_path)
    lease.install = lease.installation_passed = True
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    monkeypatch.setattr(lease, 'stop_guest', AsyncMock())
    async def convert(argv, **kwargs):
        (lease.cache/'installed.qcow2').write_bytes(b'converted artifact')
        return 0, b''
    monkeypatch.setattr(supervisor, '_command', convert)
    started, stopping, finished, release = (threading.Event() for _ in range(4))
    digest = supervisor._digest
    def held(path, *, deadline, stop):
        assert deadline == lease.deadline
        if path.name == 'installed.qcow2':
            started.set()
            try:
                if stop.wait(2):
                    stopping.set()
                    release.wait(2)
            finally:
                finished.set()
        return digest(path, deadline=deadline, stop=stop)
    monkeypatch.setattr(supervisor, '_digest', held)
    async def wait(event):
        async with asyncio.timeout(2):
            while not event.is_set():
                await asyncio.sleep(0)
    task = asyncio.create_task(supervisor.seal_runtime(lease, manifest_digest='1'*64,
        source_digest='2'*64, deadline=lease.deadline+60))
    try:
        await wait(started)
        task.cancel()
        await wait(stopping)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert not (lease.cache/'installed.qcow2').exists()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
