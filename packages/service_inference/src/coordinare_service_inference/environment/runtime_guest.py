"""Boot plumbing for the explicitly configured private-service platform adapter."""
from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .runtime import RuntimeResources, RuntimeSpec
    from .runtime_bundle import RuntimeBundleInputs


def cloud_configuration(*, repo: Path, cache: Path, scratch: Path, readonly: bool) -> str:
    mounts = []
    for tag, path, ro in [('repo', str(repo), False), ('cache', str(cache), readonly),
                          ('scratch', str(scratch), False), ('control', '/run/coordinare-runtime-control', True)]:
        quoted = shlex.quote(path)
        mounts.append(f'mkdir -p {quoted}; mount -t 9p -o trans=virtio,version=9p2000.L' + (',ro' if ro else '') + f' {tag} {quoted}')
    script = '\n'.join([
        '#!/bin/sh', 'set -eu', 'exec >>/var/log/coordinare-runtime.log 2>&1',
        'mount -t tmpfs -o mode=1777,size=512m tmpfs /tmp', 'modprobe 9pnet_virtio', *mounts,
        'rc-service cgroups start', 'rc-service docker start', 'rc-service sshd stop || true',
        'passwd -d root', 'mkdir -p /run/sshd /run/coordinare-runtime-ssh',
        'chmod 700 /run/coordinare-runtime-ssh',
        'cp /run/coordinare-runtime-control/host-key /run/coordinare-runtime-ssh/host-key',
        'cp /run/coordinare-runtime-control/authorized_keys /run/coordinare-runtime-ssh/authorized_keys',
        'chmod 600 /run/coordinare-runtime-ssh/*',
        '/usr/sbin/sshd -h /run/coordinare-runtime-ssh/host-key -o PermitRootLogin=yes -o PasswordAuthentication=no -o AuthorizedKeysFile=/run/coordinare-runtime-ssh/authorized_keys',
        'for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done',
        'docker info >/dev/null', 'touch /run/coordinare-runtime-ready',
    ])
    return '#cloud-config\n' + json.dumps({'users': [], 'ssh_pwauth': False, 'disable_root': False, 'ssh_genkeytypes': [],
                                          'runcmd': [script]}) + '\n'


# The guest's RAM is unreclaimable QEMU memory inside the container's memory limit, and the guest fills it with file
# cache during a long install. It gets half the limit, so QEMU's own overhead (its translation cache, capped here) and
# the container-side install (compilers, gem and npm builds) always keep the other half. v30 and v32 gave the guest all
# but 512 MiB, and the container's limit killed QEMU partway through installation.
TCG_CACHE_MIB = 256


def guest_memory_mib(resources: RuntimeResources) -> int:
    return max(256, resources.memory_mib // 2)


def qemu_arguments(spec: RuntimeSpec, bundle: RuntimeBundleInputs, *, disk: Path, seed: Path,
                   repo: Path, cache: Path, scratch: Path, control: Path, install: bool) -> list[str]:
    forwarding = [f'hostfwd=tcp:127.0.0.1:{spec.policy.control_port}-:22']
    forwarding.extend(f'hostfwd=tcp:127.0.0.1:{port.outer_port}-:{port.guest_port}' for port in spec.endpoints)
    arguments = [bundle.emulator, '-accel', f'tcg,tb-size={TCG_CACHE_MIB}', '-m', str(guest_memory_mib(spec.policy.resources)),
                 '-smp', str(spec.policy.resources.cpus), '-bios', bundle.firmware,
                 '-drive', f'file={disk},format=qcow2,if=virtio,discard=unmap',
                 '-drive', f'file={seed},format=raw,media=cdrom,if=virtio,readonly=on',
                 '-netdev', 'user,id=net0,restrict=' + ('off' if install else 'on') + ',' + ','.join(forwarding),
                 '-device', 'virtio-net-pci,netdev=net0,romfile=', '-nographic', '-no-reboot']
    if bundle.guest_platform == 'linux/aarch64':
        arguments += ['-M', 'virt', '-cpu', 'cortex-a72']
    for tag, path, readonly in [('repo', repo, False), ('cache', cache, not install),
                                 ('scratch', scratch, False), ('control', control, True)]:
        # QEMU's comma-delimited option syntax escapes literal commas by doubling.
        arguments += ['-virtfs', 'local,path=' + str(path).replace(',', ',,')
                      + f',mount_tag={tag},security_model=none' + (',readonly=on' if readonly else '')]
    return arguments
