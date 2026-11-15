"""Native production entrypoints retain execution and provenance gates."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.agent_runtime.configuration import AgentModelConfig
from tests.integration.test_376_bootstrap_lifecycle import BootstrapTransport
from tests.unit.test_353_bootstrap_pipeline import COMMIT, IMAGE, Repository


@pytest.mark.asyncio
async def test_field_harness_defaults_to_native_and_accounts_independent_phases(tmp_path: Any, monkeypatch: Any) -> None:
    from scripts import validate_environment_repositories as harness

    class Snapshot(Repository):
        name, org, commit = 'suite', 'fixture', COMMIT
        async def verify(self) -> None:
            pass
    selected = []
    def native(model: AgentModelConfig, instructions: str) -> BootstrapTransport:
        selected.append(model)
        return BootstrapTransport(instructions)
    def legacy(*args: Any, **kwargs: Any) -> None:
        pytest.fail('Field acceptance must not instantiate the legacy bootstrap backend')
    async def identity(image: str) -> tuple[str, str]:
        return image, 'linux/aarch64'
    monkeypatch.setattr(AgentModelConfig, 'transport', native)
    monkeypatch.setattr(harness, 'OpenAiApiBackend', legacy, raising=False)
    monkeypatch.setattr('coordinare.services.environment_capabilities._docker_identity', identity)
    monkeypatch.setenv('NATIVE_TEST_KEY', 'fixture-private-key')
    result = await harness.validate_repository(Snapshot(), image=IMAGE, output=tmp_path / 'attempt',
        profile=harness.Profile(model='selected/native', base_url='https://model.example/v1',
            api_key_env='NATIVE_TEST_KEY', phase_timeout=20, save_transcripts=True))
    assert selected and all(model.model == 'selected/native' for model in selected)
    assert result['model_calls'] > 0
    assert result['independent_source_review_reached']
    assert result['independent_source_review_invocations'] == 1
    assert not result['installer_ran'] and not result['ready_at_validation']
    assert result['agent_runtime'] == 'nare'
    assert list((tmp_path / 'daemon-transcripts').rglob('*.jsonl'))
    assert (tmp_path / 'attempt' / 'agent-activity.jsonl').is_file()
    assert 'fixture-private-key' not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper', [None, 'payload', 'source'])
async def test_native_daemon_execution_repair_requires_clean_consumer_and_provenance(tmp_path: Any, monkeypatch: Any, tamper: str | None) -> None:
    import json
    import sys
    from datetime import timedelta
    from types import SimpleNamespace

    from coordinare_service_inference.environment.runner import run_manifest
    from nare import Reply, Usage

    from coordinare.services.env_cache import verify_env_cache_clean
    from coordinare.services.environment_acceptance import accepted_environment
    from tests.integration.test_376_bootstrap_lifecycle import configured_daemon
    from tests.unit.test_353_bootstrap_pipeline import SOURCE, local_clean_transport
    from tests.unit.test_376_bootstrap_phases import PhaseTransport

    authored = []
    class Repairing(BootstrapTransport):
        async def turn(self, messages: Any, tools: Any, *, response_schema: Any = None) -> Reply:
            first = await PhaseTransport.turn(self, messages, tools)
            if first.stop_reason != 'end_turn':
                return first
            packet = json.loads(''.join(self.packet_pages[k] for k in sorted(self.packet_pages)))
            context = json.loads(packet['context'][0]['content']) if 'context' in packet else packet
            if 'repair_delta' in context:
                authored.append('repair')
                answer = {'manifest_delta': {'install': [{'id': 'write-payload', 'argv': [sys.executable, '-c',
                    "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('ready')", '{cache}/payload']}]},
                    'finding_responses': []}
                return Reply([{'type': 'text', 'text': json.dumps(answer)}], [], Usage(input=100, output=20), 'end_turn')
            reply = await super().turn(messages, tools, response_schema=response_schema)
            if reply.stop_reason == 'end_turn':
                answer = json.loads(reply.content[0]['text'])
                if 'manifest' in answer:
                    authored.append('initial')
                    answer['manifest']['install'][0]['argv'][2] = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('broken')"
                    return Reply([{'type': 'text', 'text': json.dumps(answer)}], [], Usage(input=100, output=20), 'end_turn')
            return reply

    daemon, cache, symphony, selected = configured_daemon(tmp_path, monkeypatch, Repairing)
    service = daemon._state['env_cache_service']
    await daemon._run_env_cache_and_cardless_cycle({'suite': symphony})
    initial = accepted_environment(cache)
    assert initial is not None
    repo, scratch = tmp_path / 'installer', tmp_path / 'scratch'
    repo.mkdir()
    scratch.mkdir()
    (repo / 'guide').write_text(SOURCE)
    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
    source_digest = ExecutionSourceIdentity.model_validate(cache.environment_execution_source).digest()
    report = await run_manifest(initial, cache=cache.cache_dir, repo=repo, scratch=scratch, install=True,
                                execution_source_digest=source_digest)
    assert not report.passed
    service.record_bootstrap_execution('suite', daemon._state, report.model_dump(mode='json'), cleanup_confirmed=True)
    service.on_bootstrap_complete('suite', False, daemon._state)
    expiry, calls = cache.environment_attempt['expires_at'], cache.environment_attempt['calls']
    cache.last_bootstrap_at -= timedelta(hours=1)
    await daemon._run_env_cache_and_cardless_cycle({'suite': symphony})
    repaired = accepted_environment(cache)
    assert repaired is not None and repaired.digest() != initial.digest()
    assert authored == ['initial', 'repair']
    assert cache.environment_attempt['execution_repairs'] == 1
    assert cache.environment_attempt['expires_at'] == expiry and cache.environment_attempt['calls'] > calls
    assert (await run_manifest(repaired, cache=cache.cache_dir, repo=repo, scratch=scratch, install=True)).passed
    assert not cache.cache_dir_ready and cache.provenance_digest is None
    if tamper == 'payload':
        (cache.cache_dir / 'payload').write_text('changed after installer')
    elif tamper == 'source':
        cache.environment_execution_source['commit'] = 'b' * 40
    from coordinare_service_inference.environment.models import ExecutionCapabilities
    capabilities = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    await local_clean_transport(monkeypatch, capabilities, cache)
    if tamper == 'source':
        from coordinare.services.environment_attempt import EnvironmentDiscoveryError
        with pytest.raises(EnvironmentDiscoveryError, match='execution source changed'):
            await verify_env_cache_clean(daemon._state, 'suite', SimpleNamespace(), performer_mounts=(cache.cache_dir.parent,))
        assert not cache.cache_dir_ready and cache.provenance_digest is None
        return
    passed, reason = await verify_env_cache_clean(daemon._state, 'suite', SimpleNamespace(), performer_mounts=(cache.cache_dir.parent,))
    if tamper:
        assert not passed
        service.on_bootstrap_complete('suite', True, daemon._state)
        assert not cache.cache_dir_ready and cache.provenance_digest is None
        return
    assert passed, reason
    frozen, reason = await service.freeze_provenance('suite', daemon._state)
    assert frozen, reason
    service.on_bootstrap_complete('suite', True, daemon._state)
    assert cache.cache_dir_ready and cache.provenance_digest
    assert len(selected) > 3
