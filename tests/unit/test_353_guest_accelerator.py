"""KVM is offered only to a same-architecture guest; everything else keeps the TCG argv byte for byte.

The carrier's facts (architecture, /dev/kvm) are injected, so no QEMU runs here.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinare_service_inference.environment.runtime_guest import (
    TCG_CACHE_MIB,
    host_kvm_usable,
    qemu_arguments,
)

TCG = ['-accel', f'tcg,tb-size={TCG_CACHE_MIB}']


def _arguments(*, guest='linux/x86_64', carrier=None, kvm=False):
    resources = SimpleNamespace(memory_mib=4096, cpus=2)
    spec = SimpleNamespace(policy=SimpleNamespace(control_port=2222, resources=resources), endpoints=[])
    bundle = SimpleNamespace(emulator='/usr/bin/qemu-system-x86_64', firmware='/fw', guest_platform=guest)
    paths = {name: Path('/' + name) for name in ('disk', 'seed', 'repo', 'cache', 'scratch', 'control')}
    return qemu_arguments(spec, bundle, install=True, carrier_platform=carrier, kvm_usable=kvm, **paths)


def _pairs(arguments):
    return [arguments[index + 1] for index, value in enumerate(arguments) if value == '-accel']


def test_tcg_argv_is_unchanged_when_no_carrier_facts_are_supplied():
    arguments = _arguments()
    assert _pairs(arguments) == [f'tcg,tb-size={TCG_CACHE_MIB}']
    assert arguments[:3] == ['/usr/bin/qemu-system-x86_64', '-accel', f'tcg,tb-size={TCG_CACHE_MIB}']
    assert '-cpu' not in arguments and '-M' not in arguments


def test_kvm_is_tried_first_with_tcg_as_fallback_for_a_same_architecture_guest():
    arguments = _arguments(guest='linux/x86_64', carrier='linux/x86_64', kvm=True)
    assert _pairs(arguments) == ['kvm', f'tcg,tb-size={TCG_CACHE_MIB}']
    assert arguments[:5] == ['/usr/bin/qemu-system-x86_64', '-accel', 'kvm', '-accel', f'tcg,tb-size={TCG_CACHE_MIB}']


def test_kvm_is_not_used_when_the_guest_architecture_differs_from_the_carrier():
    arguments = _arguments(guest='linux/x86_64', carrier='linux/aarch64', kvm=True)
    assert _pairs(arguments) == [f'tcg,tb-size={TCG_CACHE_MIB}']


def test_kvm_is_not_used_without_the_device():
    assert _pairs(_arguments(guest='linux/x86_64', carrier='linux/x86_64', kvm=False)) == [f'tcg,tb-size={TCG_CACHE_MIB}']


def test_kvm_is_not_used_when_the_carrier_is_unknown():
    assert _pairs(_arguments(guest='linux/x86_64', carrier=None, kvm=True)) == [f'tcg,tb-size={TCG_CACHE_MIB}']


def test_aarch64_kvm_uses_the_host_cpu_while_tcg_keeps_the_emulated_model():
    kvm = _arguments(guest='linux/aarch64', carrier='linux/aarch64', kvm=True)
    assert _pairs(kvm)[0] == 'kvm'
    assert kvm[kvm.index('-cpu') + 1] == 'host'
    emulated = _arguments(guest='linux/aarch64', carrier='linux/x86_64', kvm=True)
    assert _pairs(emulated) == [f'tcg,tb-size={TCG_CACHE_MIB}']
    assert emulated[emulated.index('-cpu') + 1] == 'cortex-a72'


def test_kvm_probe_requires_a_readable_and_writable_device(tmp_path):
    assert not host_kvm_usable(str(tmp_path / 'missing'))
    device = tmp_path / 'kvm'
    device.write_text('')
    assert host_kvm_usable(str(device))
