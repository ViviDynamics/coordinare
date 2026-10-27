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

    result = carrier_process_policy(SimpleNamespace(runtime=object()))
    assert result['applies_to'] == 'installer_and_check_carrier_processes'
    assert result['linux_capabilities'] == []
    assert result['no_new_privileges'] is True
    assert result['uid_zero_restores_capabilities'] is False
    assert 'group' in result['installation_guidance'] and '{cache}' in result['installation_guidance']


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
                closure_checks=lambda _group: [], closure_criteria=lambda _ids: [])
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
