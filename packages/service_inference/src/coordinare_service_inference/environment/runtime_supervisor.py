"""Owned private-service guest lifecycle; no repository or framework inference."""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import platform
import shlex
import shutil
import signal
import socket
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .runtime import RuntimeArtifactReceipt, RuntimeIdentity, RuntimeSpec
from .runtime_bundle import BUNDLE_PATH, RuntimeBundleInputs, inspect_bundle, read_bundle
from .runtime_guest import cloud_configuration, qemu_arguments
from .runtime_io import RuntimeIOStoppedError as RuntimeScanStoppedError
from .runtime_io import digest_file as _digest
from .runtime_io import run_io, settle
from .runtime_resources import require_runtime_resources

if TYPE_CHECKING:
    from collections.abc import Iterator
    from threading import Event


def _remaining(deadline: float) -> float:
    if not math.isfinite(deadline):
        raise ValueError("private runtime deadline must be finite")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('private runtime deadline exhausted')
    return remaining


def _scan_active(deadline: float, stop: Event | None) -> None:
    if stop is not None and stop.is_set():
        raise RuntimeScanStoppedError
    _remaining(deadline)


def _allocation_entries(descriptor: int, deadline: float, stop: Event | None) -> Iterator[os.stat_result]:
    _scan_active(deadline, stop)
    with os.scandir(descriptor) as members:
        for member in members:
            _scan_active(deadline, stop)
            try:
                metadata = member.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue  # A running project may remove entries during measurement.
            _scan_active(deadline, stop)
            yield metadata
            if stat.S_ISDIR(metadata.st_mode):
                try:
                    child = os.open(member.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
                try:
                    yield from _allocation_entries(child, deadline, stop)
                finally:
                    os.close(child)


def _allocated(path: Path, *, deadline: float, stop: Event | None, limit: int) -> int:
    total = 0
    _scan_active(deadline, stop)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with contextlib.closing(_allocation_entries(descriptor, deadline, stop)) as members:
            for metadata in members:
                total += metadata.st_blocks * 512
                if total > limit:
                    break
    finally:
        os.close(descriptor)
    _scan_active(deadline, stop)
    return total


async def _command(argv: list[str], *, deadline: float, check: bool = True) -> tuple[int, bytes]:
    timeout = _remaining(deadline)
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        code = process.returncode
        if code is None or (check and code != 0):
            raise RuntimeError('private runtime platform command failed')
        return code, output
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@dataclass(frozen=True)
class RuntimeCleanupReport:
    qemu_reaped: bool
    keys_absent: bool
    listeners_absent: bool
    artifacts_unchanged: bool
    clean_shutdown: bool
    bounds_respected: bool = True

    @property
    def confirmed(self) -> bool:
        return all((self.qemu_reaped, self.keys_absent, self.listeners_absent, self.artifacts_unchanged))


@dataclass
class RuntimeLease:
    spec: RuntimeSpec
    bundle: RuntimeBundleInputs
    owner_id: str
    work_root: Path
    cache: Path
    scratch: Path
    deadline: float
    install: bool
    manifest_digest: str | None
    source_digest: str | None
    activation_env: dict[str, str]
    process: asyncio.subprocess.Process | None = None
    initial_artifacts: dict[Path, str] = field(default_factory=dict)
    installation_passed: bool = False
    clean_shutdown: bool = False
    cleanup: RuntimeCleanupReport | None = None
    close_task: asyncio.Task[RuntimeCleanupReport] | None = None

    @property
    def identity(self) -> RuntimeIdentity:
        return self.spec.policy.identity

    async def ssh(self, script: str, *, deadline: float, check: bool = True) -> tuple[int, bytes]:
        return await _command(['/usr/bin/ssh', '-F', str(self.work_root/'ssh-config'),
                               '127.0.0.1', script], deadline=min(deadline, self.deadline), check=check)

    def check_bounds(self, *, stop: Event | None = None, deadline: float | None = None) -> None:
        deadline = self.deadline if deadline is None else min(deadline, self.deadline)
        _scan_active(deadline, stop)
        bounds = self.spec.policy.resources
        require_runtime_resources(bounds)
        if shutil.disk_usage(self.scratch).free < bounds.reserve_bytes:
            raise RuntimeError('private runtime free-space reserve exhausted')
        if _allocated(self.cache, deadline=deadline, stop=stop, limit=bounds.cache_bytes) > bounds.cache_bytes:
            raise RuntimeError('private runtime cache ceiling exhausted')
        if _allocated(self.scratch, deadline=deadline, stop=stop, limit=bounds.scratch_bytes) + _allocated(
                self.work_root, deadline=deadline, stop=stop, limit=bounds.scratch_bytes) > bounds.scratch_bytes:
            raise RuntimeError('private runtime scratch ceiling exhausted')
        _scan_active(deadline, stop)
        if self.process is not None and self.process.returncode is not None and not self.clean_shutdown:
            raise RuntimeError('private runtime guest exited unexpectedly')

    async def stop_guest(self, *, deadline: float) -> None:
        if self.clean_shutdown:
            return
        if self.process is None or self.process.returncode is not None:
            raise RuntimeError('private runtime guest is not running')
        # Refuse sealing implicit service state. Required teardown failures remain failures.
        await self.ssh('set -eu; test -z "$(docker ps -aq)"; test -z "$(docker volume ls -q)"; '
                       'test -z "$(docker network ls --filter type=custom -q)"; '
                       'rc-service docker stop; rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys; '
                       'rm -rf /var/lib/cloud; sync; fstrim /', deadline=deadline)
        await self.ssh('poweroff', deadline=deadline, check=False)
        await asyncio.wait_for(self.process.wait(), timeout=_remaining(min(deadline, self.deadline)))
        if self.process.returncode != 0:
            raise RuntimeError('private runtime shutdown failed')
        self.clean_shutdown = True

    async def close(self, *, deadline: float) -> RuntimeCleanupReport:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close_owned(deadline=deadline))
        return await settle(self.close_task)

    def _artifacts_unchanged(self, *, deadline: float, stop: Event | None = None) -> bool:
        try:
            return all(path.is_file() and not path.is_symlink()
                       and _digest(path, deadline=min(deadline, self.deadline), stop=stop) == digest
                       for path, digest in self.initial_artifacts.items())
        except (OSError, ValueError):
            return False

    async def _close_owned(self, *, deadline: float) -> RuntimeCleanupReport:
        cleanup_deadline = min(deadline, self.deadline, time.monotonic()+15)
        respected = False
        try:
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                await run_io(self.check_bounds, deadline=cleanup_deadline)
                respected = True
            if self.process is not None and self.process.returncode is None:
                with contextlib.suppress(OSError, RuntimeError, TimeoutError):
                    await asyncio.wait_for(self.stop_guest(deadline=cleanup_deadline), timeout=_remaining(cleanup_deadline))
        finally:
            try:
                if self.process is not None and self.process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                    await asyncio.wait_for(self.process.wait(), timeout=5)
            finally:
                unchanged = False
                try:
                    with contextlib.suppress(OSError, ValueError):
                        unchanged = await run_io(self._artifacts_unchanged, deadline=cleanup_deadline)
                finally:
                    with contextlib.suppress(OSError):
                        shutil.rmtree(self.work_root)
                    absent = True
                    for port in [self.spec.policy.control_port, *(endpoint.outer_port for endpoint in self.spec.endpoints)]:
                        with socket.socket() as listener:
                            try:
                                # Closed SSH connections can remain in TIME_WAIT after QEMU is reaped.
                                # Reuse that state while still requiring an exclusive live listener.
                                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                                listener.bind(('127.0.0.1', port))
                                listener.listen(1)
                            except OSError:
                                absent = False
                    self.cleanup = RuntimeCleanupReport(
                        qemu_reaped=self.process is None or self.process.returncode is not None,
                        keys_absent=not self.work_root.exists(), listeners_absent=absent,
                        artifacts_unchanged=unchanged, clean_shutdown=self.clean_shutdown, bounds_respected=respected,
                    )
        return self.cleanup


