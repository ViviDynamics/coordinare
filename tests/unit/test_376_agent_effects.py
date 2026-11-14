"""An interrupted external action must never become permission to run it twice."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError


@pytest.mark.asyncio
async def test_external_reservation_is_durable_before_execution_and_completion_is_reused() -> None:
    from coordinare.services.agent_runtime.effects import EffectJournal

    durable = []
    executed = []
    journal = EffectJournal()
    async def persist() -> None:
        durable.append(journal.receipts())
    journal.persist = persist
    async def execute() -> dict[str, Any]:
        assert durable[-1][0].status == 'pending'
        executed.append('probe')
        return {'observed': 'tool absent', 'passed': False}
    first = await journal.run('probe', 'a'*64, execute)
    assert first == {'observed': 'tool absent', 'passed': False}
    assert durable[-1][0].status == 'completed'
    restored = EffectJournal(durable[-1])
    restored.persist = persist
    second = await restored.run('probe', 'a'*64, execute)
    assert second == first
    assert executed == ['probe']


@pytest.mark.asyncio
async def test_interrupted_effect_cannot_be_replayed_or_admitted() -> None:
    from coordinare.services.agent_runtime.effects import EffectJournal

    journal = EffectJournal()
    async def persist() -> None:
        pass
    journal.persist = persist
    calls = []
    async def interrupted() -> dict[str, Any]:
        calls.append('trial')
        raise OSError('synthetic connection loss after dispatch')
    with pytest.raises(OSError):
        await journal.run('trial', 'b'*64, interrupted)
    restored = EffectJournal(journal.receipts())
    restored.persist = persist
    for operation in (lambda: restored.run('trial', 'b'*64, interrupted), restored.ensure_settled):
        with pytest.raises(EnvironmentDiscoveryError) as caught:
            await operation()
        assert caught.value.code == 'agent_effect_pending'
    assert calls == ['trial']


@pytest.mark.asyncio
async def test_effect_requires_persistence_before_any_external_call() -> None:
    from coordinare.services.agent_runtime.effects import EffectJournal

    calls = []
    async def execute() -> dict[str, Any]:
        calls.append('ran')
        return {}
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await EffectJournal().run('probe', 'a'*64, execute)
    assert caught.value.code == 'agent_effect_persistence'
    assert not calls


@pytest.mark.asyncio
async def test_runtime_checkpoint_holds_interrupted_action_before_resume(tmp_path: Any) -> None:
    import asyncio
    import json
    from dataclasses import replace

    from nare import Reply, Tool, ToolCall, Usage

    from coordinare.services.agent_runtime.checkpoint import AgentCheckpoint, CheckpointStore
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from tests.unit.test_376_agent_accounting import admit
    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    effects = EffectJournal()
    executed = []
    async def execute() -> dict[str, Any]:
        executed.append('probe')
        raise asyncio.CancelledError
    async def call(args: dict[str, Any]) -> str:
        return json.dumps(await effects.run('probe', 'c'*64, execute))
    class Probe(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            self.requests.append({})
            tool = ToolCall('p1', 'probe', {})
            return Reply([{'type': 'tool_use', 'id': tool.id, 'name': tool.name, 'input': {}}],
                         [tool], Usage(input=10, output=10), 'tool_use')
    transport = Probe('')
    req = replace(request(admit), effects=effects, checkpoint_store=CheckpointStore(tmp_path),
        allowed_tools=(Tool(name='probe', description='Bound test probe', input_schema={'type': 'object'}, call=call),))
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'cancelled' and result.checkpoint_key
    saved = AgentCheckpoint.model_validate_json((tmp_path / result.checkpoint_key).read_bytes())
    assert saved.effects[0].status == 'pending'
    resumed = await NareRuntime(transport=transport).run(replace(req, checkpoint_key=result.checkpoint_key,
                                                               effects=EffectJournal()))
    assert resumed.status == 'held' and resumed.hold_code == 'agent_effect_pending'
    assert executed == ['probe'] and len(transport.requests) == 1


@pytest.mark.asyncio
async def test_runtime_observations_are_complete_pages_and_must_be_read() -> None:
    import json

    from coordinare.services.environment_agent import ObservationTools

    observed = {'untrusted_output': '漢字 "\\"\n' * 10000, 'readiness': False}
    tools = ObservationTools()
    result = json.loads(await tools.add(observed))
    pieces = [page['content'] for page in result['pages']]
    assert tools.issues()
    while result['unread_count']:
        serialized = await tools.read({'observation_id': result['observation_id']})
        assert len(serialized) <= 30000
        result = json.loads(serialized)
        pieces.extend(page['content'] for page in result['pages'])
    assert not tools.issues()
    assert json.loads(''.join(pieces)) == observed


@pytest.mark.asyncio
async def test_native_tool_schema_preserves_nested_probe_command_validation() -> None:
    import json
    from dataclasses import replace

    from nare import Reply, Tool, ToolCall, Usage

    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from coordinare.services.agent_runtime.schema import native_tool_schema
    from coordinare.services.environment_probes import ProbeRequest
    from tests.unit.test_353_environment_probes import request_data
    from tests.unit.test_376_agent_accounting import admit
    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    executed = []
    original = ProbeRequest.model_json_schema()
    class Proposer(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            if self.requests:
                return await super().turn(messages, tools)
            self.requests.append({})
            arguments = request_data()
            arguments['commands'][0]['timeout_seconds'] = 31  # Existing 30-second bound must survive inlining.
            call = ToolCall('invalid', 'probe', arguments)
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': arguments}],
                         [call], Usage(input=10, output=10), 'tool_use')
    async def execute(args: dict[str, Any]) -> str:
        executed.append(args)
        return json.dumps({'unexpected': True})
    req = replace(request(admit), allowed_tools=(Tool(name='probe', description='Bounded probe',
        input_schema=native_tool_schema(original), call=execute),))
    result = await NareRuntime(transport=Proposer('{"accepted":true}')).run(req)
    assert result.status == 'accepted'
    assert not executed
    assert original == ProbeRequest.model_json_schema()


@pytest.mark.asyncio
async def test_native_trial_failure_retains_uncertain_external_reservation() -> None:
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_repair_trial import trial_handler

    effects = EffectJournal()
    async def persist() -> None:
        pass
    effects.persist = persist
    async def fail(request: Any) -> Any:
        raise OSError('dispatch connection interrupted')
    run = trial_handler(fail, AttemptBudget(execution_repairs=1), effect_journal=lambda: effects)
    with pytest.raises(OSError):
        await run({'checks': [{'id': 'check', 'argv': ['true']}]})
    assert effects.receipts()[0].status == 'pending'
