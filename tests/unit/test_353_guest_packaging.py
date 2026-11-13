"""Both platform guests are prepared in every carrier, each under its own directory with its own bundle."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from coordinare_service_inference.environment.runtime_bundle import bundle_path

ROOT = Path(__file__).resolve().parents[2]


def test_each_guest_is_prepared_into_its_own_directory_with_its_own_pinned_image():
    text = (ROOT / 'agent/performer/runtime/Dockerfile').read_text()
    for arch in ('x86_64', 'aarch64'):
        assert f'--arch {arch} --url "$PLATFORM_GUEST_URL_{arch.upper()}" --sha512 "$PLATFORM_GUEST_SHA512_{arch.upper()}"' in text
        assert f'--root /opt/coordinare-runtime/{arch}' in text


def test_the_carrier_installs_both_system_emulators_and_both_interpreters():
    text = (ROOT / 'agent/performer/runtime/Dockerfile').read_text()
    for package in ('qemu-system-x86', 'qemu-system-arm', 'qemu-efi-aarch64', 'qemu-user-static'):
        assert package in text


def test_the_prepared_guest_directory_is_created_for_each_architecture():
    spec = importlib.util.spec_from_file_location('prepare_guest', ROOT / 'agent/performer/runtime/prepare_guest.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.FOREIGN == {'x86_64': 'aarch64', 'aarch64': 'x86_64'}


@pytest.mark.parametrize(('platform', 'path'), [
    ('linux/aarch64', '/opt/coordinare-runtime/aarch64/bundle.json'),
    ('linux/x86_64', '/opt/coordinare-runtime/x86_64/bundle.json'),
])
def test_each_guest_reads_its_own_bundle(platform, path):
    assert str(bundle_path(platform)) == path


@pytest.mark.parametrize('platform', ['linux/riscv64', '../x86_64', 'x86_64', ''])
def test_an_unsupported_guest_platform_has_no_bundle(platform):
    with pytest.raises(ValueError):
        bundle_path(platform)