def _verify_receipt(spec: RuntimeSpec, receipt: RuntimeArtifactReceipt | None,
                    manifest_digest: str | None, source_digest: str | None) -> RuntimeArtifactReceipt:
    if receipt is None or manifest_digest is None or source_digest is None:
        raise ValueError('private runtime consumer requires bound artifact receipt')
    receipt.require_binding(manifest_digest=manifest_digest, source_digest=source_digest, runtime_digest=spec.digest())
    if len(receipt.artifacts) != 1 or receipt.artifacts[0].path != 'installed.qcow2':
        raise ValueError('private runtime receipt artifact layout is unsupported')
    return receipt


def _admit(spec: RuntimeSpec, *, repo: Path, cache: Path, scratch: Path, readonly: bool,
           deadline: float, stop: Event | None = None) -> RuntimeBundleInputs:
    actual = platform.system().lower() + '/' + platform.machine().lower()
    if actual != spec.policy.identity.carrier_platform or platform.system() != 'Linux':
        raise ValueError('private runtime carrier platform differs')
    require_runtime_resources(spec.policy.resources)
    observed = inspect_bundle(deadline=deadline, stop=stop)
    expected = spec.policy.identity.model_dump(exclude={'schema_version', 'carrier_image', 'carrier_platform'})
    if observed != expected:
        raise ValueError('private runtime bundle identity differs')
    bundle = read_bundle(BUNDLE_PATH, deadline=deadline, stop=stop)
    if bundle.digest() != spec.policy.identity.configuration_sha256:
        raise ValueError('private runtime bundle configuration differs')
    roots = [repo.resolve(strict=True), cache.resolve(strict=True), scratch.resolve(strict=True)]
    if any(not path.is_dir() or path == Path.home() for path in roots):
        raise ValueError('private runtime exports require owned directories')
    if len(set(roots)) != 3 or any(left in right.parents for left in roots for right in roots if left != right):
        raise ValueError('private runtime export roots overlap')
    if scratch.stat().st_mode & 0o077 or scratch.stat().st_uid != os.getuid():
        raise ValueError('private runtime project scratch must be private and owned')
    if readonly:
        if not os.statvfs(cache).f_flag & os.ST_RDONLY:
            raise ValueError('private runtime consumer cache must be mounted read-only')
        from .runtime_network import require_isolated_outer_network

        require_isolated_outer_network()
    if shutil.disk_usage(scratch).free < spec.policy.resources.scratch_bytes + spec.policy.resources.reserve_bytes + (0 if readonly else spec.policy.resources.cache_bytes):
        raise ValueError('private runtime storage capacity unavailable')
    for port in [spec.policy.control_port, *(endpoint.outer_port for endpoint in spec.endpoints)]:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))
    return bundle


