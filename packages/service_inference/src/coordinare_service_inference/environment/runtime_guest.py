"""Boot plumbing for the explicitly configured private-service platform adapter."""
from __future__ import annotations

import json
import os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .runtime import RuntimeResources, RuntimeSpec
    from .runtime_bundle import RuntimeBundleInputs


INTERPRETER_DIR = '/usr/local/lib/coordinare'
# Each emulated platform's guest keeps the static user-mode interpreter of the other CPU, prepared with the guest image.
_ELF_HEADER = bytes([0x7F, 0x45, 0x4C, 0x46, 2, 1, 1, 0]) + bytes(8) + bytes([2, 0])
_ELF_MASK = bytes([0xFF] * 7 + [0xFC] + [0xFF] * 8 + [0xFE, 0xFF, 0xFF, 0xFF])
_ELF_MACHINE = {'x86_64': bytes([0x3E, 0x00]), 'aarch64': bytes([0xB7, 0x00])}
FOREIGN_PLATFORMS = {'linux/aarch64': ('linux/x86_64',), 'linux/x86_64': ('linux/aarch64',)}


def foreign_platforms(guest_platform: str) -> tuple[str, ...]:
    """Platforms a guest runs through user-mode emulation; an install or check never implies one of these."""
    return FOREIGN_PLATFORMS.get(guest_platform, ())


def binfmt_entry(guest_platform: str) -> str | None:
    """The kernel registration for the guest's foreign interpreter; the F flag holds it open, so no container path matters."""
    foreign = foreign_platforms(guest_platform)
    if not foreign:
        return None
    arch = foreign[0].rsplit('/', 1)[-1]
    magic = _ELF_HEADER + _ELF_MACHINE[arch]
    escape = ''.join(f'\\x{byte:02x}' for byte in magic)
    mask = ''.join(f'\\x{byte:02x}' for byte in _ELF_MASK)
    return f':qemu-{arch}:M::{escape}:{mask}:{INTERPRETER_DIR}/qemu-{arch}-static:OCF'


def cloud_configuration(*, repo: Path, cache: Path, scratch: Path, readonly: bool, guest_platform: str | None = None) -> str:
    mounts = []
    for tag, path, ro in [('repo', str(repo), False), ('cache', str(cache), readonly),
                          ('scratch', str(scratch), False), ('control', '/run/coordinare-runtime-control', True)]:
        quoted = shlex.quote(path)
        mounts.append(f'mkdir -p {quoted}; mount -t 9p -o trans=virtio,version=9p2000.L' + (',ro' if ro else '') + f' {tag} {quoted}')
    script = '\n'.join([
        '#!/bin/sh', 'set -eu', 'exec >>/var/log/coordinare-runtime.log 2>&1',
        'mount -t tmpfs -o mode=1777,size=512m tmpfs /tmp', 'modprobe 9pnet_virtio', *mounts,
        'rc-service cgroups start', *_binfmt(guest_platform), 'rc-service docker start', 'rc-service sshd stop || true',
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


def _binfmt(guest_platform: str | None) -> list[str]:
    entry = binfmt_entry(guest_platform) if guest_platform is not None else None
    if entry is None:
        return []
    # Registration is best effort: a kernel without binfmt_misc leaves the native platform usable and the foreign one absent.
    return ['modprobe binfmt_misc 2>/dev/null || true', 'mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null || true',
            f'printf %s {shlex.quote(entry)} > /proc/sys/fs/binfmt_misc/register 2>/dev/null || echo coordinare: foreign platform emulation unavailable']


# The guest's RAM is unreclaimable QEMU memory inside the container's memory limit, and the guest fills it with file
# cache during a long install. It gets half the limit, so QEMU's own overhead (its translation cache, capped here) and
# the container-side install (compilers, gem and npm builds) always keep the other half. v30 and v32 gave the guest all
# but 512 MiB, and the container's limit killed QEMU partway through installation.
TCG_CACHE_MIB = 256


def guest_memory_mib(resources: RuntimeResources) -> int:
    return max(256, resources.memory_mib // 2)


def guest_cpus(resources: RuntimeResources) -> int:
    """Half the CPU quota: native install commands share the container with the emulated guest, which would
    otherwise take every CPU while it emulates image builds and starve them."""
    return max(1, resources.cpus // 2)


def host_kvm_usable(device: str = '/dev/kvm') -> bool:
    return os.access(device, os.R_OK | os.W_OK)


def accelerator_arguments(*, kvm: bool) -> list[str]:
    """QEMU tries -accel options in order, so KVM is attempted first and TCG remains the fallback."""
    tcg = ['-accel', f'tcg,tb-size={TCG_CACHE_MIB}']
    return ['-accel', 'kvm', *tcg] if kvm else tcg


def qemu_arguments(spec: RuntimeSpec, bundle: RuntimeBundleInputs, *, disk: Path, seed: Path,
                   repo: Path, cache: Path, scratch: Path, control: Path, install: bool,
                   carrier_platform: str | None = None, kvm_usable: bool = False) -> list[str]:
    forwarding = [f'hostfwd=tcp:127.0.0.1:{spec.policy.control_port}-:22']
    forwarding.extend(f'hostfwd=tcp:127.0.0.1:{port.outer_port}-:{port.guest_port}' for port in spec.endpoints)
    # KVM only helps a guest of the carrier's own architecture; a cross-architecture guest stays on TCG.
    kvm = kvm_usable and carrier_platform == bundle.guest_platform
    arguments = [bundle.emulator, *accelerator_arguments(kvm=kvm), '-m', str(guest_memory_mib(spec.policy.resources)),
                 '-smp', str(guest_cpus(spec.policy.resources)), '-bios', bundle.firmware,
                 '-drive', f'file={disk},format=qcow2,if=virtio,discard=unmap',
                 '-drive', f'file={seed},format=raw,media=cdrom,if=virtio,readonly=on',
                 '-netdev', 'user,id=net0,restrict=' + ('off' if install else 'on') + ',' + ','.join(forwarding),
                 '-device', 'virtio-net-pci,netdev=net0,romfile=', '-nographic', '-no-reboot']
    if bundle.guest_platform == 'linux/aarch64':
        # KVM cannot emulate a different CPU model, so a same-architecture guest runs the host's own.
        arguments += ['-M', 'virt', '-cpu', 'host' if kvm else 'cortex-a72']
    for tag, path, readonly in [('repo', repo, False), ('cache', cache, not install),
                                 ('scratch', scratch, False), ('control', control, True)]:
        # QEMU's comma-delimited option syntax escapes literal commas by doubling.
        arguments += ['-virtfs', 'local,path=' + str(path).replace(',', ',,')
                      + f',mount_tag={tag},security_model=none' + (',readonly=on' if readonly else '')]
    return arguments
