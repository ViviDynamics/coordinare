"""Discovery authors see where work runs and which foreign platforms are emulated, and the rule for choosing one."""
from __future__ import annotations

from types import SimpleNamespace

from coordinare.services.environment_capabilities import platform_roles
from coordinare.services.environment_discovery_prompts import _MANIFEST_INSTRUCTIONS


def _capabilities(runtime=None):
    return SimpleNamespace(platform='linux/aarch64', runtime=runtime)


def test_a_native_guest_is_the_container_daemon_and_the_other_cpu_is_emulated():
    guest = SimpleNamespace(identity=SimpleNamespace(guest_platform='linux/aarch64'))
    assert platform_roles(_capabilities(guest)) == {
        'install_and_check_platform': 'linux/aarch64', 'container_daemon_platform': 'linux/aarch64',
        'emulated_platforms': ['linux/x86_64']}


def test_without_a_private_runtime_nothing_is_advertised_as_emulated():
    assert platform_roles(_capabilities()) == {
        'install_and_check_platform': 'linux/aarch64', 'container_daemon_platform': 'linux/aarch64', 'emulated_platforms': []}


def test_manifest_instructions_state_the_architecture_rule():
    assert 'platform_roles.install_and_check_platform' in _MANIFEST_INSTRUCTIONS
    assert 'explicitly requires' in _MANIFEST_INSTRUCTIONS
