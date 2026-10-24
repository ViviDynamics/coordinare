"""The guest matches the carrier's CPU; the other CPU runs only through registered user-mode emulation.

The carrier's facts are injected and no QEMU, Docker or network is used here.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runtime_guest import (
    binfmt_entry,
    cloud_configuration,
    foreign_platforms,
    qemu_arguments,
)

ROOT = Path(__file__).resolve().parents[2]
PREPARE = ROOT / 'agent/performer/runtime/prepare_guest.py'
DOCKERFILE = ROOT / 'agent/performer/runtime/Dockerfile'


def _prepare():
    spec = importlib.util.spec_from_file_location('prepare_guest', PREPARE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments(guest: str):
    resources = SimpleNamespace(memory_mib=4096, cpus=2)
    spec = SimpleNamespace(policy=SimpleNamespace(control_port=2222, resources=resources), endpoints=[])
    bundle = SimpleNamespace(emulator='/usr/bin/qemu-system-aarch64', firmware='/usr/share/qemu-efi-aarch64/QEMU_EFI.fd',
                             guest_platform=guest)
    paths = {name: Path('/' + name) for name in ('disk', 'seed', 'repo', 'cache', 'scratch', 'control')}
    return qemu_arguments(spec, bundle, install=True, carrier_platform='linux/aarch64', kvm_usable=False, **paths)


def test_a_native_arm_guest_boots_the_arm_machine_with_its_uefi_firmware():
    arguments = _arguments('linux/aarch64')
    assert arguments[arguments.index('-M') + 1] == 'virt'
    assert arguments[arguments.index('-bios') + 1] == '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd'
    assert arguments[arguments.index('-cpu') + 1] == 'cortex-a72'
    assert arguments[arguments.index('-accel') + 1].startswith('tcg,')


def test_the_prepared_guest_and_the_runtime_agree_on_each_architecture_and_firmware():
    prepare = _prepare()
    for arch, platform in [('x86_64', 'linux/x86_64'), ('aarch64', 'linux/aarch64')]:
        profile = prepare.GUESTS[arch]
        assert profile['platform'] == platform
        assert profile['emulator'] == f'/usr/bin/qemu-system-{arch}'
    assert prepare.GUESTS['aarch64']['firmware'] == '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd'
    assert prepare.GUESTS['aarch64']['machine'] == ['-M', 'virt', '-cpu', 'cortex-a72']
    assert prepare.GUESTS['x86_64']['firmware'] == '/usr/share/seabios/bios-256k.bin'


def test_the_arm_guest_does_not_carry_an_x86_system_emulator():
    prepare = _prepare()
    arm = prepare.platform_script('aarch64', 'qemu-x86_64-static')
    assert 'qemu-system-x86_64' not in arm and 'seabios' not in arm
    assert 'qemu-system-x86_64 seabios' in prepare.platform_script('x86_64', 'qemu-aarch64-static')


def test_the_prepared_guest_keeps_the_other_architecture_as_a_static_interpreter():
    prepare = _prepare()
    assert prepare.FOREIGN == {'x86_64': 'aarch64', 'aarch64': 'x86_64'}
    script = prepare.platform_script('aarch64', 'qemu-x86_64-static')
    assert 'install -m 0755 /mnt/payload/qemu-x86_64-static /usr/local/lib/coordinare/qemu-x86_64-static' in script
    assert 'test -x /usr/local/lib/coordinare/qemu-x86_64-static' in script


def test_foreign_platforms_are_advertised_only_for_the_emulated_architecture():
    assert foreign_platforms('linux/aarch64') == ('linux/x86_64',)
    assert foreign_platforms('linux/x86_64') == ('linux/aarch64',)
    assert foreign_platforms('linux/riscv64') == ()


def test_binfmt_registers_the_static_interpreter_by_its_elf_machine():
    entry = binfmt_entry('linux/aarch64')
    header = '\\x7f\\x45\\x4c\\x46\\x02\\x01\\x01\\x00' + '\\x00' * 8 + '\\x02\\x00'
    assert entry.startswith(f':qemu-x86_64:M::{header}\\x3e\\x00:')
    assert entry.endswith(':/usr/local/lib/coordinare/qemu-x86_64-static:OCF')
    assert binfmt_entry('linux/x86_64').startswith(f':qemu-aarch64:M::{header}\\xb7\\x00:')
    assert binfmt_entry('linux/riscv64') is None


def test_binfmt_mask_has_the_magic_length_and_masks_only_the_variable_header_bytes():
    fields = binfmt_entry('linux/aarch64').split(':')
    magic = re.findall(r'\\x([0-9a-f]{2})', fields[4])
    mask = re.findall(r'\\x([0-9a-f]{2})', fields[5])
    assert len(magic) == len(mask) == 20
    assert mask[7] == 'fc' and mask[16] == 'fe' and mask[18:] == ['ff', 'ff']


def _script(guest: str | None) -> str:
    config = cloud_configuration(repo=Path('/r'), cache=Path('/c'), scratch=Path('/s'), readonly=False, guest_platform=guest)
    return config.split('"runcmd": ["', 1)[1]


def test_a_native_guest_registers_the_foreign_interpreter_before_docker_starts():
    script = _script('linux/aarch64')
    assert 'binfmt_misc' in script
    assert script.index('binfmt_misc/register') < script.index('rc-service docker start')
    assert 'qemu-x86_64-static' in script


def test_a_guest_without_a_foreign_interpreter_registers_nothing():
    assert 'binfmt_misc/register' not in _script(None)


def test_dockerfile_selects_the_native_guest_and_the_pinned_alpine_images():
    text = DOCKERFILE.read_text()
    assert 'qemu-efi-aarch64' in text and 'qemu-system-x86' in text and 'qemu-user-static' in text
    assert 'uname -m' in text and 'ARG PLATFORM_GUEST_SHA512_AARCH64' in text
    assert 'd13783e02020662cdd8e88359654881a178948a851207baa94dd57b3c69dd1fd6f1a3dacec2165ec557ff2eb7451caaf0f15f5efca561abe24473b26169c4c41' in text
