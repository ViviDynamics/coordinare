"""One policy carries the carrier's guest and, optionally, the other CPU's; a selected policy holds exactly one."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.runtime import RuntimeIdentity, RuntimePolicy

IMAGE = 'trusted/carrier@sha256:' + 'c' * 64


def identity(guest: str, digit: str = 'a') -> RuntimeIdentity:
    return RuntimeIdentity(schema_version=1, carrier_image=IMAGE, carrier_platform='linux/aarch64',
        guest_base_sha256=digit * 64, guest_platform=guest, emulator_sha256='e' * 64, firmware_sha256='f' * 64,
        configuration_sha256='9' * 64)


def policy(**changes) -> RuntimePolicy:
    resources = {'cpus': 8, 'memory_mib': 4096, 'cache_bytes': 1, 'scratch_bytes': 1, 'reserve_bytes': 1, 'max_seconds': 60}
    return RuntimePolicy(schema_version=1, kind='private_service_guest', identity=identity('linux/aarch64'),
        resources=resources, control_port=2222, max_endpoints=0, **changes)


def test_a_policy_without_an_alternate_serves_only_its_own_guest():
    base = policy()
    assert base.for_guest('linux/aarch64').identity == base.identity
    with pytest.raises(ValueError, match='no identity'):
        base.for_guest('linux/x86_64')


def test_the_selected_policy_carries_exactly_the_requested_guest():
    base = policy(alternate_identity=identity('linux/x86_64', 'b'))
    native = base.for_guest('linux/aarch64')
    foreign = base.for_guest('linux/x86_64')
    assert native.identity.guest_platform == 'linux/aarch64' and native.alternate_identity is None
    assert foreign.identity == base.alternate_identity and foreign.alternate_identity is None
    assert native.resources == foreign.resources == base.resources


def test_the_alternate_must_be_the_other_cpu_of_the_same_carrier():
    with pytest.raises(ValueError, match='other CPU'):
        policy(alternate_identity=identity('linux/aarch64', 'b'))
    other_carrier = identity('linux/x86_64', 'b').model_copy(update={'carrier_image': 'other@sha256:' + 'd' * 64})
    with pytest.raises(ValueError, match='other CPU'):
        policy(alternate_identity=other_carrier)


def test_a_native_selection_does_not_depend_on_the_alternate_being_configured():
    with_alternate = policy(alternate_identity=identity('linux/x86_64', 'b'))
    assert with_alternate.for_guest('linux/aarch64').digest() == policy().for_guest('linux/aarch64').digest()
