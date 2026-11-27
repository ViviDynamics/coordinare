"""Invalid evidence calls stop through real native dispatch, across successful reads."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from nare import Reply, Tool, ToolCall, Usage

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import run_environment_phase
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_repair import begin_attempt, save_attempt
from tests.unit.test_351_environment_discovery import PATH, SOURCE
from tests.unit.test_353_execution_repair import cache_fixture
from tests.unit.test_376_agent_runtime import request
from tests.unit.test_376_environment_review import phase, review


def required_read_reply(owner: Any, choice: str | None) -> Reply | None:
    if choice != 'required':
        return None
    owner.required_calls = getattr(owner, 'required_calls', 0) + 1
    call = ToolCall('required-' + str(owner.required_calls), 'continue_required_reads', {})
    return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': {}}],
                 [call], Usage(input=10, output=5), 'tool_use')


class InvalidEvidenceTransport:
    required_tool_choice = 'required'
    def __init__(self, *, schema: bool, progress: bool = False) -> None:
        self.calls = 0
        self.schema = schema
        self.progress = progress

    async def context_window(self) -> int:
        return 262144

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        if (required := required_read_reply(self, tool_choice)) is not None:
            return required
        self.calls += 1
        if self.progress and self.calls == 5:
            name, args = 'review_evidence', {}
        else:
            name = 'review_evidence' if self.schema else 'reread_evidence'
            args = {'pages': [10000 + self.calls]}
        call = ToolCall(str(self.calls), name, args)
        return Reply([{'type': 'tool_use', 'id': call.id, 'name': name, 'input': args}],
                     [call], Usage(input=10, output=5), 'tool_use')


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [False, True], ids=['catalog-range', 'native-schema'])
@pytest.mark.parametrize('progress', [False, True], ids=['repeated-error', 'interspersed-progress'])
async def test_invalid_calls_have_monotonic_allowance_and_exhausted_resume_refuses(
    tmp_path: Path, schema: bool, progress: bool,
) -> None:
    transport = InvalidEvidenceTransport(schema=schema, progress=progress)
    req = replace(phase(tmp_path, transport), budget=AttemptBudget(max_calls=40, max_schema_repairs=2))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'agent_evidence_call_limit'
    assert transport.calls == 8 + int(progress)
    assert req.budget.agent_evidence_errors == 8
    assert req.budget.schema_repairs == 0
    assert len(req.budget.agent_evidence_error_receipts) == 8
    # Even a session that nare still considers working is terminal at this seam.
    again = InvalidEvidenceTransport(schema=schema)
    resumed = replace(phase(tmp_path, again), budget=req.budget)
    with pytest.raises(EnvironmentDiscoveryError) as resumed_error:
        await run_environment_phase(resumed)
    assert resumed_error.value.code == 'agent_evidence_call_limit'
    assert again.calls == 0


class RereadingTransport:
    required_tool_choice = 'required'
    def __init__(self) -> None:
        self.calls = 0

    async def context_window(self) -> int:
        return 262144

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        if (required := required_read_reply(self, tool_choice)) is not None:
            return required
        self.calls += 1
        if self.calls > 14:
            return Reply([{'type': 'text', 'text': json.dumps(review())}], [], Usage(input=10, output=5), 'end_turn')
        name = 'review_evidence' if self.calls <= 3 else 'reread_evidence'
        args = {} if name == 'review_evidence' else {'pages': [0]}
        call = ToolCall(str(self.calls), name, args)
        return Reply([{'type': 'tool_use', 'id': call.id, 'name': name, 'input': args}],
                     [call], Usage(input=10, output=5), 'tool_use')


@pytest.mark.asyncio
async def test_valid_rereads_do_not_spend_protocol_allowance(tmp_path: Path) -> None:
    transport = RereadingTransport()
    req = replace(phase(tmp_path, transport), budget=AttemptBudget(max_calls=30, max_schema_repairs=2))
    result = await run_environment_phase(req)
    assert result.agent.status == 'accepted'
    assert transport.calls == 15
    assert req.budget.agent_evidence_errors == 0


class BatchedFailureTransport(InvalidEvidenceTransport):
    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        if (required := required_read_reply(self, tool_choice)) is not None:
            return required
        self.calls += 1
        calls = [ToolCall(str(i), 'review_evidence', {'pages': [999]}) for i in range(8)]
        calls.append(ToolCall('action', 'approved_action', {}))
        return Reply([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.args} for c in calls],
                     calls, Usage(input=10, output=5), 'tool_use')


@pytest.mark.asyncio
async def test_a_reply_of_invalid_calls_is_one_strike(tmp_path: Path) -> None:
    """nare-native-046: a first reply of 20 identical invalid parallel calls was charged 20 strikes against eight, and
    the session held before it saw the refusal. Each reply is one strike; eight such replies still hold."""
    ran = []

    async def action(arguments: dict[str, Any]) -> str:
        ran.append('approved')
        return 'complete'

    tool = Tool('approved_action', 'An independently admitted synthetic action',
                {'type': 'object', 'properties': {}}, action)
    transport = BatchedFailureTransport(schema=True)
    req = replace(phase(tmp_path, transport), tools=(tool,), budget=AttemptBudget(max_calls=40))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'agent_evidence_call_limit'
    assert transport.calls == 8
    assert ran == ['approved'] * 8  # Native events arrive after the whole current batch.
    assert req.budget.agent_evidence_errors == 64
    assert list(req.budget.agent_evidence_phase_errors.values()) == [8]


@pytest.mark.asyncio
async def test_persisted_journal_preserves_spent_navigation_allowance(tmp_path: Path) -> None:
    transport = InvalidEvidenceTransport(schema=True)
    cache = cache_fixture(tmp_path)
    bounds = DiscoveryLimits(max_model_calls=40)
    budget = begin_attempt(cache, bounds, outer_seconds=600)
    req = replace(phase(tmp_path, transport), budget=budget)
    with pytest.raises(EnvironmentDiscoveryError):
        await run_environment_phase(req)
    save_attempt(cache, req.budget)
    restored = begin_attempt(cache, bounds, outer_seconds=600)
    assert restored.agent_evidence_errors == 8
    assert restored.agent_evidence_error_receipts == req.budget.agent_evidence_error_receipts
    again = InvalidEvidenceTransport(schema=True)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(replace(phase(tmp_path, again), budget=restored))
    assert caught.value.code == 'agent_evidence_call_limit'
    assert again.calls == 0


class RecoveringTransport(InvalidEvidenceTransport):
    def __init__(self, *, schema: bool) -> None:
        super().__init__(schema=schema)
        self.received: dict[int, dict[str, Any]] = {}

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        if (required := required_read_reply(self, tool_choice)) is not None:
            return required
        if not self.calls:
            return await super().turn(messages, tools)
        self.calls += 1
        remaining = 1
        for message in messages:
            for block in message.content:
                if block.get('type') == 'tool_result' and not block.get('is_error'):
                    value = json.loads(block['content'])
                    remaining = value['unread_count']
                    self.received.update({p['id']: p for p in value['pages']})
        if remaining:
            call = ToolCall(str(self.calls), 'review_evidence', {})
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                         [call], Usage(input=10, output=5), 'tool_use')
        return Reply([{'type': 'text', 'text': json.dumps(review())}], [], Usage(input=10, output=5), 'end_turn')


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [False, True])
async def test_invalid_call_recovers_to_real_domain_assessment_with_exact_evidence(tmp_path: Path, schema: bool) -> None:
    transport = RecoveringTransport(schema=schema)
    req = replace(phase(tmp_path, transport), budget=AttemptBudget(max_calls=40))
    result = await run_environment_phase(req)
    assert result.agent.status == 'accepted'
    assert result.value.verdict == 'accept'
    assert not result.evidence.remaining_required
    assert req.budget.agent_evidence_errors == 1
    assert ''.join(page['content'] for page in transport.received.values() if page['source'] == PATH) == SOURCE


@pytest.mark.asyncio
async def test_missing_prior_evidence_accounting_refuses_before_inference(tmp_path: Path) -> None:
    transport = InvalidEvidenceTransport(schema=True)
    budget = AttemptBudget(max_calls=40)
    budget.agent_evidence_protocol = 0
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(replace(phase(tmp_path, transport), budget=budget))
    assert caught.value.code == 'agent_evidence_policy'
    assert transport.calls == 0


class InterruptedBatchTransport(InvalidEvidenceTransport):
    def __init__(self, failures: int) -> None:
        super().__init__(schema=True)
        self.failures = failures

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        if (required := required_read_reply(self, tool_choice)) is not None:
            return required
        self.calls += 1
        calls = [ToolCall(str(i), 'review_evidence', {'pages': [999]}) for i in range(self.failures)]
        calls.append(ToolCall('cancel', 'interrupted_action', {}))
        return Reply([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.args} for c in calls],
                     calls, Usage(input=10, output=5), 'tool_use')


@pytest.mark.asyncio
@pytest.mark.parametrize('failures', [1, 8, 10])
async def test_interrupted_batch_preserves_completed_invalid_calls_before_checkpoint(
    tmp_path: Path, failures: int,
) -> None:
    async def interrupted(arguments: dict[str, Any]) -> str:
        raise asyncio.CancelledError

    tool = Tool('interrupted_action', 'Synthetic interruption', {'type': 'object'}, interrupted)
    transport = InterruptedBatchTransport(failures)
    req = replace(phase(tmp_path, transport), tools=(tool,), budget=AttemptBudget(max_calls=40))
    with pytest.raises(asyncio.CancelledError):
        await run_environment_phase(req)
    assert req.budget.agent_evidence_errors == failures
    assert len(req.budget.agent_evidence_error_receipts) == failures
    assert req.checkpoint_store is not None
    reference = next(iter(req.budget.agent_checkpoints.values()))
    saved = await req.checkpoint_store.load_verified(reference.key, reference.identity)
    assert saved.counters['agent_evidence_errors'] == failures
    assert saved.evidence_error_receipts == req.budget.agent_evidence_error_receipts
    # However many invalid calls the interrupted reply made, it was one strike: the session stays resumable, and the
    # calls it spent do not disappear.
    assert list(req.budget.agent_evidence_phase_errors.values()) == [1]
    with pytest.raises(asyncio.CancelledError):
        await run_environment_phase(req)
    assert req.budget.agent_evidence_errors == 2 * failures
    assert transport.calls == 2


@pytest.mark.asyncio
async def test_held_evidence_failure_cannot_resume_a_native_working_session(tmp_path: Path) -> None:
    async def broken(arguments: dict[str, Any]) -> str:
        raise RuntimeError('synthetic evidence failure')

    tool = Tool('review_evidence', 'Synthetic failed source', {'type': 'object', 'properties': {
        'pages': {'type': 'array', 'items': {'type': 'integer'}}}}, broken)
    transport = InvalidEvidenceTransport(schema=True)
    runtime = NareRuntime(transport=transport)
    req = replace(request(None), allowed_tools=(tool,), evidence_tools=frozenset({tool.name}),
                  budget=AttemptBudget(max_calls=40), checkpoint_store=CheckpointStore(tmp_path))
    first = await runtime.run(req)
    assert first.status == 'held' and first.hold_code == 'agent_evidence_tool_failed'
    assert first.checkpoint_key is not None
    resumed = await runtime.run(replace(req, checkpoint_key=first.checkpoint_key))
    assert resumed.status == 'held' and resumed.hold_code == first.hold_code
    assert transport.calls == 1


class DuplicateIdentityTransport(InvalidEvidenceTransport):
    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        if (required := required_read_reply(self, tool_choice)) is not None:
            return required
        self.calls += 1
        calls = [ToolCall('duplicate', 'review_evidence', {'pages': [999]}) for _ in range(8)]
        return Reply([{'type': 'tool_use', 'id': c.id, 'name': c.name, 'input': c.args} for c in calls],
                     calls, Usage(input=10, output=5), 'tool_use')

    async def stream_turn(self, messages: Any, tools: Any, **kwargs: Any):
        yield await self.turn(messages, tools, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('streaming', [False, True])
async def test_duplicate_tool_identities_refuse_before_dispatch(tmp_path: Path, streaming: bool) -> None:
    transport = DuplicateIdentityTransport(schema=True)
    transport.streaming = streaming
    req = replace(phase(tmp_path, transport), budget=AttemptBudget(max_calls=40))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(req)
    assert caught.value.code == 'agent_tool_identity'
    assert transport.calls == 1
    assert req.budget.agent_evidence_errors == 0
    assert req.budget.agent_usage_unknown == 0


def test_each_agent_session_has_its_own_invalid_call_allowance() -> None:
    """nare-native-042: a fresh divided scope ran 35 sessions; shard agents spent seven invalid calls, and the merge
    reviewer's first slip held the attempt. Another session's slips never spend this session's allowance."""
    from coordinare.services.agent_runtime.accounting import BudgetBridge
    from coordinare.services.agent_runtime.evidence_protocol import (
        MAX_INVALID_EVIDENCE_CALLS,
        EvidenceFailureBudget,
    )

    budget = AttemptBudget(max_calls=40)
    budget.agent_evidence_phase_errors['shard-session'] = MAX_INVALID_EVIDENCE_CALLS
    tools = frozenset({'read_source'})
    spent = EvidenceFailureBudget(BudgetBridge(budget, phase='scope_shard_review', phase_key='shard-session'), tools)
    fresh = EvidenceFailureBudget(BudgetBridge(budget, phase='scope_merge_review', phase_key='merge-session'), tools)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        spent.check()
    assert caught.value.code == 'agent_evidence_call_limit'
    fresh.check()


def test_per_session_invalid_calls_survive_the_attempt_journal() -> None:
    from coordinare.services.environment_repair import (
        RepairJournal,
        _restore_agent_accounting,
        _save_agent_accounting,
    )

    budget = AttemptBudget(max_calls=40)
    budget.agent_evidence_phase_errors = {'a': 3, 'b': 1}
    journal = RepairJournal.model_construct()
    _save_agent_accounting(journal, budget)
    restored = AttemptBudget(max_calls=40)
    _restore_agent_accounting(restored, journal)
    assert restored.agent_evidence_phase_errors == {'a': 3, 'b': 1}
