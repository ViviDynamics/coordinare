from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import runtime_io
from coordinare_service_inference.environment import runtime_supervisor as supervisor

from tests.unit.test_353_runtime_hash_lifecycle import lease_fixture


@pytest.mark.asyncio
async def test_cleanup_that_crosses_original_deadline_cannot_remain_accepted(tmp_path, monkeypatch):
    lease = lease_fixture(tmp_path)
    clock = [100.0]
    lease.deadline = 110.0
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    monkeypatch.setattr(supervisor, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(runtime_io, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    remove = runtime_io.os.rmdir
    def late_remove(path, *args, **kwargs):
        result = remove(path, *args, **kwargs)
        if Path(path) == lease.work_root:
            clock[0] = 111.0
        return result
    monkeypatch.setattr(runtime_io.os, 'rmdir', late_remove)
    report = await lease.close(deadline=lease.deadline)
    assert report.keys_absent
    assert not report.bounds_respected, 'cleanup crossed original deadline but report remains accepted'


def test_owned_removal_does_not_follow_source_links(tmp_path):
    import time

    from coordinare_service_inference.environment.runtime_cleanup import remove_owned_tree

    source = tmp_path/'source'
    source.mkdir()
    (source/'important').write_text('keep')
    work = tmp_path/'owned'
    (work/'nested').mkdir(parents=True)
    (work/'nested'/'link').symlink_to(source, target_is_directory=True)
    (work/'nested'/'regular').write_bytes(b'data')
    remove_owned_tree(work, deadline=time.monotonic()+5)
    assert not work.exists()
    assert (source/'important').read_text() == 'keep'


def test_owned_removal_stops_between_entries(tmp_path, monkeypatch):
    from coordinare_service_inference.environment.runtime_cleanup import remove_owned_tree

    work = tmp_path/'owned'
    work.mkdir()
    for name in ('one', 'two', 'three'):
        (work/name).write_text('data')
    clock = [0.0]
    monkeypatch.setattr(runtime_io, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    unlink = runtime_io.os.unlink
    removed = []
    def advancing_unlink(path, **kwargs):
        unlink(path, **kwargs)
        removed.append(path)
        clock[0] = 2.0
    monkeypatch.setattr(runtime_io.os, 'unlink', advancing_unlink)
    with pytest.raises(TimeoutError, match='deadline'):
        remove_owned_tree(work, deadline=1.0)
    assert len(removed) == 1
    assert len(list(work.iterdir())) == 2


@pytest.mark.asyncio
async def test_expired_cleanup_removes_known_credentials_without_walking_unknown_data(tmp_path):
    lease = lease_fixture(tmp_path, expired=True)
    (lease.work_root/'unknown').mkdir()
    (lease.work_root/'unknown'/'data').write_text('must not traverse after expiry')
    (lease.work_root/'guest-control').mkdir()
    (lease.work_root/'guest-control'/'host-key').write_text('secret')
    report = await lease.close(deadline=lease.deadline)
    assert not (lease.work_root/'client-key').exists()
    assert not (lease.work_root/'guest-control').exists()
    assert (lease.work_root/'unknown'/'data').exists()
    assert not report.keys_absent
    assert not report.bounds_respected
    assert not report.confirmed


@pytest.mark.asyncio
async def test_removal_failure_cannot_be_waived_by_emergency_key_cleanup(tmp_path, monkeypatch):
    lease = lease_fixture(tmp_path)
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    def denied(*args, **kwargs):
        raise PermissionError('tree removal failed')
    monkeypatch.setattr(supervisor, 'remove_owned_tree', denied)
    report = await lease.close(deadline=lease.deadline)
    assert report.keys_absent
    assert not report.bounds_respected


def test_emergency_cleanup_does_not_follow_replaced_control_directory(tmp_path):
    from coordinare_service_inference.environment.runtime_cleanup import remove_control_material

    source = tmp_path/'source'
    source.mkdir()
    (source/'host-key').write_text('unrelated')
    work = tmp_path/'owned'
    work.mkdir()
    (work/'guest-control').symlink_to(source, target_is_directory=True)
    (work/'client-key').write_text('owned secret')
    remove_control_material(work)
    assert not (work/'client-key').exists()
    assert (source/'host-key').read_text() == 'unrelated'
    assert (work/'guest-control').is_symlink()


@pytest.mark.asyncio
async def test_close_keeps_tree_worker_owned_through_repeated_cancellation(tmp_path, monkeypatch):
    import asyncio
    import threading

    lease = lease_fixture(tmp_path)
    monkeypatch.setattr(lease, 'check_bounds', lambda **kwargs: None)
    started, release = threading.Event(), threading.Event()
    unlink = runtime_io.os.unlink
    def blocked_unlink(path, **kwargs):
        if path == 'client-key':
            started.set()
            if not release.wait(2):
                raise TimeoutError('test did not release worker')
        unlink(path, **kwargs)
    monkeypatch.setattr(runtime_io.os, 'unlink', blocked_unlink)
    task = asyncio.create_task(lease.close(deadline=lease.deadline))
    try:
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert lease.work_root.exists()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lease.work_root.exists()
    assert lease.close_task.done()
    assert lease.cleanup.keys_absent


def test_importing_optional_cleanup_adapter_does_not_require_posix_flags(monkeypatch):
    import importlib

    from coordinare_service_inference.environment import runtime_cleanup

    with monkeypatch.context() as patch:
        patch.delattr(runtime_cleanup.os, 'O_DIRECTORY')
        patch.delattr(runtime_cleanup.os, 'O_NOFOLLOW')
        importlib.reload(runtime_cleanup)
