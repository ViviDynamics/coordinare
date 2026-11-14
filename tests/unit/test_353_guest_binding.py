"""The pinned source's recorded requirement picks the guest in capabilities, so every later step reuses it."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.runtime import RuntimeIdentity

from coordinare.models.env_cache import EnvCacheState
from coordinare.services import environment_capabilities as caps

IMAGE = 'trusted/carrier@sha256:' + 'c' * 64


def identity(guest: str, digit: str) -> RuntimeIdentity:
    return RuntimeIdentity(schema_version=1, carrier_image=IMAGE, carrier_platform='linux/aarch64',
        guest_base_sha256=digit * 64, guest_platform=guest, emulator_sha256='e' * 64, firmware_sha256='f' * 64,
        configuration_sha256=digit * 64)


def runtime(alternate: bool = True) -> dict:
    native, foreign = identity('linux/aarch64', 'a'), identity('linux/x86_64', 'b')
    data = {'schema_version': 1, 'kind': 'private_service_guest', 'identity': native.model_dump(),
            'resources': {'cpus': 8, 'memory_mib': 4096, 'cache_bytes': 1, 'scratch_bytes': 1, 'reserve_bytes': 1, 'max_seconds': 60},
            'control_port': 2222, 'max_endpoints': 0}
    if alternate:
        data['alternate_identity'] = foreign.model_dump()
    return data


@pytest.fixture
def inspected(monkeypatch):
    calls: list[str] = []

    async def fake_inspect(image, guest_platform):
        calls.append(guest_platform)
        digit = 'a' if guest_platform == 'linux/aarch64' else 'b'
        return identity(guest_platform, digit).model_dump(exclude={'schema_version', 'carrier_image', 'carrier_platform'})

    async def fake_identity(image):
        return IMAGE, 'linux/aarch64'

    async def no_bootstrap(state, symphony, target):
        return None

    monkeypatch.setattr(caps, '_inspect_runtime_bundle', fake_inspect)
    monkeypatch.setattr(caps, '_docker_identity', fake_identity)
    monkeypatch.setattr(caps, '_check_bootstrap_target', no_bootstrap)
    return calls


def state(required, *, alternate=True):
    cache = EnvCacheState(symphony_name='sym', sanitised_name='sym', cache_dir='/tmp/cache', bootstrap_commit_sha='a' * 40,
        environment_guest=None if required is None else {'required': required, 'triggers': [], 'unsupported': []})
    config = SimpleNamespace(agent_transport='docker', performer_image=IMAGE, performer_endpoints=[],
                             env_cache=SimpleNamespace(private_service_runtime=runtime(alternate)))
    return {'config': config, 'env_cache': {'sym': cache}}


@pytest.mark.asyncio
async def test_without_a_requirement_the_carrier_guest_is_bound(inspected):
    result = await caps.resolve_execution_capabilities(state(None), 'sym')
    assert result.runtime.identity.guest_platform == 'linux/aarch64' and result.runtime.alternate_identity is None
    assert inspected == ['linux/aarch64']


@pytest.mark.asyncio
async def test_an_explicit_foreign_requirement_binds_the_other_guest(inspected):
    result = await caps.resolve_execution_capabilities(state(['linux/x86_64']), 'sym')
    assert result.runtime.identity.guest_platform == 'linux/x86_64'
    assert inspected == ['linux/x86_64']


@pytest.mark.asyncio
async def test_the_selected_guest_changes_the_capability_digest(inspected):
    native = await caps.resolve_execution_capabilities(state(None), 'sym')
    foreign = await caps.resolve_execution_capabilities(state(['linux/x86_64']), 'sym')
    assert native.digest() != foreign.digest()


@pytest.mark.asyncio
async def test_a_foreign_requirement_without_a_configured_guest_is_refused(inspected):
    with pytest.raises(ValueError, match='no configured guest'):
        await caps.resolve_execution_capabilities(state(['linux/x86_64'], alternate=False), 'sym')
    assert inspected == []
