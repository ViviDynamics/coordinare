"""An inner step timeout is not reported as the attempt's own deadline."""
from __future__ import annotations

import asyncio

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_bootstrap_pipeline import Proposal, Repository, setup
from tests.unit.test_353_scope_contract import Replies, criterion, proposal, run


async def held_code(tmp_path, monkeypatch, failure):
    from tests.unit.scope_chat_fixture import ScopeChatFixture
    service, cache, _, capability = setup(tmp_path)
    monkeypatch.setattr('coordinare.services.environment_capabilities.resolve_execution_capabilities', failure)
    try:
        await service._build_manifest_artifacts(performer_mounts=(cache.cache_dir.parent,), symphony_name='suite',
            repo='suite', github_org='org', github_service=Repository(), readme_contents={}, current_sha='fingerprint',
            cache_dir=cache.cache_dir, cache_mount_path=capability.cache_mount_path,
            llm_chat=ScopeChatFixture(Proposal(), 'guide'), cache_state=cache, capabilities=None)
    except EnvironmentDiscoveryError as exc:
        return exc.code
    return cache.environment_attempt.get('last_error')


@pytest.mark.asyncio
async def test_capability_step_timeout_is_labelled_as_a_step_timeout(tmp_path, monkeypatch):
    async def inner_timeout(*args, **kwargs):
        raise TimeoutError
    assert await held_code(tmp_path, monkeypatch, inner_timeout) == 'step_timeout'


@pytest.mark.asyncio
async def test_scope_step_timeout_is_labelled_as_a_step_timeout():
    async def fetch(path):
        raise TimeoutError
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion())]), fetch=fetch)
    assert failure.value.code in {'step_timeout', 'scope_phase_deadline'}
    assert failure.value.code != 'attempt_deadline'


@pytest.mark.asyncio
async def test_real_scope_attempt_deadline_keeps_its_label():
    async def fetch(path):
        await asyncio.sleep(1)
        return 'source'
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(Replies([{'tool': 'read_file', 'path': 'guide'}]), budget=AttemptBudget(timeout_seconds=0.05), fetch=fetch)
    assert failure.value.code == 'attempt_deadline'
