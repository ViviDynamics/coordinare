"""Native corrections must deliver the domain's actionable, sanitized details."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_admission import _validation_feedback
from coordinare.services.environment_agent import EnvironmentPhaseRequest, run_environment_phase
from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_scope_mapping import mapped_manifest
from tests.unit.test_376_bootstrap_phases import PhaseTransport


class CorrectionTransport(PhaseTransport):
    def __init__(self, bad: dict[str, Any], good: dict[str, Any]) -> None:
        super().__init__({'manifest': bad})
        self.good = good
        self.proposed = False
        self.feedback = ''

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Any:
        if self.proposed:
            self.feedback = '\n'.join(block.get('text', '') for block in messages[-1].content)
            self.answer = {'manifest': self.good}
        reply = await super().turn(messages, tools, tool_choice=tool_choice)
        self.proposed |= reply.stop_reason == 'end_turn'
        return reply


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing', 'invalid', 'collision'])
async def test_domain_error_details_reach_the_next_native_model_request(mutation: str) -> None:
    good, criterion_id = await mapped_manifest()
    bad = deepcopy(good)
    if mutation == 'missing':
        bad['criterion_checks'] = {}
    elif mutation == 'invalid':
        bad['criterion_checks'][criterion_id] = ['absent-check']
    else:
        bad['requirements'][0]['id'] = 'operational'
    transport = CorrectionTransport(bad, good)
    budget = AttemptBudget(max_schema_repairs=1)
    request = EnvironmentPhaseRequest(
        attempt_id='correction', phase='discovery', role='author', source_commit=good['source_commit'],
        domain_digests=(), model_config_digest='a' * 64, instructions='Declare the manifest.',
        packet={}, sources=(), required=frozenset(), envelope='manifest',
        schema={'type': 'object', 'properties': {'manifest': {'type': 'object'}}, 'required': ['manifest']},
        validate=EnvironmentManifest.model_validate, rejection_errors=_validation_feedback,
        runtime=NareRuntime(transport=transport), budget=budget, phase_timeout_seconds=10,
    )
    result = await run_environment_phase(request)
    assert result.value == EnvironmentManifest.model_validate(good)
    assert budget.schema_repairs == 1
    assert budget.calls == budget.calls_completed == 3
    if mutation == 'collision':
        assert 'record_id_collision' in transport.feedback
        assert json.dumps(['operational']) in transport.feedback
        assert 'unavailable' in transport.feedback
    else:
        assert 'scope_required_check_mapping' in transport.feedback
        assert criterion_id in transport.feedback
        assert ('missing_criteria' if mutation == 'missing' else 'invalid_criteria') in transport.feedback
    assert 'absent-check' not in transport.feedback  # Arbitrary candidate values stay excluded.


@pytest.mark.asyncio
async def test_strictly_improving_native_correction_is_not_charged_twice() -> None:
    from dataclasses import replace

    from nare import Reply, Usage

    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    class Improving(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
            replies = ['{}', '{"accepted":true}', '{"accepted":true,"reason":"verified"}']
            reply = replies[len(self.requests)]
            self.requests.append({'messages': messages.copy()})
            return Reply([{'type': 'text', 'text': reply}], [], Usage(input=10, output=5), 'end_turn')

    async def admit(answer: Any) -> list[Any]:
        return []

    budget = AttemptBudget(max_calls=3, max_schema_repairs=2)
    schema = {'type': 'object', 'properties': {'accepted': {'type': 'boolean'}, 'reason': {'type': 'string'}},
              'required': ['accepted', 'reason'], 'additionalProperties': False}
    result = await NareRuntime(transport=Improving('')).run(replace(request(admit), budget=budget, answer_schema=schema))
    assert result.status == 'accepted'
    assert budget.calls == 3
    assert budget.schema_repairs == 1
    assert 'correction_progress' in budget.validation_codes


@pytest.mark.asyncio
@pytest.mark.parametrize(('native_reason', 'hold_code'), [
    ('max_tokens', 'agent_output_truncated'), ('refusal', 'agent_provider_refusal'),
])
async def test_native_terminal_reason_survives_domain_classification(native_reason: str, hold_code: str) -> None:
    from nare import Reply, Usage

    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    class Unfinished(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
            return Reply([], [], Usage(input=10, output=5), native_reason)

    async def admit(answer: Any) -> list[Any]:
        return []

    result = await NareRuntime(transport=Unfinished('')).run(request(admit))
    assert result.status == 'held'
    assert result.answer is None
    assert result.hold_code == hold_code


@pytest.mark.asyncio
@pytest.mark.parametrize('shard', [False, True])
async def test_cancellation_before_correction_event_keeps_completed_charge(tmp_path: Any, shard: bool) -> None:
    import asyncio
    from dataclasses import replace

    from nare import loads

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    async def admit(answer: Any) -> list[Any]:
        return []

    async def interrupt(event: dict[str, Any]) -> None:
        if event['calls'] == 1 and event['turn'] == 1 and event['kind'] == 'progress':
            raise asyncio.CancelledError

    disk = CheckpointStore(tmp_path)
    budget = AttemptBudget(max_calls=4, max_schema_repairs=1)
    req = replace(request(admit), budget=budget, checkpoint_store=disk, event_sink=interrupt,
                  shard_corrections=1 if shard else None)
    first = await NareRuntime(transport=ScriptedTransport('{}')).run(req)
    assert first.status == 'cancelled'
    saved = await disk.load_verified(first.checkpoint_key, req.identity)
    assert loads(saved.session_text).schema_corrections == 1
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1
    resumed = await NareRuntime(transport=ScriptedTransport('{}')).run(
        replace(req, checkpoint_key=first.checkpoint_key, event_sink=None))
    assert resumed.status == 'held' and resumed.hold_code == 'attempt_schema_limit'
    assert budget.calls == 2
    assert (budget.shard_schema_repairs if shard else budget.schema_repairs) == 1


@pytest.mark.asyncio
async def test_cancelled_queued_correction_cannot_create_a_zero_budget_resume(tmp_path: Any) -> None:
    import asyncio
    from dataclasses import replace

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from tests.unit.test_376_agent_runtime import ScriptedTransport, request

    async def admit(answer: Any) -> list[Any]:
        return []

    async def interrupt(event: dict[str, Any]) -> None:
        if event['calls'] == 1 and event['turn'] == 1 and event['kind'] == 'progress':
            raise asyncio.CancelledError

    req = replace(request(admit), checkpoint_store=CheckpointStore(tmp_path), event_sink=interrupt)
    first = await NareRuntime(transport=ScriptedTransport('{}')).run(req)
    assert first.status == 'held' and first.hold_code == 'attempt_schema_limit'
    assert req.budget.schema_repairs == 0
    resumed = await NareRuntime(transport=ScriptedTransport('{}')).run(
        replace(req, checkpoint_key=first.checkpoint_key, event_sink=None))
    assert resumed.status == 'held' and req.budget.calls == 1
