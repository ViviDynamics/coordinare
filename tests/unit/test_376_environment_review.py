"""Native review keeps whole evidence, independent roles and real domain admission."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from functools import partial
from typing import Any

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from nare import Reply, ToolCall, Usage

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.evidence import EvidenceSource
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import _url_checked_assessment
from coordinare.services.environment_discovery_prompts import assessment_envelope_schema
from tests.unit.test_351_environment_discovery import PATH, SOURCE, candidate
from tests.unit.test_353_review_findings_flow import finding, review


class ReviewerTransport:
    def __init__(self, answer: dict[str, Any], *, early: bool = True) -> None:
        self.answer, self.early = answer, early
        self.calls = 0
        self.received: dict[int, dict[str, Any]] = {}
        self.requests: list[Any] = []
        self.unread = 1
        self.navigation = False

    async def context_window(self) -> int:
        return 262144

    async def turn(self, messages: Any, tools: Any) -> Reply:
        self.calls += 1
        self.requests.append(messages.copy())
        assert {tool['name'] for tool in tools} == ({'review_evidence', 'read_source', 'list_sources'}
                                                   if self.navigation else {'review_evidence'})
        for message in messages:
            for block in message.content:
                if block.get('type') == 'tool_result':
                    value = json.loads(block['content'])
                    self.unread = value['unread_count']
                    self.received.update({page['id']: page for page in value['pages']})
        if (self.early and self.calls == 1) or not self.unread:
            return Reply([{'type': 'text', 'text': json.dumps(self.answer)}], [], Usage(input=100, output=20), 'end_turn')
        call = ToolCall(str(self.calls), 'review_evidence', {})
        return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                     [call], Usage(input=100, output=20), 'tool_use')


def phase(tmp_path: Any, transport: ReviewerTransport, *, required: frozenset[str] = frozenset({PATH})) -> Any:
    from coordinare.services.environment_agent import EnvironmentPhaseRequest
    from coordinare.services.environment_discovery import native_review_instructions

    manifest = EnvironmentManifest.model_validate(candidate())
    sources = (EvidenceSource(PATH, SOURCE, hashlib.sha256(SOURCE.encode()).hexdigest()),
               EvidenceSource('other/optional', 'optional evidence', hashlib.sha256(b'optional evidence').hexdigest()))
    return EnvironmentPhaseRequest(
        attempt_id='attempt', phase='review', role='reviewer', source_commit=manifest.source_commit,
        domain_digests=(('manifest', manifest.digest()),), model_config_digest='a' * 64,
        instructions=native_review_instructions(), packet={'manifest': manifest.model_dump(mode='json')},
        sources=sources, required=required, envelope='assessment', schema=assessment_envelope_schema(),
        validate=partial(_url_checked_assessment, [], reviewer_reads=manifest.inputs, manifest=manifest, require_accept=False),
        runtime=NareRuntime(transport=transport), budget=AttemptBudget(max_schema_repairs=2),
        checkpoint_store=CheckpointStore(tmp_path), phase_timeout_seconds=10,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', ['accept', 'revise', 'indeterminate'])
async def test_real_domain_verdict_after_complete_tool_delivery(tmp_path: Any, verdict: str) -> None:
    from coordinare.services.environment_agent import run_environment_phase

    transport = ReviewerTransport(review(verdict=verdict, findings=[] if verdict == 'accept' else [finding()]))
    request = phase(tmp_path, transport)
    result = await run_environment_phase(request)
    assert result.value.verdict == verdict
    assert result.value.manifest_digest == request.domain_digests[0][1]
    assert result.agent.status == 'accepted'  # Admitted assessment, not environment readiness.
    assert result.data == transport.answer['assessment']
    assert request.budget.schema_repairs == 1
    assert result.evidence.remaining_required == 0
    delivered = {page['source'] for page in transport.received.values()}
    assert delivered == {PATH, '@phase-packet'}  # Scoped requirement does not grow.
    source = ''.join(page['content'] for page in transport.received.values() if page['source'] == PATH)
    assert source == SOURCE
    assert not any(message.role == 'assistant' for message in transport.requests[0])


@pytest.mark.asyncio
async def test_schema_and_domain_criteria_are_preserved(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase
    from coordinare.services.environment_discovery import EXECUTABLE_REVIEW_CRITERIA
    from coordinare.services.environment_discovery_prompts import SOURCE_CRITERIA

    invalid = review()
    invalid['assessment']['sources'] = ['not/read']
    transport = ReviewerTransport(invalid, early=False)
    request = phase(tmp_path, transport)
    assert EXECUTABLE_REVIEW_CRITERIA in request.instructions
    assert SOURCE_CRITERIA in request.instructions
    assert '"tool":"read_file"' not in request.instructions
    assert 'Choose one source tool request' not in request.instructions
    assert request.schema == assessment_envelope_schema()
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(request)
    assert caught.value.code == 'review_citations_invalid'


@pytest.mark.asyncio
async def test_author_checkpoint_cannot_be_reused_for_reviewer(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase

    transport = ReviewerTransport(review(), early=False)
    request = phase(tmp_path, transport)
    authored = await run_environment_phase(replace(request, role='author'))
    calls = transport.calls
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(replace(request, checkpoint_key=authored.agent.checkpoint_key))
    assert caught.value.code == 'agent_checkpoint_identity'
    assert transport.calls == calls


@pytest.mark.asyncio
async def test_phase_deadline_preserves_outer_budget(tmp_path: Any) -> None:
    import asyncio

    from coordinare.services.environment_agent import run_environment_phase

    class Waiting(ReviewerTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            await asyncio.sleep(10)
            return await super().turn(messages, tools)

    request = replace(phase(tmp_path, Waiting(review())), phase_timeout_seconds=0.01)
    deadline = request.budget.deadline
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(request)
    assert caught.value.code == 'phase_deadline'
    assert request.budget.deadline == deadline


@pytest.mark.asyncio
@pytest.mark.parametrize('retained_finding', [False, True])
async def test_production_review_uses_native_runtime_and_independent_fetch(tmp_path: Any, retained_finding: bool) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from coordinare_service_inference.environment.findings import FindingLedger

    from coordinare.services.environment_agent import EnvironmentAgentContext
    from coordinare.services.environment_discovery import DiscoveryLimits, _review
    from tests.unit.test_353_review_findings_contract import ledger_data
    from tests.unit.test_353_review_findings_flow import decision

    answer = review(decisions=[decision()] if retained_finding else [])
    transport = ReviewerTransport(answer)
    transport.navigation = True
    request = phase(tmp_path, transport)
    class SchemaCheckingRuntime(NareRuntime):
        async def run(self, agent_request: Any) -> Any:
            definitions = agent_request.answer_schema['$defs']
            decisions = definitions['CoverageAssessmentProposal']['properties']['decisions']
            assert decisions['minItems'] == decisions['maxItems'] == int(retained_finding)
            if retained_finding:
                for branch in definitions['FindingDecisionProposal']['anyOf']:
                    assert branch['properties']['finding_id']['enum'] == ['F0001']
            assert 'anyOf' not in agent_request.answer_schema
            return await super().run(agent_request)

    request.budget.agent_context = EnvironmentAgentContext(runtime=SchemaCheckingRuntime(transport=transport),
        attempt_id='attempt', model_config_digest='a' * 64, checkpoint_store=request.checkpoint_store)
    manifest = EnvironmentManifest.model_validate(candidate())
    request.budget.finding_ledger = FindingLedger(source_commit=manifest.source_commit)
    if retained_finding:
        data = ledger_data()
        data['source_commit'] = manifest.source_commit
        data['records'][0].update(source_commit=manifest.source_commit,
            manifest_digest=manifest.digest(), inputs=manifest.inputs)
        data['records'][0]['proposal']['sources'] = [PATH]
        data['reviews'] = [{'sequence': 1, 'manifest_digest': manifest.digest(), 'inputs': manifest.inputs,
                            'decisions': [], 'added_ids': ['F0001']}]
        request.budget.finding_ledger = FindingLedger.model_validate(data)
    fetch = AsyncMock(return_value=SOURCE)
    chat = AsyncMock(side_effect=AssertionError('Legacy transport must not run'))
    attempt = SimpleNamespace(budget=request.budget,
        discovery=SimpleNamespace(finding_responses=[]), inventory=[PATH], fetch=fetch,
        bounds=DiscoveryLimits(), options=SimpleNamespace(entries=None, declared_services=None),
        construction=SimpleNamespace(observe_sources=AsyncMock()), missing_urls=[],
        identity={'commit': manifest.source_commit}, chat=chat)
    data, reads = await _review(attempt, None, manifest)
    assert data == answer['assessment']
    assert reads == manifest.inputs
    fetch.assert_awaited_once_with(PATH)
    chat.assert_not_called()
    assert transport.calls >= 3


@pytest.mark.asyncio
async def test_additional_source_tools_deliver_whole_pages_before_admission() -> None:
    from coordinare.services.environment_agent import PinnedSourceTools
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection

    text = 'arbitrary source with unicode 漢字\n' * 3000
    calls = []
    async def fetch(path: str) -> str:
        calls.append(path)
        return text
    inspection = SourceInspection(['extra'], fetch, DiscoveryLimits())
    navigation = PinnedSourceTools(inspection)
    tool = next(tool for tool in navigation.tools() if tool.name == 'read_source')
    returned = []
    while True:
        result = json.loads(await tool.call({'path': 'extra'}))
        assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':'))) <= 30000
        returned.extend(page['content'] for page in result['pages'])
        if result['unread_count'] == 0:
            break
        assert navigation.issues()
    assert not navigation.issues()
    assert ''.join(returned) == text
    assert calls == ['extra']


@pytest.mark.asyncio
async def test_native_phase_limit_does_not_spend_other_phases_calls(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase

    transport = ReviewerTransport(review())
    request = replace(phase(tmp_path, transport), max_model_calls=1)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(request)
    assert caught.value.code == 'phase_call_limit'
    assert transport.calls == 1
    assert request.budget.calls == 1 < request.budget.max_calls


@pytest.mark.asyncio
async def test_native_extra_read_notifies_domain_scope_observer() -> None:
    from unittest.mock import AsyncMock

    from coordinare.services.environment_agent import PinnedSourceTools
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection

    inspection = SourceInspection(['extra'], AsyncMock(return_value='new evidence'), DiscoveryLimits())
    observe = AsyncMock(side_effect=EnvironmentDiscoveryError('Source scope reopened', code='scope_reopened'))
    navigation = PinnedSourceTools(inspection, observe_sources=observe)
    with pytest.raises(EnvironmentDiscoveryError):
        await navigation.read({'path': 'extra'})
    observe.assert_awaited_once_with(inspection.inputs)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        navigation.issues()
    assert caught.value.code == 'scope_reopened'


@pytest.mark.asyncio
async def test_runtime_progress_reports_delivered_pages_without_source_text(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase

    events = []
    async def sink(event: Any) -> None:
        events.append(event)
    result = await run_environment_phase(replace(phase(tmp_path, ReviewerTransport(review())), event_sink=sink))
    terminal = events[-1]
    assert terminal['kind'] == 'terminal'
    assert terminal['evidence_delivered'] == len(result.evidence.delivered)
    assert terminal['evidence_remaining'] == 0
    assert SOURCE not in json.dumps(events)


@pytest.mark.asyncio
async def test_checkpoint_refuses_rewound_phase_allowance(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase

    transport = ReviewerTransport(review())
    request = replace(phase(tmp_path, transport), max_model_calls=1)
    with pytest.raises(EnvironmentDiscoveryError):
        await run_environment_phase(request)
    [reference] = request.budget.agent_checkpoints.values()
    request.budget.agent_phase_calls.clear()
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(replace(request, checkpoint_key=reference.key))
    assert caught.value.code == 'agent_checkpoint_accounting'
    assert transport.calls == 1