async def _prepare_control(lease: RuntimeLease, *, repo: Path) -> list[str]:
    work = lease.work_root
    guest_control = work/'guest-control'
    guest_control.mkdir(mode=0o700)
    for path in (work/'client-key', guest_control/'host-key'):
        await _command(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(path)], deadline=lease.deadline)
    shutil.copyfile(work/'client-key.pub', guest_control/'authorized_keys')
    (work/'known-hosts').write_text(f'[127.0.0.1]:{lease.spec.policy.control_port} ' + (guest_control/'host-key.pub').read_text())
    (work/'ssh-config').write_text('Host 127.0.0.1\n User root\n'
        f' Port {lease.spec.policy.control_port}\n IdentityFile {work}/client-key\n UserKnownHostsFile {work}/known-hosts\n'
        ' StrictHostKeyChecking yes\n IdentitiesOnly yes\n BatchMode yes\n PasswordAuthentication no\n ForwardAgent no\n ConnectTimeout 2\n LogLevel ERROR\n')
    (work/'bin').mkdir()
    (work/'bin/ssh').write_text('#!/bin/sh\nexec /usr/bin/ssh -F ' + shlex.quote(str(work/'ssh-config')) + ' "$@"\n')
    (work/'bin/ssh').chmod(0o700)
    (work/'docker').mkdir()
    (work/'docker/config.json').write_text('{"auths":{}}\n')
    (work/'user-data').write_text(cloud_configuration(repo=repo, cache=lease.cache,
        scratch=lease.scratch, readonly=not lease.install))
    (work/'meta-data').write_text('instance-id: coordinare-' + lease.owner_id + '\n')
    await _command(['genisoimage', '-quiet', '-output', str(work/'seed.iso'), '-volid', 'cidata', '-joliet', '-rock',
                    str(work/'user-data'), str(work/'meta-data')], deadline=lease.deadline)
    return qemu_arguments(lease.spec, lease.bundle, disk=work/'working.qcow2', seed=work/'seed.iso',
                          repo=repo, cache=lease.cache, scratch=lease.scratch, control=guest_control, install=lease.install)


