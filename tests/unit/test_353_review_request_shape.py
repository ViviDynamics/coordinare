"""Every divided review request is one JSON document, and the parts share a byte-identical prefix (353 repair).

The live run held three review judges before any reply came back, with a JSONDecodeError ("Extra data") behind the
invalid_contract. The judges' user content was two JSON objects joined by a newline, which no strict JSON consumer
can read. These tests drive the divided review through the real run_agent path with a strict chat.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.scope import (
    ScopeCriterion,
    ScopeCriterionProposal,
    criterion_id,
)

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import DiscoveryLimits, review_part_messages
from coordinare.services.environment_shard_construction import CheckGroup
from coordinare.services.environment_shard_scope import AgentContext


def make_criterion(index: int) -> ScopeCriterion:
    proposal = ScopeCriterionProposal.model_validate({
        'description': f'Obligation {index}', 'sources': [f'src/{index:03d}.md'], 'applicability': 'required',
        'rationale': 'Cited source obligation', 'usability': f'{index} succeeds', 'runtime_needs': []})
    return ScopeCriterion.model_validate({**proposal.model_dump(), 'id': criterion_id(proposal)})


class FakeManifest(SimpleNamespace):
    def digest(self):
        return 'd' * 64

    def model_dump(self, **_kwargs):
        return {'checks': self.checks, 'requirements': [], 'install': [], 'activation': {}, 'services': []}

    install: list = ()
    services: list = ()


def test_review_part_messages_is_one_json_object_with_the_shared_text_first():
    shared = {'base': {'requested_scope': 'x'}, 'manifest_digest': 'd', 'global_part': {'checks': 3}}
    one = review_part_messages('SYSTEM', shared, {'part': {'prefix': 'a'}, 'assigned_findings': [{'id': 'f1'}]})
    two = review_part_messages('SYSTEM', shared, {'part': {'prefix': 'b'}, 'assigned_findings': [{'id': 'f2'}]})
    assert json.loads(one[1]['content']) == {'shared': shared, 'own': {'part': {'prefix': 'a'}, 'assigned_findings': [{'id': 'f1'}]}}
    shared_text = json.dumps(shared, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    assert one[1]['content'].startswith('{"shared":' + shared_text + ',"own":')
    assert one[1]['content'][:len('{"shared":' + shared_text)] == two[1]['content'][:len('{"shared":' + shared_text)]


@pytest.mark.asyncio
async def test_every_divided_review_request_parses_as_one_document_through_run_agent(monkeypatch):
    first, second = make_criterion(1), make_criterion(2)
    groups = [CheckGroup('c00-', (first,)), CheckGroup('c01-', (second,))]
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    budget.finding_ledger = SimpleNamespace(records=[])
    budget.coverage_manifest = None
    inputs = {first.sources[0]: 'a' * 64, second.sources[0]: 'b' * 64, 'Makefile': 'c' * 64}
    requests: list[list[dict[str, str]]] = []
    reply = {'assessment': {'findings': [], 'decisions': [], 'capability_unavailable': False, 'verdict': 'accept',
                            'rationale': 'Every part input was read and judged against its criteria.', 'sources': [first.sources[0]]}}

    async def chat(messages, **_kwargs):
        # A strict consumer: every message is a single JSON document, as a chat template or parser would require.
        for message in messages[1:]:
            json.loads(message['content'])
        requests.append(messages)
        return json.dumps(reply)

    monkeypatch.setattr(discovery_module, 'manifest_obligation_ids', lambda _manifest: set())
    monkeypatch.setattr(discovery_module, 'dependency_digest', lambda _manifest, _ids: 'x' * 64)
    manifest = FakeManifest(inputs=inputs, checks=[{'id': 'toolchain'}, {'id': 'c00-0'}, {'id': 'c01-0'}],
                            criterion_checks={first.id: ['c00-0'], second.id: ['c01-0']})
    scope = SimpleNamespace(definition=SimpleNamespace(criteria=[first, second]))
    build = SimpleNamespace(groups=groups, scope=scope, context=AgentContext(budget, DiscoveryLimits(), chat), concurrency=4)
    attempt = SimpleNamespace(budget=budget, delta=None, identity={'symphony_name': 'suite'},
                              discovery=SimpleNamespace(finding_responses=[]))
    reviewer = discovery_module._Inspection('review', sorted(inputs), None, DiscoveryLimits(), budget)
    reviewer.inputs.update(inputs)

    combined, _reads = await discovery_module._divided_review(build, attempt, manifest, reviewer)

    assert combined['verdict'] == 'accept'
    assert len(requests) == 3
    prefixes = {message[1]['content'].split(',"own":', 1)[0] for message in requests}
    assert len(prefixes) == 1
