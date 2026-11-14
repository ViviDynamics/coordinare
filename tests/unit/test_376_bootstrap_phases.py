"""Native phase boundaries preserve domain limits, independent roles and admission."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from nare import Reply, ToolCall, Usage

from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import EnvironmentAgentContext
from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_376_agent_accounting import admit
from tests.unit.test_376_agent_runtime import ScriptedTransport, request


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [' ' * 129 + '{"accepted":true}', '{"accepted":true,"note":"' + '漢' * 45 + '"}'])
async def test_oversized_native_reply_charges_usage_and_cannot_reach_admission(text: str) -> None:
    checked = []
    async def check(value: Any) -> list[Any]:
        checked.append(value)
        return []
    transport = ScriptedTransport(text)
    req = replace(request(check), max_output_bytes=128)
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'held' and result.hold_code == 'phase_output_limit'
    assert not checked
    assert len(transport.requests) == req.budget.calls == req.budget.calls_completed == 1
    assert req.budget.usage['input_tokens'] == 11
    assert req.budget.agent_usage_unknown == 0


@pytest.mark.asyncio
async def test_native_context_cap_includes_system_tools_and_schema_before_inference() -> None:
    transport = ScriptedTransport('{"accepted":true}')
    req = replace(request(admit), max_context_bytes=128, instructions='漢' * 100)
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'held' and result.hold_code == 'review_context_limit'
    assert not transport.requests and req.budget.calls_completed == 0
    assert req.budget.agent_usage_unknown == 0


class PhaseTransport:
    def __init__(self, answer: dict[str, Any], *, read_path: str | None = None) -> None:
        self.answer, self.read_path = answer, read_path
        self.calls = 0
        self.delivered = False
        self.read = False
        self.messages: list[Any] = []
        self.packet_pages: dict[int, str] = {}

    async def context_window(self) -> int:
        return 262144

    async def turn(self, messages: Any, tools: Any) -> Reply:
        self.calls += 1
        self.messages.append(messages.copy())
        names = {b['id']: b['name'] for m in messages for b in m.content if b.get('type') == 'tool_use'}
        for message in messages:
            for block in message.content:
                if block.get('type') == 'tool_result':
                    value = json.loads(block['content'])
                    if value.get('pages'):
                        if names.get(block['tool_use_id']) == 'review_evidence':
                            self.delivered = value['unread_count'] == 0
                        self.packet_pages.update({p['offset']: p['content'] for p in value['pages']
                                                  if p['source'] == '@phase-packet'})
                        self.read |= any(p['source'] == self.read_path for p in value['pages'])
        if not self.delivered:
            call = ToolCall(str(self.calls), 'review_evidence', {})
        elif self.read_path and not self.read:
            call = ToolCall(str(self.calls), 'read_source', {'path': self.read_path})
        else:
            return Reply([{'type': 'text', 'text': json.dumps(self.answer)}], [], Usage(input=100, output=20), 'end_turn')
        assert call.name in {tool['name'] for tool in tools}
        return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                     [call], Usage(input=100, output=20), 'tool_use')


@pytest.mark.asyncio
async def test_scope_author_and_reviewer_use_fresh_native_sessions(tmp_path: Any) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from tests.unit.test_353_scope_contract import COMMIT, criterion, proposal, review, run

    phases = []
    transports = []
    class Runtime:
        async def run(self, req: Any) -> Any:
            phases.append(req)
            assert '"tool":"read_file"' not in req.instructions
            answer = proposal(criterion()) if req.identity.role == 'author' else review()
            transport = PhaseTransport(answer, read_path='guide')
            transports.append(transport)
            result = await NareRuntime(transport=transport).run(req)
            assert result.status == 'accepted', (result, transport.messages[-1])
            return result
    budget = AttemptBudget()
    budget.agent_context = EnvironmentAgentContext(runtime=Runtime(), attempt_id='scope-attempt',
        model_config_digest='a'*64, checkpoint_store=CheckpointStore(tmp_path))
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Native scope must not invoke legacy chat')
    scope = await run(legacy, budget=budget)
    assert scope.definition.source_commit == COMMIT
    assert scope.source_assessment.assessment.verdict == 'accept'
    assert [r.identity.role for r in phases] == ['author', 'reviewer']
    assert [r.identity.phase for r in phases] == ['scope_author', 'scope_review']
    assert all(not any(m.role == 'assistant' for m in t.messages[0]) for t in transports)
    assert scope.review_inputs == scope.definition.inputs


@pytest.mark.asyncio
async def test_unsupported_schema_refuses_before_spending_a_model_call() -> None:
    transport = ScriptedTransport('{"accepted":true}')
    req = replace(request(admit), answer_schema={'type': 'string', 'format': 'email'})
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'held' and result.hold_code == 'agent_schema_unsupported'
    assert not transport.requests and req.budget.calls == 0


@pytest.mark.asyncio
async def test_sharded_check_author_uses_own_correction_budget_and_preserves_mapping() -> None:
    from functools import partial

    from coordinare_service_inference.environment.findings import FindingLedger

    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_discovery_prompts import NATIVE_SOURCE_INSTRUCTIONS
    from coordinare.services.environment_inspection import SourceInspection
    from coordinare.services.environment_shard_scope import AgentContext, AgentTask, run_agent
    from tests.unit.test_353_sharded_executable import (
        SUMMARY,
        CheckGroup,
        group_reply,
        make_criterion,
        validate_group,
    )

    group = CheckGroup('c00-', (make_criterion(1),))
    answer = group_reply(group)
    bad = group_reply(group, criterion_checks={})
    class Correcting(PhaseTransport):
        async def turn(self, messages: Any, tools: Any) -> Reply:
            reply = await super().turn(messages, tools)
            if reply.stop_reason == 'end_turn':
                self.answer = answer
            return reply
    transport = Correcting(bad)
    budget = AttemptBudget(max_schema_repairs=0)
    budget.scope_findings = FindingLedger(source_commit='a'*40)
    budget.agent_context = EnvironmentAgentContext(runtime=NareRuntime(transport=transport),
        attempt_id='checks', model_config_digest='a'*64)
    async def fetch(path: str) -> str:
        return 'The pinned obligation needs a consumer check.'
    inspection = SourceInspection(['src/001.md'], fetch, DiscoveryLimits(), budget)
    inspection.name = 'executable_checks'
    await inspection.read('src/001.md')
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Sharded native author must not invoke legacy chat')
    part = await run_agent(AgentContext(budget, inspection.limits, legacy), inspection,
        [{'role': 'system', 'content': NATIVE_SOURCE_INSTRUCTIONS}, {'role': 'user', 'content': '{}'}],
        AgentTask('shard_checks', partial(validate_group, group=group, summary=SUMMARY)))
    assert part.criterion_checks == answer['shard_checks']['criterion_checks']
    assert budget.shard_schema_repairs == 1
    assert budget.schema_repairs == budget.coverage_revisions == budget.execution_repairs == 0


@pytest.mark.asyncio
async def test_complete_native_pipeline_executes_bound_probe_once_before_manifest(tmp_path: Any) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.environment_discovery import discover_and_review
    from coordinare.services.environment_probes import ProbeReport
    from tests.unit.test_353_discovery_repair import (
        CAPABILITIES,
        COMMIT,
        PATH,
        SOURCE,
        assessment,
        candidate,
    )
    from tests.unit.test_353_environment_probes import report_data, request_data
    from tests.unit.test_353_scope_contract import criterion, proposal, review

    executed = []
    phases = []
    budget = AttemptBudget(max_calls=30)
    async def execute(plan: Any) -> Any:
        executed.append(plan.digest())
        return ProbeReport.model_validate(report_data(plan))
    async def fetch(path: str) -> str:
        return SOURCE
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Complete native bootstrap must not use legacy chat')
    class Author(PhaseTransport):
        probes = 0
        async def turn(self, messages: Any, tools: Any) -> Reply:
            reply = await super().turn(messages, tools)
            if reply.stop_reason == 'end_turn' and self.probes < 2:
                self.probes += 1
                call = ToolCall(f'probe{self.probes}', 'probe', request_data())
                return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                             [call], Usage(input=100, output=20), 'tool_use')
            return reply
    class Runtime:
        async def run(self, req: Any) -> Any:
            phases.append(req.identity.phase)
            assert '"tool":"read_file"' not in req.instructions
            if req.identity.phase == 'scope_author':
                answer = proposal(criterion(PATH))
            elif req.identity.phase == 'scope_review':
                answer = review(sources=[PATH])
            elif req.identity.phase == 'review':
                assert len(executed) == 1
                answer = assessment(candidate())
            else:
                manifest = candidate()
                manifest['criterion_checks'] = {c.id: ['version'] for c in budget.scope_contract.definition.criteria}
                answer = {'manifest': manifest, 'finding_responses': []}
            transport = (Author if req.identity.phase == 'discovery' else PhaseTransport)(answer, read_path=PATH)
            result = await NareRuntime(transport=transport).run(req)
            if result.status != 'accepted':
                from tests.unit.scope_chat_fixture import FixtureAssertionError

                raise FixtureAssertionError(f'{req.identity.phase}: {result.hold_code}')
            return result
    budget.agent_context = EnvironmentAgentContext(runtime=Runtime(), attempt_id='pipeline',
        model_config_digest='a'*64, checkpoint_store=CheckpointStore(tmp_path))
    manifest, judged = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
        fetch=fetch, chat=legacy, capabilities=CAPABILITIES, budget=budget, probe_executor=execute, require_preflight=True)
    assert phases == ['scope_author', 'scope_review', 'discovery', 'review']
    assert judged.verdict == 'accept' and judged.manifest_digest == manifest.digest()
    assert len(executed) == budget.probe_batches == len(budget.probe_reports) == 1
    assert manifest.criterion_checks


@pytest.mark.asyncio
async def test_native_repair_trial_is_deduplicated_and_cannot_supply_artifacts(tmp_path: Any) -> None:
    from types import SimpleNamespace

    from coordinare_service_inference.environment.findings import FindingLedger
    from coordinare_service_inference.environment.models import EnvironmentManifestProposal

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.environment_discovery import (
        DiscoveryLimits,
        _attach_tools,
        _Inspection,
        _phase,
    )
    from tests.unit.test_353_discovery_repair import COMMIT, PATH, SOURCE, candidate
    from tests.unit.test_353_repair_trial import report

    budget = AttemptBudget(execution_repairs=1)
    executed = []
    class Author(PhaseTransport):
        trials = 0
        async def turn(self, messages: Any, tools: Any) -> Reply:
            reply = await super().turn(messages, tools)
            if reply.stop_reason == 'end_turn' and self.trials < 2:
                self.trials += 1
                call = ToolCall(f'trial{self.trials}', 'trial', {'checks': [{'id': 'smoke', 'argv': ['true']}]})
                return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                             [call], Usage(input=100, output=20), 'tool_use')
            return reply
    transport = Author({'manifest': candidate(), 'finding_responses': []}, read_path=PATH)
    budget.finding_ledger = FindingLedger(source_commit=COMMIT)
    budget.agent_context = EnvironmentAgentContext(runtime=NareRuntime(transport=transport), attempt_id='trial',
        model_config_digest='a'*64, checkpoint_store=CheckpointStore(tmp_path))
    async def fetch(path: str) -> str:
        return SOURCE
    async def execute(request: Any) -> Any:
        executed.append(request)
        return report({'id': 'smoke', 'status': 'pass', 'required': True})
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Repair author must use native trial')
    inspection = _Inspection('discovery', [PATH], fetch, DiscoveryLimits(), budget)
    await inspection.read(PATH)
    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    _attach_tools(inspection, SimpleNamespace(probe_executor=None, require_preflight=False, trial_executor=execute),
                  COMMIT, messages, repair=True)
    assert '"trial":{' not in json.loads(messages[1]['content'])['repair_trial']
    assert 'native trial tool' in json.loads(messages[1]['content'])['repair_trial']
    data, _ = await _phase(inspection=inspection, messages=messages, chat=legacy, budget=budget,
                           validate=EnvironmentManifestProposal.model_validate)
    assert data == candidate()
    assert len(executed) == budget.trial_runs == 1
    observations = [json.loads(block['content']) for message in transport.messages[-1] for block in message.content
                    if block.get('type') == 'tool_result' and 'observation_id' in json.loads(block['content'])]
    assert observations
    for batch in observations:
        content = ''.join(page['content'] for page in batch['pages'])
        assert 'runtime_artifacts' not in content and 'trial_artifacts' not in content


@pytest.mark.asyncio
async def test_domain_normalization_preserves_native_answer_identity(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase
    from tests.unit.test_353_review_findings_flow import review
    from tests.unit.test_376_environment_review import ReviewerTransport, phase

    req = phase(tmp_path, ReviewerTransport(review(), early=False))
    def normalize(data: dict[str, Any]) -> Any:
        data['rationale'] = 'Domain-normalized rationale'
        return req.validate(data)
    result = await run_environment_phase(replace(req, validate=normalize))
    assert result.agent.status == 'accepted'
    assert result.data['rationale'] == 'Domain-normalized rationale'
    assert result.value.verdict == 'accept'


@pytest.mark.asyncio
@pytest.mark.parametrize('recover', [False, True])
async def test_native_preflight_has_one_free_reminder_then_holds_or_executes(tmp_path: Any, recover: bool) -> None:
    from types import SimpleNamespace

    from coordinare_service_inference.environment.findings import FindingLedger
    from coordinare_service_inference.environment.models import EnvironmentManifestProposal

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import (
        DiscoveryLimits,
        _attach_tools,
        _Inspection,
        _phase,
    )
    from coordinare.services.environment_probes import ProbeReport
    from tests.unit.test_353_discovery_repair import COMMIT, PATH, SOURCE, candidate
    from tests.unit.test_353_environment_probes import report_data, request_data

    budget = AttemptBudget(max_schema_repairs=0)
    budget.finding_ledger = FindingLedger(source_commit=COMMIT)
    executed = []
    class Author(PhaseTransport):
        answers = 0
        async def turn(self, messages: Any, tools: Any) -> Reply:
            reply = await super().turn(messages, tools)
            if reply.stop_reason == 'end_turn':
                self.answers += 1
                if self.answers == 2 and recover:
                    call = ToolCall('probe', 'probe', request_data())
                    assert call.name in {tool['name'] for tool in tools}
                    return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                        [call], Usage(input=100, output=20), 'tool_use')
            return reply
    transport = Author({'manifest': candidate(), 'finding_responses': []})
    budget.agent_context = EnvironmentAgentContext(runtime=NareRuntime(transport=transport), attempt_id='preflight',
        model_config_digest='a'*64, checkpoint_store=CheckpointStore(tmp_path))
    async def fetch(path: str) -> str:
        return SOURCE
    async def execute(plan: Any) -> Any:
        executed.append(plan)
        return ProbeReport.model_validate(report_data(plan))
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Preflight must stay in the native session')
    inspection = _Inspection('discovery', [PATH], fetch, DiscoveryLimits(), budget)
    await inspection.read(PATH)
    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    from tests.unit.test_353_discovery_repair import CAPABILITIES

    _attach_tools(inspection, SimpleNamespace(probe_executor=execute, require_preflight=True, trial_executor=None,
                  execution_source_digest=None, capabilities=CAPABILITIES),
                  COMMIT, messages, repair=False)
    if recover:
        data, _ = await _phase(inspection=inspection, messages=messages, chat=legacy, budget=budget,
                               validate=EnvironmentManifestProposal.model_validate)
        assert data == candidate() and len(executed) == 1
    else:
        with pytest.raises(EnvironmentDiscoveryError, match='preflight') as caught:
            await _phase(inspection=inspection, messages=messages, chat=legacy, budget=budget,
                         validate=EnvironmentManifestProposal.model_validate)
        assert caught.value.code == 'preflight_required'
        assert transport.answers == 2 and not executed
    assert budget.schema_repairs == budget.shard_schema_repairs == 0


@pytest.mark.asyncio
async def test_real_sharded_scope_uses_native_tools_and_separate_triage_identities(tmp_path: Any) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from tests.unit.scope_chat_fixture import FixtureAssertionError
    from tests.unit.test_353_shard_scope import SOURCES, Router, run

    router = Router()
    phases = []
    class Runtime:
        async def run(self, req: Any) -> Any:
            if '"tool":"read_file"' in req.instructions:
                raise FixtureAssertionError('Sharded phase advertises obsolete source protocol')
            phases.append(req)
            class Routed(PhaseTransport):
                async def turn(self, messages: Any, tools: Any) -> Reply:
                    reply = await super().turn(messages, tools)
                    if reply.stop_reason == 'end_turn':
                        packet = json.loads(''.join(self.packet_pages[k] for k in sorted(self.packet_pages)))
                        # Only the synthetic provider uses the existing domain-proposal router.
                        # Production still crosses actual native tools, schema and admission.
                        original = [{'role': 'system', 'content': req.instructions}, *packet['context'],
                                    {'role': 'user', 'content': '{}'}]
                        answer = await router(original, response_schema=req.answer_schema)
                        return Reply([{'type': 'text', 'text': answer}], [], Usage(input=100, output=20), 'end_turn')
                    return reply
            result = await NareRuntime(transport=Routed({})).run(req)
            if result.status != 'accepted':
                raise FixtureAssertionError(f'{req.identity.phase}: {result.hold_code}')
            return result
    budget = AttemptBudget(max_calls=120, max_schema_repairs=0)
    budget.agent_context = EnvironmentAgentContext(runtime=Runtime(), attempt_id='sharded',
        model_config_digest='a'*64, checkpoint_store=CheckpointStore(tmp_path))
    async def legacy(*args: Any, **kwargs: Any) -> str:
        raise FixtureAssertionError('Sharded scope invoked legacy chat')
    scope = await run(legacy, budget=budget)
    assert set(scope.all_inputs) == set(SOURCES)
    assert scope.source_assessment.assessment.verdict == 'accept'
    triages = [r.identity for r in phases if r.identity.phase == 'scope_triage']
    assert len(triages) >= 2 and len(set(triages)) == len(triages)
    assert {'scope_shard_author', 'scope_shard_review', 'scope_merge_review'} <= {r.identity.phase for r in phases}
