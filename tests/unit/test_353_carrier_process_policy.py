"""Private-runtime authors and reviewers receive the carrier's real privilege boundary."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import ExecutionCapabilities
from coordinare_service_inference.environment.runtime import RuntimePolicy

from tests.unit.test_351_environment_discovery import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    ScriptedChat,
)
from tests.unit.test_353_runtime_identity import policy_data


def test_native_execution_does_not_invent_a_capability_policy():
    from coordinare.services.environment_capabilities import carrier_process_policy

    assert carrier_process_policy(SimpleNamespace(runtime=None)) is None


def test_private_runtime_policy_does_not_equate_uid_zero_with_capabilities():
    from coordinare.services.environment_capabilities import carrier_process_policy

    runtime = SimpleNamespace(resources=SimpleNamespace(cpus=8, memory_mib=4096, max_seconds=3600))
    result = carrier_process_policy(SimpleNamespace(runtime=runtime))
    assert result['applies_to'] == 'installer_and_check_carrier_processes'
    assert result['linux_capabilities'] == []
    assert result['no_new_privileges'] is True
    assert result['uid_zero_restores_capabilities'] is False
    assert 'group' in result['installation_guidance'] and '{cache}' in result['installation_guidance']
    assert result['guest_docker_ready_before_manifest_commands'] is True
    assert result['manifest_execution_order']==['guest_readiness','activation_environment','install_if_requested',
        'activation_path_validation','service_init_start_health','checks','service_stop','seal_if_install_passed','runtime_teardown']
    assert 'continued daemon health' in result['runtime_ordering_guidance']


@pytest.mark.asyncio
async def test_actual_author_and_fresh_reviewer_share_the_private_carrier_policy():
    from coordinare.services.environment_capabilities import carrier_process_policy
    from tests.unit.scope_chat_fixture import discover_with_fixture_scope

    policy = policy_data()
    policy['identity'].update(carrier_image=CAPABILITIES.image_ref, carrier_platform=CAPABILITIES.platform)
    capabilities = ExecutionCapabilities.model_validate({**CAPABILITIES.model_dump(mode='json'), 'schema_version': 2,
        'runtime': RuntimePolicy.model_validate(policy).model_dump(mode='json')})
    chat = ScriptedChat()

    async def fetch(path):
        return SOURCE if path == PATH else 'Additional fixture evidence'

    await discover_with_fixture_scope(symphony_name='suite', commit=COMMIT, paths=[PATH, 'other/evidence'],
        fetch=fetch, chat=chat, capabilities=capabilities)
    for messages in (chat.calls[0], chat.calls[2]):
        identity = json.loads(messages[1]['content'])
        assert identity['carrier_process_policy'] == carrier_process_policy(capabilities)
        assert identity['carrier_process_policy']['linux_capabilities'] == []


@pytest.mark.asyncio
async def test_native_author_and_reviewer_do_not_claim_the_private_runtime_boundary():
    from tests.unit.test_351_environment_discovery import discover

    chat = ScriptedChat()
    await discover(chat)
    for messages in (chat.calls[0], chat.calls[2]):
        assert 'carrier_process_policy' not in json.loads(messages[1]['content'])


def test_private_policy_matches_actual_docker_carrier_options(tmp_path):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    from coordinare.services.environment_capabilities import carrier_process_policy
    from coordinare.services.environment_runtime import runtime_verifier_options
    from tests.unit.runtime_manifest_fixture import runtime_cache

    cache = runtime_cache(tmp_path)
    manifest = EnvironmentManifest.model_validate(cache.environment_manifest)
    capabilities = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    policy = carrier_process_policy(capabilities)
    options = runtime_verifier_options(manifest, tmp_path)
    assert options[options.index('--cap-drop') + 1] == 'ALL'
    assert policy['linux_capabilities'] == []
    assert options[options.index('--security-opt') + 1] == 'no-new-privileges'
    assert policy['no_new_privileges'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('private', [False, True], ids=['native', 'private'])
@pytest.mark.parametrize('repair', [False, True], ids=['initial', 'delta'])
async def test_actual_divided_check_authors_receive_the_daemon_process_policy(monkeypatch, private, repair):
    from coordinare.services import environment_discovery as discovery
    from coordinare.services import environment_shard_construction as shards
    from coordinare.services.environment_capabilities import carrier_process_policy
    from tests.unit.scope_chat_fixture import discover_with_fixture_scope
    from tests.unit.test_353_sharded_executable import SUMMARY

    selected = CAPABILITIES
    if private:
        policy = policy_data()
        policy['identity'].update(carrier_image=CAPABILITIES.image_ref, carrier_platform=CAPABILITIES.platform)
        selected = ExecutionCapabilities.model_validate({**CAPABILITIES.model_dump(mode='json'),
            'schema_version': 2, 'runtime': policy})
    captured = {}
    saved_keys = []
    original_saved = shards.saved_part
    original = discovery._divided_construction

    class BoundaryReached(BaseException):
        pass

    def build(scope, attempt):
        attempt.budget.scope_shards = {'synthetic_division': True}
        return original(scope, attempt)

    async def author(_attempt, divided, _validate):
        group = divided.groups[0]
        if repair:
            divided.delta = SimpleNamespace(group_touched=lambda _group: True,
                triage=SimpleNamespace(criterion_ids=list(group.ids)), digest='d' * 64,
                closure_checks=lambda _group: [], closure_criteria=lambda _ids: [], closure=frozenset(),
                fixed=frozenset())
        await divided.author(group, summary=SUMMARY, probes=[])

    def saved_part(budget, stage, prefix, key):
        saved_keys.append(key)
        return original_saved(budget, stage, prefix, key)

    async def capture(_context, _inspection, messages, _task):
        captured.update(json.loads(messages[1]['content']))
        raise BoundaryReached

    async def fetch(path):
        return SOURCE if path == PATH else 'Additional fixture evidence'

    monkeypatch.setattr(discovery, 'divided', lambda _scope: True)
    monkeypatch.setattr(discovery, '_divided_construction', build)
    monkeypatch.setattr(discovery, '_author_candidate', author)
    monkeypatch.setattr(shards, 'run_agent', capture)
    monkeypatch.setattr(shards, 'saved_part', saved_part)
    with pytest.raises(BoundaryReached):
        await discover_with_fixture_scope(symphony_name='suite', commit=COMMIT,
            paths=[PATH, 'other/evidence'], fetch=fetch, chat=ScriptedChat(), capabilities=selected)
    if private:
        assert captured['carrier_process_policy'] == carrier_process_policy(selected)
    else:
        assert 'carrier_process_policy' not in captured
    if repair:
        assert (saved_keys[-1] != 'd' * 64) is private


class Captured(BaseException):
    pass


async def get_attempt(monkeypatch, private):
    from coordinare.services import environment_discovery as discovery
    from tests.unit.scope_chat_fixture import discover_with_fixture_scope

    selected = CAPABILITIES
    if private:
        policy = policy_data()
        policy['identity'].update(carrier_image=CAPABILITIES.image_ref, carrier_platform=CAPABILITIES.platform)
        selected = ExecutionCapabilities.model_validate({**CAPABILITIES.model_dump(mode='json'),
            'schema_version': 2, 'runtime': policy})
    seen = {}

    async def stop(attempt, _build, _validate):
        seen['attempt'] = attempt
        raise Captured

    async def fetch(path):
        return SOURCE if path == PATH else 'Synthetic additional evidence'

    monkeypatch.setattr(discovery, '_author_candidate', stop)
    with pytest.raises(Captured):
        await discover_with_fixture_scope(symphony_name='suite', commit=COMMIT,
            paths=[PATH, 'other/evidence'], fetch=fetch, chat=ScriptedChat(), capabilities=selected)
    return seen['attempt'], selected


@pytest.mark.asyncio
@pytest.mark.parametrize('private', [False, True], ids=['native', 'private'])
@pytest.mark.parametrize('path', ['initial_global', 'delta_global', 'delta_manifest'])
@pytest.mark.parametrize('legacy_context',['without_policy','without_ordering'])
async def test_legacy_delta_author_cache_replays_only_if_policy_context_unchanged(monkeypatch, private, path, legacy_context):
    import copy

    from coordinare.services import environment_discovery as discovery
    from tests.unit.test_353_delta_repair import base_proposal
    from tests.unit.test_353_sharded_executable import global_part

    attempt, _selected = await get_attempt(monkeypatch, private)
    attempt.delta = SimpleNamespace(digest='d' * 64, base_proposal=base_proposal, guard_part=lambda _base, _candidate: None,
                                    restore_frozen=lambda _base, candidate: candidate)
    current = copy.deepcopy(attempt.messages)
    legacy = json.loads(attempt.messages[1]['content'])
    if legacy_context=='without_policy':
        legacy.pop('carrier_process_policy',None)
    else:
        for key in ('guest_docker_ready_before_manifest_commands','manifest_execution_order','runtime_ordering_guidance'):
            legacy.get('carrier_process_policy',{}).pop(key,None)
    attempt.messages[1]['content'] = discovery._json(legacy)
    called = []

    async def synthetic_author(**kwargs):
        called.append(copy.deepcopy(kwargs['messages']))
        data = global_part() if path == 'initial_global' else {
            'install': [{'id': 'install-a', 'argv': ['make', 'a', 'PREFIX={cache}']}]}
        return data, dict(attempt.discovery.inputs)

    monkeypatch.setattr(discovery, '_phase', synthetic_author)

    async def invoke():
        if path == 'initial_global':
            return await discovery._global_part(attempt)
        if path == 'delta_global':
            return await discovery._delta_global_part(attempt, lambda data: data, base_proposal())
        return await discovery._delta_candidate(attempt, lambda data: data)

    original_budget = attempt.budget
    original_started = original_budget.started
    await invoke()
    assert len(called) == 1
    attempt.messages[:] = current
    await invoke()
    assert attempt.budget is original_budget and original_budget.started == original_started
    assert len(called) == (2 if private else 1), (
        'legacy private delta author result reused without delivering the newly enforced policy context')

    await invoke()
    assert len(called) == (2 if private else 1)


def test_delta_policy_key_binds_policy_content_and_preserves_native_keys():
    from coordinare.services.environment_shard_construction import delta_part_key

    digest = 'd' * 64
    assert delta_part_key(digest, None) == digest
    policy = {'linux_capabilities': [], 'no_new_privileges': True}
    key = delta_part_key(digest, policy)
    assert key != digest
    assert delta_part_key(digest, dict(reversed(list(policy.items())))) == key
    assert delta_part_key(digest, {**policy, 'no_new_privileges': False}) != key
    assert delta_part_key('e' * 64, policy) != key
