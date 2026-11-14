"""The daemon's normal bootstrap route selects nare and retains domain authority."""
from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest
from nare import Reply, ToolCall, Usage

from coordinare.config import ConductingConfig
from coordinare.services.activity_log import ActivityLog
from coordinare.services.agent_runtime.configuration import AgentModelConfig
from tests.unit.test_060_env_cache import _make_daemon_for_env_cache
from tests.unit.test_353_bootstrap_pipeline import Proposal, Repository, probe_transport, setup
from tests.unit.test_353_scope_contract import criterion, proposal, review
from tests.unit.test_376_bootstrap_phases import PhaseTransport


class BootstrapTransport(PhaseTransport):
    def __init__(self, instructions: str) -> None:
        super().__init__({}, read_path='guide')
        self.instructions = instructions
        self.probed = False

    async def turn(self, messages: Any, tools: Any) -> Reply:
        reply = await super().turn(messages, tools)
        if reply.stop_reason != 'end_turn':
            return reply
        packet = json.loads(''.join(self.packet_pages[k] for k in sorted(self.packet_pages)))
        context = json.loads(packet['context'][0]['content']) if 'context' in packet else packet
        if 'Author source-defined obligations' in self.instructions:
            answer = proposal(criterion('guide'))
        elif 'Independently review source-defined scope' in self.instructions:
            answer = review(sources=['guide'])
        elif 'manifest' in context:
            answer = {'assessment': {'verdict': 'accept', 'rationale': 'Independent fixture coverage review.',
                'sources': ['guide'], 'findings': [], 'decisions': [], 'capability_unavailable': False}}
        elif not self.probed:
            self.probed = True
            call = ToolCall('preflight', 'probe', {'commands': [{'id': 'runtime', 'argv': [sys.executable, '--version']}],
                'citations': {'runtime': ['guide']}, 'questions': {'runtime': 'Is the documented runtime available?'}})
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                [call], Usage(input=100, output=20), 'tool_use')
        else:
            answer = json.loads(await Proposal()([{'role': 'system', 'content': ''},
                {'role': 'user', 'content': json.dumps(context)}, {'role': 'user', 'content': '{"probe_result":{}}'}]))
            answer['manifest']['criterion_checks'] = {c['id']: ['read-payload']
                for c in context['scope_contract']['definition']['criteria']}
            answer['finding_responses'] = []
        return Reply([{'type': 'text', 'text': json.dumps(answer)}], [], Usage(input=100, output=20), 'end_turn')


def configured_daemon(tmp_path: Any, monkeypatch: Any, transport_type: Any = BootstrapTransport) -> Any:
    from types import SimpleNamespace

    service, cache, state, capabilities = setup(tmp_path)
    config = service._coordinare_config
    config.global_config.base_branch = 'main'
    config.global_config.conducting = ConductingConfig(backend='openai_api', model='selected/bootstrap',
        base_url='https://provider.example/v1', api_key_env='BOOTSTRAP_TEST_KEY')
    symphony = SimpleNamespace(enabled=True, env_bootstrap_performer_id='bootstrap', env_spec_files=['guide'],
        test_env=None, effective_config=lambda cfg: cfg)
    repository = Repository()
    daemon = _make_daemon_for_env_cache()
    daemon._state.update(state)
    daemon._state.update(symphony_github_services={'suite': repository}, performer_services_by_id={}, activity_log=ActivityLog())
    config.global_config.state_file_path.parent.mkdir(parents=True)
    async def persist() -> None:
        config.global_config.state_file_path.write_text(cache.model_dump_json())
    daemon._state['snapshot_checkpoint_fn'] = persist
    daemon._maybe_dispatch_intake = AsyncMock()
    daemon._execute_bootstrap_dispatch = AsyncMock()
    def legacy() -> None:
        pytest.fail('The daemon must not construct the old bootstrap chat backend')
    monkeypatch.setattr(daemon, '_get_manifest_llm_chat', legacy)
    selected = []
    def transport(model: AgentModelConfig, instructions: str) -> BootstrapTransport:
        selected.append(model)
        return transport_type(instructions)
    monkeypatch.setattr(AgentModelConfig, 'transport', transport)
    monkeypatch.setenv('BOOTSTRAP_TEST_KEY', 'fixture-key')
    monkeypatch.setattr('coordinare.services.environment_probe_service.execute_environment_probe', probe_transport)
    monkeypatch.setattr('coordinare.services.environment_capabilities.resolve_execution_capabilities', AsyncMock(return_value=capabilities))
    return daemon, cache, symphony, selected