async def open_runtime(spec: RuntimeSpec, *, artifacts: RuntimeArtifactReceipt | None,
                       repo: Path, cache: Path, scratch: Path, install: bool, deadline: float,
                       manifest_digest: str | None = None, source_digest: str | None = None,
                       purpose: Literal['installer', 'consumer', 'preflight'] | None = None) -> RuntimeLease:
    """Create only an owned guest after all source-independent admission checks."""
    deadline = min(deadline, time.monotonic()+spec.policy.resources.max_seconds)
    _remaining(deadline)
    purpose = purpose or ('installer' if install else 'consumer')
    if (purpose == 'installer') != install or (purpose == 'preflight' and artifacts is not None):
        raise ValueError('private runtime purpose differs from artifact policy')
    receipt = _verify_receipt(spec, artifacts, manifest_digest, source_digest) if purpose == 'consumer' else None
    bundle = await run_io(partial(_admit, spec, repo=repo, cache=cache, scratch=scratch, readonly=not install),
                          deadline=deadline)
    initial = {Path(bundle.guest_image): spec.policy.identity.guest_base_sha256,
               Path(bundle.emulator).resolve(): spec.policy.identity.emulator_sha256,
               Path(bundle.firmware).resolve(): spec.policy.identity.firmware_sha256}
    if receipt is not None:
        artifact = receipt.artifacts[0]
        path = cache/artifact.path
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != artifact.bytes
                or await run_io(partial(_digest, path), deadline=deadline) != artifact.sha256):
            raise ValueError('private runtime artifact identity differs')
        initial[path] = artifact.sha256
    work = Path(tempfile.mkdtemp(prefix='coordinare-runtime-', dir=scratch.parent))
    lease = RuntimeLease(spec, bundle, uuid.uuid4().hex, work, cache, scratch,
                         deadline, install,
                         manifest_digest, source_digest, {}, initial_artifacts=initial)
    lease.activation_env = {'DOCKER_HOST': f'ssh://root@127.0.0.1:{spec.policy.control_port}',
                            'DOCKER_CONFIG': str(work/'docker'), 'DOCKER_CONTEXT': '',
                            'COORDINARE_ENV_RUNTIME_BIN': str(work/'bin'),
                            'DOCKER_TLS_VERIFY': '', 'DOCKER_CERT_PATH': '', 'SSH_AUTH_SOCK': '',
                            'PATH': str(work/'bin') + os.pathsep + os.environ.get('PATH', '')}
    try:
        await run_io(lease.check_bounds, deadline=lease.deadline)
        base = cache/'installed.qcow2' if receipt is not None else Path(bundle.guest_image)
        _, raw_info = await _command(['/usr/bin/qemu-img', 'info', '--output=json', str(base)], deadline=lease.deadline)
        info = json.loads(raw_info)
        if (info.get('format') != 'qcow2' or info.get('backing-filename')
                or info.get('virtual-size', 0) > spec.policy.resources.cache_bytes):
            raise ValueError('private runtime disk layout exceeds admitted resources')
        await _command(['qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'qcow2', '-b', str(base),
                        str(work/'working.qcow2'), str(spec.policy.resources.cache_bytes)], deadline=lease.deadline)
        arguments = await _prepare_control(lease, repo=repo)
        with (work/'serial.log').open('wb') as serial:
            lease.process = await asyncio.create_subprocess_exec(*arguments, stdout=serial,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        while True:
            await run_io(lease.check_bounds, deadline=lease.deadline)
            try:
                code, _ = await lease.ssh('test -f /run/coordinare-runtime-ready && docker info >/dev/null',
                                          deadline=min(lease.deadline, time.monotonic()+5))
                if code == 0:
                    return lease
            except (RuntimeError, TimeoutError):
                if time.monotonic() >= lease.deadline:
                    raise
            await asyncio.sleep(min(1, _remaining(lease.deadline)))
    except BaseException:
        await lease.close(deadline=lease.deadline)
        raise


async def seal_runtime(lease: RuntimeLease, *, manifest_digest: str, source_digest: str,
                       deadline: float) -> RuntimeArtifactReceipt:
    """Publish closed project runtime state, never credentials or live service state."""
    if not lease.install or not lease.installation_passed or (manifest_digest, source_digest) != (lease.manifest_digest, lease.source_digest):
        raise ValueError('private runtime installer identity differs')
    deadline = min(deadline, lease.deadline)
    await run_io(lease.check_bounds, deadline=deadline)
    await lease.stop_guest(deadline=deadline)
    if not await run_io(lease._artifacts_unchanged, deadline=deadline):
        raise ValueError('private runtime platform artifacts changed during installation')
    target = lease.cache/'installed.qcow2'
    if target.exists() or target.is_symlink():
        raise ValueError('private runtime artifact publication is not fresh')
    try:
        await _command(['qemu-img', 'convert', '-O', 'qcow2', str(lease.work_root/'working.qcow2'), str(target)],
                        deadline=min(deadline, lease.deadline))
        await run_io(lease.check_bounds, deadline=deadline)
        digest = await run_io(partial(_digest, target), deadline=deadline)
        _remaining(min(deadline, lease.deadline))
        return RuntimeArtifactReceipt.model_validate({
            'schema_version': 1, 'manifest_digest': manifest_digest, 'source_digest': source_digest,
            'runtime_digest': lease.spec.digest(), 'sealed': True,
            'artifacts': [{'path': target.name, 'sha256': digest, 'bytes': target.stat().st_size}],
        })
    except BaseException:
        target.unlink(missing_ok=True)
        raise
