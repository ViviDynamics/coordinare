"""Runtime guards own filesystem scans until worker threads have stopped."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runtime_execution import run_guarded


async def _wait(event: threading.Event) -> None:
    async with asyncio.timeout(3):
        while not event.is_set():
            await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True], ids=['operation-completes', 'operation-cancelled'])
async def test_guard_does_not_release_runtime_while_scanner_thread_is_active(cancel: bool) -> None:
    started, finished, release = threading.Event(), threading.Event(), threading.Event()

    def scan(*, stop: threading.Event | None = None) -> None:
        started.set()
        try:
            (stop if stop is not None else release).wait(3)
        finally:
            finished.set()

    async def operation() -> int:
        await _wait(started)
        if cancel:
            await asyncio.Event().wait()
        return 42

    task = asyncio.create_task(run_guarded(SimpleNamespace(deadline=time.monotonic()+5, check_bounds=scan), operation()))
    try:
        await _wait(started)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        else:
            assert await asyncio.wait_for(task, timeout=1) == 42
        assert finished.is_set(), 'guard returned before its filesystem worker stopped'
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_allocation_scan_checks_original_deadline_during_traversal(tmp_path, monkeypatch) -> None:
    from coordinare_service_inference.environment import runtime_supervisor as supervisor
    from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs

    from tests.unit.test_353_runtime_supervisor import runtime_spec

    (tmp_path/'payload').write_bytes(b'x'*8192)
    clock = [0.0]
    real_scan = supervisor.os.scandir

    def advancing_scan(path):
        clock[0] = 2.0
        return real_scan(path)

    # Isolate the deadline from the host's unrelated free-space reserve.
    monkeypatch.setattr(supervisor.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024**4))
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(supervisor.os, 'scandir', advancing_scan)
    bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
                                 guest_image='/base', emulator='/qemu', firmware='/firmware')
    lease = supervisor.RuntimeLease(runtime_spec(), bundle, 'owned', tmp_path, tmp_path, tmp_path,
                                    1.0, False, '1'*64, '2'*64, {})
    with pytest.raises(TimeoutError, match='deadline'):
        lease.check_bounds()


@pytest.mark.asyncio
async def test_repeated_cancellation_still_waits_for_worker_to_finish() -> None:
    started, stopping, finished, release = (threading.Event() for _ in range(4))

    def scan(*, stop: threading.Event) -> None:
        started.set()
        if stop.wait(3):
            stopping.set()
            release.wait(3)
        finished.set()

    task = asyncio.create_task(run_guarded(SimpleNamespace(deadline=time.monotonic()+5, check_bounds=scan),
                                           asyncio.Event().wait()))
    try:
        await _wait(started)
        task.cancel()
        await _wait(stopping)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), 'repeated cancellation detached the worker'
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert finished.is_set()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_resource_failure_during_scan_drain_cannot_become_success() -> None:
    started = threading.Event()

    def scan(*, stop: threading.Event) -> None:
        started.set()
        stop.wait(3)
        raise RuntimeError('cache ceiling exhausted')

    async def operation() -> int:
        await _wait(started)
        return 42

    with pytest.raises(RuntimeError, match='cache ceiling'):
        await asyncio.wait_for(run_guarded(SimpleNamespace(deadline=time.monotonic()+5, check_bounds=scan),
                                           operation()), timeout=1)


def test_allocation_measurement_does_not_follow_links_and_stops_at_ceiling(tmp_path) -> None:
    from coordinare_service_inference.environment.runtime_supervisor import _allocated

    outside, root = tmp_path/'outside', tmp_path/'root'
    outside.mkdir()
    (outside/'large').write_bytes(b'x'*65536)
    root.mkdir()
    (root/'link').symlink_to(outside, target_is_directory=True)
    assert _allocated(root, deadline=time.monotonic()+1, stop=None, limit=65536) == (root/'link').lstat().st_blocks * 512
    (root/'link').unlink()
    for index in range(10):
        (root/str(index)).write_bytes(b'x'*8192)
    used = _allocated(root, deadline=time.monotonic()+1, stop=None, limit=1)
    assert used == (root/'0').stat().st_blocks * 512


def test_missing_allocation_root_is_unknown_instead_of_zero(tmp_path) -> None:
    from coordinare_service_inference.environment.runtime_supervisor import _allocated

    with pytest.raises(FileNotFoundError):
        _allocated(tmp_path/'missing', deadline=time.monotonic()+1, stop=None, limit=1024)


def test_unreadable_subtree_is_unknown_instead_of_zero(tmp_path, monkeypatch) -> None:
    from coordinare_service_inference.environment import runtime_supervisor as supervisor

    blocked = tmp_path/'blocked'
    blocked.mkdir()
    real_scan = supervisor.os.scandir
    inode = blocked.stat().st_ino

    def denied_scan(path):
        if path == str(blocked) or path == blocked or (isinstance(path, int) and supervisor.os.fstat(path).st_ino == inode):
            raise PermissionError('fixture subtree cannot be read')
        return real_scan(path)

    monkeypatch.setattr(supervisor.os, 'scandir', denied_scan)
    with pytest.raises(PermissionError):
        supervisor._allocated(tmp_path, deadline=time.monotonic()+1, stop=None,
                              limit=blocked.stat().st_blocks*512 + 1024)