@pytest.mark.asyncio
async def test_daemon_cycle_uses_selected_native_model_and_publishes_progress(tmp_path: Any, monkeypatch: Any) -> None:
    daemon, cache, symphony, selected = configured_daemon(tmp_path, monkeypatch)
    await daemon._run_env_cache_and_cardless_cycle({'suite': symphony})
    assert selected and {model.model for model in selected} == {'selected/bootstrap'}
    assert all(model.api_key == 'fixture-key' and model.base_url == 'https://provider.example/v1' for model in selected)
    daemon._execute_bootstrap_dispatch.assert_awaited_once()
    assert cache.environment_assessment['verdict'] == 'accept'
    assert cache.environment_attempt['agent_checkpoints']
    assert not cache.cache_dir_ready and cache.provenance_digest is None
    entries = daemon._state['activity_log'].snapshot()
    assert any(entry['activity_type'] == 'bootstrap_agent' and entry['symphony'] == 'suite' for entry in entries)
    assert all('fixture-key' not in entry['text'] for entry in entries)


@pytest.mark.asyncio
async def test_daemon_preparation_has_one_owner_and_cancellation_is_terminal(tmp_path: Any, monkeypatch: Any) -> None:
    import asyncio
    from datetime import UTC, datetime

    from coordinare.services.bootstrap_status import symphony_view

    entered = asyncio.Event()
    class Waiting(BootstrapTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            entered.set()
            await asyncio.Event().wait()
            return await super().turn(messages, tools)
    daemon, cache, symphony, selected = configured_daemon(tmp_path, monkeypatch, Waiting)
    first = asyncio.create_task(daemon._run_env_cache_and_cardless_cycle({'suite': symphony}))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        owner = cache.environment_source_owner
        assert owner and len(selected) == 1
        view = symphony_view(daemon._state, 'suite', configured=True, now=datetime.now(UTC))
        assert view['state'] == 'bootstrapping' and view['label'] == 'Reviewing source requirements'
        await asyncio.wait_for(daemon._run_env_cache_and_cardless_cycle({'suite': symphony}), 1)
        assert cache.environment_source_owner == owner
        assert cache.last_bootstrap_error is None
        assert len(selected) == 1
    finally:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
    assert cache.environment_source_owner is None
    assert cache.environment_attempt['status'] == 'held'
    assert cache.environment_attempt['last_error'] == 'attempt_cancelled'
    assert not cache.cache_dir_ready
    daemon._execute_bootstrap_dispatch.assert_not_awaited()
    view = symphony_view(daemon._state, 'suite', configured=True, now=datetime.now(UTC))
    assert view['state'] == 'cancelled'


@pytest.mark.asyncio
async def test_clean_legacy_restart_keeps_budget_and_starts_native_without_invented_receipts(tmp_path: Any, monkeypatch: Any) -> None:
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt

    daemon, cache, symphony, selected = configured_daemon(tmp_path, monkeypatch)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    budget.calls, budget.reads, budget.execution_repairs = 5, 3, 1
    save_attempt(cache, budget)
    original_expiry = cache.environment_attempt['expires_at']
    # An actual old journal has neither native references nor trial accounting.
    for field in ('agent_checkpoints', 'agent_phase_calls', 'trial_runs', 'agent_runtime'):
        cache.environment_attempt.pop(field, None)
    await daemon._run_env_cache_and_cardless_cycle({'suite': symphony})
    assert selected
    journal = cache.environment_attempt
    assert journal['agent_runtime'] == 'nare'
    assert journal['expires_at'] == original_expiry and journal['calls'] > 5 and journal['reads'] >= 3
    assert journal['trial_usage_unknown'] is True and journal['trial_runs'] == 3
    assert journal['agent_checkpoints']
    assert not cache.cache_dir_ready


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['source', 'probe'])
async def test_uncertain_legacy_external_work_refuses_before_any_native_request(tmp_path: Any, monkeypatch: Any, owner: str) -> None:
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt

    daemon, cache, symphony, selected = configured_daemon(tmp_path, monkeypatch)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    cache.environment_attempt[owner + '_owner'] = 'unreconciled-owner'
    await daemon._run_env_cache_and_cardless_cycle({'suite': symphony})
    assert not selected
    assert cache.environment_attempt[owner + '_owner'] == 'unreconciled-owner'
    assert not cache.cache_dir_ready and cache.environment_attempt['calls'] == 0
    daemon._execute_bootstrap_dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_daemon_restart_resumes_verified_provider_failure_without_renewing_budget(tmp_path: Any, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache

    class Broken(BootstrapTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            raise ConnectionError('synthetic provider outage')

    daemon, cache, symphony, _ = configured_daemon(tmp_path, monkeypatch, Broken)
    await daemon._run_env_cache_and_cardless_cycle({'suite': symphony})
    journal = cache.environment_attempt
    assert journal['last_error'] == 'provider_request_failed'
    assert journal['agent_checkpoints']
    old_refs = dict(journal['agent_checkpoints'])
    calls, expiry = journal['calls'], journal['expires_at']
    snapshot = SimpleNamespace(env_cache=_persist_env_cache({'suite': cache}))
    restart_root = tmp_path / 'restart'
    restart_root.mkdir()
    restarted, restored, symphony, selected = configured_daemon(restart_root, monkeypatch)
    restarted._state['config'].state_file_path = daemon._state['config'].state_file_path
    _rehydrate_env_cache(restarted._state, snapshot)
    from datetime import timedelta
    restored.last_bootstrap_at -= timedelta(hours=1)  # Existing retry cooldown, not the attempt deadline.
    await restarted._run_env_cache_and_cardless_cycle({'suite': symphony})
    assert selected and restored.environment_assessment['verdict'] == 'accept'
    assert restored.environment_attempt['calls'] > calls
    assert restored.environment_attempt['expires_at'] == expiry
    assert old_refs.keys() <= restored.environment_attempt['agent_checkpoints'].keys()
    assert not restored.cache_dir_ready
    restarted._execute_bootstrap_dispatch.assert_awaited_once()


@pytest.mark.parametrize('relation', ['parent', 'child'])
def test_private_checkpoints_refuse_any_performer_mount_overlap(tmp_path: Any, monkeypatch: Any, relation: str) -> None:
    from coordinare.services.environment_agent_lifecycle import bootstrap_agent_context
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt

    daemon, cache, _, _ = configured_daemon(tmp_path, monkeypatch)
    begin_attempt(cache, DiscoveryLimits(), outer_seconds=600)
    config = daemon._state['config']
    model = daemon._get_manifest_agent_model()
    context = bootstrap_agent_context(model=model, cache=cache, state=daemon._state, config=config, performer_mounts=[])
    root = context.checkpoint_store.root
    mount = root.parent if relation == 'parent' else root / 'visible-subtree'
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        bootstrap_agent_context(model=model, cache=cache, state=daemon._state, config=config, performer_mounts=[mount])
    assert caught.value.code == 'agent_checkpoint_exposed'


@pytest.mark.asyncio
async def test_verified_legacy_artifact_survives_real_snapshot_restore(tmp_path: Any, monkeypatch: Any) -> None:
    from types import SimpleNamespace

    from coordinare_service_inference.environment.runner import run_manifest

    from coordinare.daemon import _persist_env_cache, _rehydrate_env_cache
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.env_cache import verify_env_cache_clean
    from coordinare.services.environment_acceptance import (
        accepted_environment,
        saved_execution_accepted,
    )
    from tests.unit.test_353_bootstrap_pipeline import SOURCE, local_clean_transport, publish

    service, cache, state, capabilities = setup(tmp_path)
    await publish(service, cache, state, capabilities, Proposal(repair=True))
    manifest = accepted_environment(cache)
    repo, scratch = tmp_path / 'installer', tmp_path / 'scratch'
    repo.mkdir()
    scratch.mkdir()
    (repo / 'guide').write_text(SOURCE)
    assert (await run_manifest(manifest, cache=cache.cache_dir, repo=repo, scratch=scratch, install=True)).passed
    await local_clean_transport(monkeypatch, capabilities, cache)
    passed, reason = await verify_env_cache_clean(state, 'suite', SimpleNamespace(), performer_mounts=(cache.cache_dir.parent,))
    assert passed, reason
    frozen, reason = await service.freeze_provenance('suite', state)
    assert frozen, reason
    service.on_bootstrap_complete('suite', True, state)
    snapshots = _persist_env_cache({'suite': cache})
    restored = EnvCacheState(symphony_name='suite', sanitised_name='suite', cache_dir=cache.cache_dir)
    _rehydrate_env_cache({'env_cache': {'suite': restored}}, SimpleNamespace(env_cache=snapshots))
    assert restored.cache_dir_ready and saved_execution_accepted(restored)
    assert restored.environment_verification == cache.environment_verification
    assert restored.provenance_digest == cache.provenance_digest
    assert accepted_environment(restored).digest() == manifest.digest()


def test_preparation_status_accepts_serialized_cache_shape(tmp_path: Any, monkeypatch: Any) -> None:
    from datetime import UTC, datetime

    from coordinare.services.bootstrap_status import symphony_view

    daemon, cache, _, _ = configured_daemon(tmp_path, monkeypatch)
    daemon._state['env_cache']['suite'] = cache.model_dump(mode='json')
    view = symphony_view(daemon._state, 'suite', configured=True, now=datetime.now(UTC))
    assert view['state'] == 'not_started'
