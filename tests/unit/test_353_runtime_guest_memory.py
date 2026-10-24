"""The guest gets half the container's memory, so QEMU and the container-side install keep the other half.

v30 and v32 gave the guest all but 512 MiB of the container's 4 GiB limit. The guest filled its RAM with file
cache during installation, QEMU's own memory and the container-side build grew, and the limit killed QEMU (-9).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runtime_guest import (
    TCG_CACHE_MIB,
    guest_memory_mib,
    qemu_arguments,
)


@pytest.mark.parametrize(('limit', 'guest'), [(4096, 2048), (8192, 4096), (512, 256)])
def test_the_guest_gets_half_the_container_memory(limit, guest):
    assert guest_memory_mib(SimpleNamespace(memory_mib=limit)) == guest


def test_qemu_is_started_with_the_halved_memory_and_a_bounded_translation_cache():
    resources = SimpleNamespace(memory_mib=4096, cpus=2)
    spec = SimpleNamespace(policy=SimpleNamespace(control_port=2222, resources=resources), endpoints=[])
    bundle = SimpleNamespace(emulator='/usr/bin/qemu-system-x86_64', firmware='/fw', guest_platform='linux/amd64')
    paths = {name: Path('/' + name) for name in ('disk', 'seed', 'repo', 'cache', 'scratch', 'control')}
    arguments = qemu_arguments(spec, bundle, install=True, **paths)
    assert arguments[arguments.index('-m') + 1] == '2048'
    assert arguments[arguments.index('-accel') + 1] == f'tcg,tb-size={TCG_CACHE_MIB}'
    assert TCG_CACHE_MIB <= 256
