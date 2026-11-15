"""The domain owns physical request budgets and terminal outcomes under nare."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2
import pytest
from nare import Reply, Usage
from nare.loop import RetryPolicy
from nare.transport import Delta

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_376_agent_runtime import ScriptedTransport, request


async def admit(answer: Any) -> list[Any]:
    return []


async def no_wait(seconds: float) -> None:
    pass


@pytest.mark.asyncio
async def test_every_physical_retry_is_reserved_and_persisted_first() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    budget = AttemptBudget(max_calls=2)
    durable = []

    async def flush() -> None:
        durable.append(budget.calls)

    budget.checkpoint = flush

    class Flaky(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            assert durable[-1] == budget.calls
            if not self.requests:
                self.requests.append({})
                raise httpx2.ConnectError('synthetic offline failure')
            return await super().turn(messages, tools)

    transport = Flaky('{"accepted":true}')
    result = await NareRuntime(transport=transport,
        retry=RetryPolicy(retries=1, seconds=10, sleep=no_wait)).run(replace(request(admit), budget=budget))
    assert result.status == 'accepted'
    assert budget.calls == 2
    assert budget.calls_completed == 1
    assert budget.usage['input_tokens'] == 11
    assert budget.agent_usage_unknown == 1
    assert len(transport.requests) == 2


@pytest.mark.asyncio
async def test_exhaustion_prevents_the_next_retry() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    class Broken(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            self.requests.append({})
            raise httpx2.ConnectError('synthetic offline failure')

    transport = Broken('')
    budget = AttemptBudget(max_calls=1)
    result = await NareRuntime(transport=transport,
        retry=RetryPolicy(retries=2, seconds=10, sleep=no_wait)).run(replace(request(admit), budget=budget))
    assert result.status == 'held'
    assert result.hold_code == 'attempt_call_limit'
    assert budget.calls == len(transport.requests) == 1


@pytest.mark.asyncio
async def test_replayed_usage_is_counted_once() -> None:
    from coordinare.services.agent_runtime.accounting import BudgetBridge

    budget = AttemptBudget()
    bridge = BudgetBridge(budget, phase='review')
    reply = Reply([], [], Usage(input=17, output=9, cache_read=4), 'end_turn')
    await bridge.record_reply('physical-1', reply)
    await bridge.record_reply('physical-1', reply)
    assert budget.calls_completed == 1
    assert budget.usage == {'input_tokens': 17, 'output_tokens': 9, 'cache_read': 4, 'cache_write': 0}


@pytest.mark.asyncio
async def test_schema_correction_is_charged_once_before_next_call() -> None:
    from coordinare.services.agent_runtime.models import AdmissionIssue
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    checks = 0
    budget = AttemptBudget(max_calls=3, max_schema_repairs=1)

    async def check(answer: Any) -> list[Any]:
        nonlocal checks
        checks += 1
        return [AdmissionIssue(code='missing_proof', message='A receipt is required.')] if checks == 1 else []

    result = await NareRuntime(transport=ScriptedTransport('{"accepted":true}')).run(replace(request(check), budget=budget))
    assert result.status == 'accepted'
    assert budget.schema_repairs == 1
    assert budget.coverage_revisions == budget.scope_revisions == budget.execution_repairs == 0
    assert budget.calls == 2


@pytest.mark.asyncio
async def test_malformed_answer_cannot_get_extra_calls_with_zero_corrections() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    transport = ScriptedTransport('not JSON')
    budget = AttemptBudget(max_calls=3, max_schema_repairs=0)
    result = await NareRuntime(transport=transport).run(replace(request(admit), budget=budget))
    assert result.status == 'held'
    assert result.hold_code == 'attempt_schema_limit'
    assert len(transport.requests) == 1


@pytest.mark.asyncio
async def test_timeout_reports_terminal_hold_and_preserves_working_checkpoint(tmp_path: Path) -> None:
    from nare import loads

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    class Waiting(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            await asyncio.Future()
            raise AssertionError('unreachable')

    budget = AttemptBudget(timeout_seconds=0.05)
    deadline = budget.deadline
    disk = CheckpointStore(tmp_path)
    req = replace(request(admit), budget=budget, checkpoint_store=disk)
    result = await NareRuntime(transport=Waiting('')).run(req)
    assert result.status == 'held'
    assert result.hold_code == 'attempt_deadline'
    assert budget.deadline == deadline
    assert result.checkpoint_key is not None
    saved = await disk.load_verified(result.checkpoint_key, req.identity)
    assert loads(saved.session_text).status == 'working'
    assert saved.counters['calls'] == budget.calls == 1


@pytest.mark.asyncio
async def test_cancelled_stream_closes_and_emits_no_source_or_credentials() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    streaming = asyncio.Event()
    closed = asyncio.Event()
    events = []

    async def progress(event: Any) -> None:
        events.append(event)

    class Streaming(ScriptedTransport):
        streaming = True

        async def stream_turn(self, messages: Any, tools: Any):
            try:
                yield Delta('progress', 'private-source credential=never-export-this')
                streaming.set()
                await asyncio.Future()
            finally:
                closed.set()

    budget = AttemptBudget()
    req = replace(request(admit), budget=budget, event_sink=progress)
    task = asyncio.create_task(NareRuntime(transport=Streaming('')).run(req))
    await asyncio.wait_for(streaming.wait(), timeout=2)
    task.cancel()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.status == 'cancelled'
    assert closed.is_set()
    assert budget.calls == 1
    assert budget.agent_usage_unknown == 1
    assert events
    assert 'private-source' not in repr(events)
    assert 'never-export-this' not in repr(events)


@pytest.mark.asyncio
async def test_expired_budget_makes_no_model_request() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    budget = AttemptBudget(timeout_seconds=1, started=time.monotonic() - 2)
    transport = ScriptedTransport('{"accepted":true}')
    result = await NareRuntime(transport=transport).run(replace(request(admit), budget=budget))
    assert result.hold_code == 'attempt_deadline'
    assert not transport.requests


@pytest.mark.asyncio
async def test_missing_provider_usage_remains_unknown() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    class Unmetered(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            return Reply([{'type': 'text', 'text': '{"accepted":true}'}], [], Usage(), 'end_turn')

    budget = AttemptBudget()
    result = await NareRuntime(transport=Unmetered('')).run(replace(request(admit), budget=budget))
    assert result.status == 'accepted'
    assert budget.agent_usage_unknown == 1
    assert budget.usage == {}


@pytest.mark.asyncio
async def test_usage_receipts_survive_domain_restart(tmp_path: Path) -> None:
    from coordinare.services.agent_runtime.accounting import BudgetBridge
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=100)
    reply = Reply([], [], Usage(input=31, output=5), 'end_turn')
    await BudgetBridge(budget, phase='review').record_reply('stable-receipt', reply)
    save_attempt(cache, budget)
    restored = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=200)
    await BudgetBridge(restored, phase='review').record_reply('stable-receipt', reply)
    assert restored.usage['input_tokens'] == 31
    assert restored.usage['output_tokens'] == 5
    assert restored.calls_completed == 1


@pytest.mark.asyncio
async def test_physical_calls_reach_existing_transcript_and_usage_pipeline(tmp_path: Path) -> None:
    import json

    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from coordinare.services.conductor_calls import ConductorCallLog
    from coordinare.services.run_transcript import TranscriptCollector

    collector = TranscriptCollector(tmp_path, enabled=True, max_bytes_per_run=1000000, retention_days=0)
    calls = ConductorCallLog(collector)
    result = await NareRuntime(transport=ScriptedTransport('{"accepted":true}'), calls=calls).run(request(admit))
    await calls.drain()
    assert result.status == 'accepted'
    paths = list(tmp_path.rglob('*.jsonl'))
    assert len(paths) == 1
    records = [json.loads(line) for line in paths[0].read_text().splitlines()]
    usage = [row for row in records if row.get('type') == 'usage']
    assert len(usage) == 1
    assert usage[0]['usage']['input_tokens'] == 11
    assert usage[0]['usage']['output_tokens'] == 7


@pytest.mark.asyncio
async def test_runtime_resume_retains_coverage_usage_and_consumed_requests(tmp_path: Path) -> None:
    from nare import ToolCall

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.evidence import EvidenceStore
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime
    from tests.unit.test_376_agent_evidence import source

    started = asyncio.Event()

    class ReadsThenWaits(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            if self.requests:
                started.set()
                await asyncio.Future()
            self.requests.append({})
            call = ToolCall('evidence-1', 'review_evidence', {})
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': {}}],
                         [call], Usage(input=100, output=10), 'tool_use')

    budget = AttemptBudget(max_calls=4)
    disk = CheckpointStore(tmp_path)
    catalog = EvidenceStore((source('evidence'),), required=frozenset({'nested/custom.input'}))
    req = replace(request(admit), budget=budget, checkpoint_store=disk,
                  evidence_catalog=catalog, allowed_tools=(catalog.tool(),))
    task = asyncio.create_task(NareRuntime(transport=ReadsThenWaits('')).run(req))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    stopped = await asyncio.wait_for(task, timeout=2)
    assert stopped.status == 'cancelled'
    assert stopped.checkpoint_key is not None
    assert budget.calls == 2
    fresh = EvidenceStore((source('evidence'),), required=frozenset({'nested/custom.input'}))
    resumed = await NareRuntime(transport=ScriptedTransport('{"accepted":true}')).run(replace(req,
        checkpoint_key=stopped.checkpoint_key, evidence_catalog=fresh, allowed_tools=(fresh.tool(),)))
    assert resumed.status == 'accepted'
    assert fresh.remaining_required == 0
    assert resumed.usage['input_tokens'] == 111
    assert budget.usage['input_tokens'] == 111
    assert budget.calls == 3
    assert budget.agent_usage_unknown == 1


@pytest.mark.asyncio
async def test_stream_disconnect_retries_without_replaying_partial_output() -> None:
    from nare.transport import StreamDropped

    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    class Disconnects(ScriptedTransport):
        streaming = True

        async def stream_turn(self, messages: Any, tools: Any):
            self.requests.append({})
            if len(self.requests) == 1:
                yield Delta('progress', 'partial output is not a complete tool invocation')
                raise StreamDropped('synthetic stream interruption')
            yield Reply([{'type': 'text', 'text': '{"accepted":true}'}], [], Usage(input=13, output=4), 'end_turn')

    budget = AttemptBudget(max_calls=2)
    result = await NareRuntime(transport=Disconnects(''), retry=RetryPolicy(retries=1, seconds=10, sleep=no_wait)).run(
        replace(request(admit), budget=budget))
    assert result.status == 'accepted'
    assert budget.calls == 2
    assert budget.calls_completed == 1
    assert budget.usage['input_tokens'] == 13
    assert budget.agent_usage_unknown == 1


@pytest.mark.asyncio
async def test_failed_reservation_persistence_prevents_inference() -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    async def unavailable() -> None:
        raise OSError('private storage location')

    budget = AttemptBudget()
    budget.checkpoint = unavailable
    transport = ScriptedTransport('{"accepted":true}')
    result = await NareRuntime(transport=transport).run(replace(request(admit), budget=budget))
    assert result.status == 'held'
    assert result.hold_code == 'attempt_persistence'
    assert not transport.requests
    assert 'private storage' not in repr(result)


def test_pre_native_journal_retains_existing_usage_diagnostics(tmp_path: Path) -> None:
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=100)
    budget.usage = {'input_tokens': 321, 'output_tokens': 12}
    budget.calls_completed = 2
    save_attempt(cache, budget)
    cache.environment_attempt.pop('usage')
    cache.environment_attempt.pop('calls_completed')
    restored = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=200)
    assert restored.usage == {'input_tokens': 321, 'output_tokens': 12}
    assert restored.calls_completed == 2


@pytest.mark.asyncio
async def test_phase_call_reservations_survive_domain_restart(tmp_path: Path) -> None:
    from coordinare.services.agent_runtime.accounting import BudgetBridge
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=100)
    await BudgetBridge(budget, phase='review', phase_key='review-a', phase_limit=1).reserve()
    save_attempt(cache, budget)
    restored = begin_attempt(cache, DiscoveryLimits(), outer_seconds=600, now=200)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await BudgetBridge(restored, phase='review', phase_key='review-a', phase_limit=1).reserve()
    assert caught.value.code == 'phase_call_limit'
    assert restored.calls == 1
    await BudgetBridge(restored, phase='review', phase_key='review-b', phase_limit=1).reserve()
    assert restored.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('window', [None, 0])
async def test_missing_context_metadata_holds_before_nare_default_or_model_request(window: int | None) -> None:
    from coordinare.services.agent_runtime.nare_runtime import NareRuntime

    class UnknownWindow(ScriptedTransport):
        async def context_window(self) -> int | None:
            return window
    transport = UnknownWindow('{"accepted":true}')
    req = request(admit)
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'held' and result.hold_code == 'agent_context_unavailable'
    assert not transport.requests and req.budget.calls == 0
    assert req.budget.agent_usage_unknown == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(('native_reason', 'domain_reason'), [
    ('max_tokens', 'length'), ('end_turn', 'stop'), ('tool_use', 'tool_calls'),
    ('refusal', 'content_filter'), ('stop_sequence', 'stop'), ('repetition', 'repetition'),
])
async def test_native_finish_reasons_reach_domain_diagnostics_once(native_reason: str, domain_reason: str) -> None:
    from coordinare.services.agent_runtime.accounting import BudgetBridge

    budget = AttemptBudget()
    bridge = BudgetBridge(budget, phase='review')
    reply = Reply([], [], Usage(input=17, output=9), native_reason)
    await bridge.record_reply('physical-1', reply)
    await bridge.record_reply('physical-1', reply)
    assert budget.finish_reasons == {domain_reason: 1}
    assert budget.call_stats['review']['length_stops'] == (domain_reason == 'length')
    assert budget.last_finish_reason == domain_reason
    assert reply.stop_reason == native_reason  # The native loop still owns its vocabulary.


@pytest.mark.asyncio
async def test_native_truncation_reason_reaches_the_call_record() -> None:
    from datetime import UTC, datetime

    from coordinare.services.agent_runtime.accounting import (
        BudgetBridge,
        BudgetTransport,
        TransportContext,
    )
    from coordinare.services.conductor_calls import CallRecord

    native_reply = Reply([{'type': 'text', 'text': '{'}], [], Usage(input=11, output=7), 'max_tokens')

    async def save() -> None:
        pass

    budget = AttemptBudget()
    transport = BudgetTransport(ScriptedTransport(''), BudgetBridge(budget, phase='review'),
        TransportContext(session_id='fixture', before_send=save))
    record = CallRecord(kind='environment_review', card_id=None, symphony='fixture', call_id='physical-1',
                        started_at=datetime.now(UTC))
    await transport._reply('physical-1', record, native_reply)
    assert record.reply is not None and record.reply.finish_reason == 'length'
    assert budget.finish_reasons == {'length': 1}
    assert native_reply.stop_reason == 'max_tokens'


@pytest.mark.asyncio
async def test_provider_schema_rejection_precedes_any_reserved_request() -> None:
    from nare import new_session, run
    from nare.transport.anthropic import check_response_schema

    from coordinare.services.agent_runtime.accounting import (
        BudgetBridge,
        BudgetTransport,
        TransportContext,
    )

    class SchemaAware(ScriptedTransport):
        supports_structured_output = True

        def check_response_schema(self, schema: dict[str, Any]) -> None:
            check_response_schema(schema)

    async def save() -> None:
        pass

    budget = AttemptBudget()
    inner = SchemaAware('{}')
    wrapped = BudgetTransport(inner, BudgetBridge(budget, phase='review'),
        TransportContext(session_id='fixture', before_send=save))
    schema = {'type': 'object', 'additionalProperties': {'type': 'string'}}
    with pytest.raises(ValueError, match='a map at the root'):
        async for _ in run(new_session('Return an object.'), transport=wrapped,
                           schema=schema, structured_output='json_schema', tools=()):
            pass
    assert budget.calls == 0 and inner.requests == []
