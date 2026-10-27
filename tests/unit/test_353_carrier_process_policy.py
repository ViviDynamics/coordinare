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
