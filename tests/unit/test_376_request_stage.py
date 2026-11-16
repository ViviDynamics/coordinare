"""Request-stage selection precedes accounting and constrains native dispatch."""
from __future__ import annotations

from typing import Any

import pytest
from nare import Reply, ToolCall, Usage

from coordinare.services.agent_runtime.accounting import (
    BudgetBridge,
    BudgetTransport,
    TransportContext,
)
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError


class Provider:
    def __init__(self, name: str = 'required_read') -> None:
        self.name = name
        self.sent: list[Any] = []

    async def turn(self, messages: Any, tools: Any, **kwargs: Any) -> Reply:
        self.sent.append((tools, kwargs))
        call = ToolCall('one', self.name, {})
        return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': {}}],
                     [call], Usage(input=30, output=5), 'tool_use')

    async def stream_turn(self, messages: Any, tools: Any, **kwargs: Any) -> Any:
        yield await self.turn(messages, tools, **kwargs)


class ReadStage:
    def select(self, tools: Any, schema: Any, mode: Any, choice: Any) -> Any:
        from coordinare.services.agent_runtime.request_stage import RequestSelection
        return RequestSelection(tools=[{'name': 'required_read', 'input_schema': {'type': 'object'}}],
            tool_choice='required', allowed_calls=frozenset({'required_read'}))


async def saved() -> None:
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('name', ['required_read', 'side_effect'])
async def test_stage_checks_reply_before_dispatch_without_losing_known_usage(streaming: bool, name: str) -> None:
    budget = AttemptBudget()
    provider = Provider(name)
    transport = BudgetTransport(provider, BudgetBridge(budget, phase='review'), TransportContext(
        session_id='fictional', before_send=saved, request_stage=ReadStage()))
    async def invoke() -> list[Any]:
        if streaming:
            return [event async for event in transport.stream_turn([], [{'name': 'side_effect'}],
                response_schema={'type': 'object'}, response_mode='json_object')]
        return [await transport.turn([], [{'name': 'side_effect'}],
            response_schema={'type': 'object'}, response_mode='json_object')]
    if name == 'side_effect':
        with pytest.raises(EnvironmentDiscoveryError) as caught:
            await invoke()
        assert caught.value.code == 'agent_request_stage_violation'
    else:
        assert len(await invoke()) == 1
    assert provider.sent == [([{'name': 'required_read', 'input_schema': {'type': 'object'}}],
                             {'tool_choice': 'required'})]
    assert budget.calls == budget.calls_completed == 1
    assert budget.agent_usage_unknown == 0
    assert budget.usage['input_tokens'] == 30


@pytest.mark.asyncio
async def test_call_metadata_records_effective_request_not_nominal_answer_turn(tmp_path: Any) -> None:
    import json

    from coordinare.services.conductor_calls import ConductorCallLog
    from coordinare.services.run_transcript import TranscriptCollector
    calls = ConductorCallLog(TranscriptCollector(tmp_path, enabled=True, max_bytes_per_run=1000000, retention_days=0))
    provider = Provider()
    transport = BudgetTransport(provider, BudgetBridge(AttemptBudget(), phase='review'), TransportContext(
        session_id='fictional', before_send=saved, calls=calls, request_stage=ReadStage()))
    await transport.turn([], [{'name': 'side_effect'}], response_schema={'type': 'object'}, response_mode='json_object')
    await calls.drain()
    headers = [json.loads(path.read_text().splitlines()[0]) for path in tmp_path.rglob('*.jsonl')]
    effective = headers[0]['conductor']['parameters']['native_request']
    assert effective == {'tools': provider.sent[0][0], 'tool_choice': 'required',
                         'response_schema': None, 'response_mode': None}


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_real_native_loop_never_dispatches_out_of_stage_side_effect(streaming: bool) -> None:
    from dataclasses import replace

    from nare import Tool

    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from tests.unit.test_376_agent_runtime import request
    executed = []
    async def side_effect(arguments: Any) -> str:
        executed.append(arguments)
        return 'executed'
    async def admit(answer: Any) -> list[Any]:
        return []
    class NativeProvider(Provider):
        async def context_window(self) -> int:
            return 262144
    provider = NativeProvider('side_effect')
    provider.streaming = streaming
    req = replace(request(admit), request_stage=ReadStage(), allowed_tools=(Tool(name='side_effect',
        description='Approved action available only outside read stage.',
        input_schema={'type': 'object'}, call=side_effect),))
    result = await NareRuntime(transport=provider).run(req)
    assert result.status == 'held' and result.hold_code == 'agent_request_stage_violation'
    assert executed == []
    assert req.budget.calls == req.budget.calls_completed == 1
    assert req.budget.agent_usage_unknown == 0
    # Rejected replies never enter nare's conversation; physical usage belongs
    # to the attempt ledger and is persisted before this refusal.
    assert req.budget.usage['input_tokens'] == 30


@pytest.mark.asyncio
async def test_read_selection_preserves_declared_context_budget_floor() -> None:
    provider = Provider()
    budget = AttemptBudget()
    transport = BudgetTransport(provider, BudgetBridge(budget, phase='review'), TransportContext(
        session_id='fictional', before_send=saved, request_stage=ReadStage(), max_context_bytes=500,
        answer_schema={'description': 'declared contract' * 1000}))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await transport.turn([], [])
    assert caught.value.code == 'review_context_limit'
    assert not provider.sent and budget.calls == 0
