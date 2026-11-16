"""Divided native construction preserves global ownership and complete final coverage."""
from __future__ import annotations

import json
from typing import Any

import pytest
from nare import Reply, Usage

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import EnvironmentAgentContext
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import discover_and_review
from tests.unit.scope_chat_fixture import FixtureAssertionError
from tests.unit.test_351_environment_discovery import CAPABILITIES
from tests.unit.test_353_shard_scope import COMMIT, ENTRIES, LIMITS, SOURCES, Router, criterion
from tests.unit.test_353_sharded_executable import global_part
from tests.unit.test_376_bootstrap_phases import PhaseTransport


@pytest.mark.asyncio
async def test_divided_native_authoring_merges_all_criteria_without_global_mapping(tmp_path: Any) -> None:
    class ManyCriteria(Router):
        def author(self, messages: Any, user: Any) -> dict[str, Any]:
            path = next(s for s in self.plan.shards if user['shard']['id'].startswith(s.id)).paths[0]
            return {'shard_summary': {'criteria': [criterion(path, f'{path} duty {i}') for i in range(10)],
                                     'cross_references': [], 'questions': []}}
    router = ManyCriteria()
    phases = []
    budget = AttemptBudget(max_calls=256)
    class Runtime:
        async def run(self, req: Any) -> Any:
            phases.append(req.identity.phase)
            if '"tool":"read_file"' in req.instructions:
                raise FixtureAssertionError('Native role advertised old transport')
            class Routed(PhaseTransport):
                async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
                    reply = await super().turn(messages, tools, tool_choice=tool_choice)
                    if reply.stop_reason != 'end_turn':
                        return reply
                    packet = json.loads(''.join(self.packet_pages[k] for k in sorted(self.packet_pages)))
                    original = [{'role': 'system', 'content': req.instructions}, *packet['context'],
                                {'role': 'user', 'content': '{}'}]
                    content = json.loads(packet['context'][0]['content'])
                    if req.identity.phase.startswith('scope_'):
                        answer = json.loads(await router(original, response_schema=req.answer_schema))
                    elif req.identity.phase == 'discovery':
                        assert 'criterion_checks maps every required criterion ID' not in req.instructions
                        answer = {'manifest': global_part(requirements=[{'id': 'tool', 'description': 'Fixture tool',
                            'source': 'README.md', 'checks': ['toolchain'], 'required': True}]), 'finding_responses': []}
                    elif req.identity.phase == 'executable_checks':
                        check = content['check_id_prefix'] + 'smoke'
                        answer = {'shard_checks': {'checks': [{'id': check, 'argv': ['true']}],
                            'criterion_checks': {c['id']: [check] for c in content['assigned_criteria']}}}
                    else:
                        own = content['own']
                        answer = {'assessment': {'verdict': 'accept', 'rationale': 'Independent fixture review',
                            'sources': [own['mandatory_evidence'][0]['path']], 'findings': [], 'decisions': [],
                            'capability_unavailable': False}}
                    return Reply([{'type': 'text', 'text': json.dumps(answer)}], [], Usage(input=100, output=20), 'end_turn')
            result = await NareRuntime(transport=Routed({})).run(req)
            if result.status != 'accepted':
                raise FixtureAssertionError(f'{req.identity.phase}: {result.hold_code}')
            return result
    budget.agent_context = EnvironmentAgentContext(runtime=Runtime(), attempt_id='divided',
        model_config_digest='a'*64, checkpoint_store=CheckpointStore(tmp_path))
    async def fetch(path: str) -> str:
        return SOURCES[path]
    async def legacy(*args: Any, **kwargs: Any) -> str:
        raise FixtureAssertionError('Divided pipeline invoked legacy chat')
    manifest, review = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=list(SOURCES),
        entries=ENTRIES, fetch=fetch, chat=legacy, capabilities=CAPABILITIES, budget=budget, limits=LIMITS)
    assert review.verdict == 'accept' and review.manifest_digest == manifest.digest()
    assert len(manifest.scope_contract.definition.criteria) > 25
    assert set(manifest.criterion_checks) == {c.id for c in manifest.scope_contract.definition.criteria}
    assert phases.count('executable_checks') >= 2 and phases.count('review') >= 3
    assert budget.schema_repairs == 0
