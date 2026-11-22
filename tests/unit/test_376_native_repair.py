"""Both delta and full domain repairs stay on native sessions and preserve frozen work."""
from __future__ import annotations

import json
from typing import Any

import pytest
from nare import Reply, Usage

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import EnvironmentAgentContext
from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.scope_chat_fixture import FixtureAssertionError
from tests.unit.test_353_delta_repair import delta_run
from tests.unit.test_353_discovery_repair import PATH, assessment, candidate
from tests.unit.test_353_scope_contract import criterion, proposal, review
from tests.unit.test_376_bootstrap_phases import PhaseTransport


@pytest.mark.asyncio
@pytest.mark.parametrize('fallback', [None, 'schema', 'domain'])
async def test_native_delta_restores_frozen_checks_and_full_escalation_stays_native(tmp_path: Any, fallback: str | None) -> None:
    budget = AttemptBudget(max_schema_repairs=1, max_calls=64)
    authored = []
    identities = []
    class Runtime:
        async def run(self, req: Any) -> Any:
            identities.append(req.identity)
            class Repair(PhaseTransport):
                async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
                    try:
                        return await self.answer_turn(messages, tools, tool_choice=tool_choice)
                    except Exception as exc:
                        raise FixtureAssertionError(f'{req.identity.phase}: {type(exc).__name__}: {exc}') from exc

                async def answer_turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
                    reply = await super().turn(messages, tools, tool_choice=tool_choice)
                    if reply.stop_reason != 'end_turn':
                        return reply
                    packet = json.loads(''.join(self.packet_pages[k] for k in sorted(self.packet_pages)))
                    context = json.loads(packet['context'][0]['content']) if 'context' in packet else packet
                    if req.identity.phase == 'scope_author':
                        answer = proposal(criterion(PATH))
                    elif req.identity.phase == 'scope_review':
                        answer = review(sources=[PATH])
                    elif req.identity.role == 'reviewer':
                        answer = assessment(candidate())
                    else:
                        mapping = {c.id: ['version'] for c in budget.scope_contract.definition.criteria}
                        if 'repair_delta' in context:
                            authored.append('delta')
                            if fallback == 'domain':
                                mapping = {identifier: ['missing-check'] for identifier in mapping}
                            answer = {} if fallback == 'schema' else {'manifest_delta': {'criterion_checks': mapping, 'checks': [
                                {'id': 'other', 'argv': ['zog', '--changed'], 'required': False},
                                # The failed check's timeout grows: left as it ran, the daemon refuses it.
                                {'id': 'version', 'argv': ['zog', '--version'], 'required': True, 'timeout_seconds': 600}]},
                                'finding_responses': []}
                        else:
                            authored.append('full')
                            full = candidate()
                            full['checks'].append({'id': 'other', 'argv': ['zog', '--other'], 'required': False})
                            full['criterion_checks'] = mapping
                            answer = {'manifest': full, 'finding_responses': []}
                    return Reply([{'type': 'text', 'text': json.dumps(answer)}], [], Usage(input=100, output=20), 'end_turn')
            return await NareRuntime(transport=Repair({}, read_path=PATH)).run(req)
    budget.agent_context = EnvironmentAgentContext(runtime=Runtime(), attempt_id='repair', model_config_digest='a'*64,
        checkpoint_store=CheckpointStore(tmp_path))
    async def legacy(*args: Any, **kwargs: Any) -> str:
        raise FixtureAssertionError('Delta-to-full repair invoked the old chat transport')
    manifest, assessed = await delta_run(legacy, budget=budget, executed=True)
    assert assessed.verdict == 'accept' and assessed.manifest_digest == manifest.digest()
    assert [check.argv for check in manifest.checks if check.id == 'other'] == [['zog', '--other']]
    assert bool(budget.delta_fallback) is bool(fallback)
    assert authored == (['delta', 'delta', 'full'] if fallback else ['delta'])
    assert budget.schema_repairs == int(bool(fallback))
    assert any(identity.role == 'reviewer' for identity in identities)
    if fallback:
        assert budget.delta_fallback_code == 'attempt_schema_limit'
    if fallback == 'domain':
        assert 'scope_required_check_mapping' in budget.validation_codes
