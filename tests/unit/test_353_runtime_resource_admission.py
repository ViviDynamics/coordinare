"""Kernel controls must match reviewed policy before a guest can start."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from coordinare_service_inference.environment import runtime_supervisor as supervisor
from coordinare_service_inference.environment.runtime_bundle import RuntimeBundleInputs

from tests.unit.test_353_runtime_supervisor import runtime_spec


@pytest.mark.parametrize('fault', [None, 'cpu', 'memory', 'swap', 'pids', 'privilege', 'bounding', 'gain', 'missing', 'config'])
def test_admission_refuses_unmeasured_or_different_kernel_controls(tmp_path, monkeypatch, fault):
    spec = runtime_spec()
    roots = [tmp_path/name for name in ('repo', 'cache', 'scratch')]
    for root in roots:
        root.mkdir(mode=0o700)
    bundle = tmp_path/'bundle.json'
    contents = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
        guest_image='/base', emulator='/qemu', firmware='/firmware')
    bundle.write_text(contents.model_dump_json())
    identity = spec.policy.identity.model_copy(update={'configuration_sha256': contents.digest()})
    spec = spec.model_copy(update={'policy': spec.policy.model_copy(update={'identity': identity})})
    if fault == 'config':
        bundle.write_text(contents.model_copy(update={'guest_image': '/changed'}).model_dump_json())
    monkeypatch.setattr(supervisor, 'BUNDLE_PATH', bundle)
    monkeypatch.setattr(supervisor, 'inspect_bundle', lambda **kwargs: spec.policy.identity.model_dump(
        exclude={'schema_version', 'carrier_image', 'carrier_platform'}))
    monkeypatch.setattr(supervisor.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(supervisor.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(supervisor.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 1024**4})())
    files = {'/sys/fs/cgroup/cpu.max': '200000 100000', '/sys/fs/cgroup/memory.max': str(4096*1024**2),
        '/sys/fs/cgroup/memory.swap.max': '0', '/sys/fs/cgroup/pids.max': '512',
        '/proc/self/status': 'CapInh:\t0\nCapPrm:\t0\nCapEff:\t0\nCapBnd:\t0\nCapAmb:\t0\nNoNewPrivs:\t1\n'}
    if fault in {'cpu', 'memory', 'pids'}:
        files['/sys/fs/cgroup/'+{'cpu': 'cpu.max', 'memory': 'memory.max', 'pids': 'pids.max'}[fault]] = 'max 100000' if fault == 'cpu' else 'max'
    elif fault == 'swap':
        files['/sys/fs/cgroup/memory.swap.max'] = '4096'
    elif fault in {'privilege', 'bounding'}:
        field = 'CapEff' if fault == 'privilege' else 'CapBnd'
        files['/proc/self/status'] = files['/proc/self/status'].replace(field+':\t0', field+':\t1')
    elif fault == 'gain':
        files['/proc/self/status'] = files['/proc/self/status'].replace('NoNewPrivs:\t1', 'NoNewPrivs:\t0')
    elif fault == 'missing':
        del files['/sys/fs/cgroup/cpu.max']
    read = Path.read_text

    def kernel_read(path, *args, **kwargs):
        name = str(path)
        if name.startswith(('/sys/fs/cgroup/', '/proc/self/')):
            if name not in files:
                raise OSError('kernel measurement unavailable')
            return files[name]
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', kernel_read)
    if fault is None:
        assert supervisor._admit(spec, repo=roots[0], cache=roots[1], scratch=roots[2], readonly=False, deadline=time.monotonic()+5).guest_image == '/base'
    else:
        with pytest.raises(ValueError, match='private runtime'):
            supervisor._admit(spec, repo=roots[0], cache=roots[1], scratch=roots[2], readonly=False, deadline=time.monotonic()+5)


@pytest.mark.asyncio
async def test_resource_drift_rejects_running_lease_and_still_removes_keys(tmp_path, monkeypatch):
    import socket
    import time

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    spec = runtime_spec()
    spec = spec.model_copy(update={'policy': spec.policy.model_copy(update={'control_port': port})})
    work = tmp_path/'owned'
    work.mkdir()
    (work/'client-key').write_text('owned ephemeral material')
    bundle = RuntimeBundleInputs(schema_version=1, guest_platform='linux/x86_64',
        guest_image='/base', emulator='/qemu', firmware='/firmware')
    lease = supervisor.RuntimeLease(spec, bundle, 'owned', work, tmp_path, tmp_path,
        time.monotonic()+10, False, '1'*64, '2'*64, {}, clean_shutdown=True)
    def drifted(resources):
        raise ValueError('private runtime kernel controls differ')
    monkeypatch.setattr(supervisor, 'require_runtime_resources', drifted)
    with pytest.raises(ValueError, match='kernel controls'):
        lease.check_bounds()
    report = await lease.close(deadline=lease.deadline)
    assert not report.bounds_respected
    assert report.keys_absent and report.qemu_reaped
    assert not work.exists()
