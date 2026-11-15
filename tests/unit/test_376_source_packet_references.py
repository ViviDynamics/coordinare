"""Native packet references remove only verified duplicate source bodies."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

import pytest
from coordinare_service_inference.environment.findings import FindingLedger, FindingReview
from coordinare_service_inference.environment.scope import canonical
from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding

from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import (
    EnvironmentAgentContext,
    SourcePhase,
    run_source_phase,
)
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import DiscoveryLimits, discover_and_review
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_response_schema import final_response_schema
from coordinare.services.environment_scope import _messages, _Request
from coordinare.services.environment_scope_investigation import investigate_scope_requests
from coordinare.services.environment_shard_scope import ShardedScope
from tests.unit.test_353_discovery_repair import CAPABILITIES, COMMIT, PATH, SOURCE
from tests.unit.test_353_scope_contract import criterion, proposal, review
from tests.unit.test_376_bootstrap_phases import PhaseTransport

TEXT = 'A pinned source with "quotes", a newline\n and unicode é.' * 900


class CapturedPacketError(BaseException):
    pass


def setup_inspection(*, native: bool = True) -> SourceInspection:
    budget = AttemptBudget()
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    if native:
        budget.agent_context = EnvironmentAgentContext(None, 'packet-contract', 'a' * 64)
    async def fetch(path: str) -> str:
        return TEXT
    return SourceInspection(['guide'], fetch, DiscoveryLimits(), budget)


@pytest.mark.parametrize('native', [False, True])
def test_scope_packet_references_exact_source_only_for_native_transport(native: bool) -> None:
    inspection = setup_inspection(native=native)
    item = inspection.admit_content('guide', TEXT, fetched=False)
    before = copy.deepcopy(item)
    messages = _messages(inspection, review=True, definition=None, evidence=[item])
    [record] = json.loads(messages[1]['content'])['mandatory_evidence']
    assert item == before
    assert inspection.source_files['guide'] == TEXT
    assert record == ({'path': 'guide', 'sha256': hashlib.sha256(TEXT.encode()).hexdigest(),
                       'content_ref': 'guide'} if native else before)


@pytest.mark.parametrize('change', [{'content': 'different'}, {'sha256': 'b' * 64}, {'path': 'elsewhere'}])
def test_unverified_packet_record_keeps_its_entire_body(change: dict[str, str]) -> None:
    inspection = setup_inspection()
    item = {**inspection.admit_content('guide', TEXT, fetched=False), **change}
    before = copy.deepcopy(item)
    messages = _messages(inspection, review=True, definition=None, evidence=[item])
    assert json.loads(messages[1]['content'])['mandatory_evidence'] == [before]
    assert item == before


@pytest.mark.asyncio
async def test_source_reference_keeps_canonical_bytes_and_requires_their_delivery() -> None:
    inspection = setup_inspection()
    item = inspection.admit_content('guide', TEXT, fetched=False)
    messages = _messages(inspection, review=True, definition=None, evidence=[item])
    class Runtime:
        async def run(self, request: Any) -> Any:
            catalog = request.evidence_catalog
            pages = catalog._pages
            packet = json.loads(''.join(page.content for page in pages if page.source == '@phase-packet'))
            metadata = json.loads(packet['context'][0]['content'])
            assert metadata['mandatory_evidence'][0] == {
                'path': 'guide', 'sha256': item['sha256'], 'content_ref': 'guide'}
            source_pages = [page for page in pages if page.source == 'guide']
            assert ''.join(page.content for page in source_pages).encode() == TEXT.encode()
            assert all(page.source_sha256 == item['sha256'] for page in source_pages)
            assert catalog.remaining_required == catalog.page_count
            assert not catalog.delivered
            assert len([page for page in pages if page.source == '@phase-packet']) == 1
            raise CapturedPacketError
    inspection.budget.agent_context = EnvironmentAgentContext(Runtime(), 'packet-contract', 'a' * 64)
    with pytest.raises(CapturedPacketError):
        await run_source_phase(inspection, messages, SourcePhase(envelope='scope_assessment',
            schema=final_response_schema('scope_assessment'), source_commit=COMMIT,
            validate=lambda _: None, role='reviewer'))


@pytest.mark.asyncio
async def test_investigation_packet_keeps_binding_and_result_but_references_exact_content() -> None:
    inspection = setup_inspection()
    budget = inspection.budget
    budget.scope_findings.reviews.append(FindingReview(sequence=1, manifest_digest='1' * 64,
        inputs={}, decisions=[], added_ids=[]))
    binding = ScopeInspectionBinding.bind(finding_id='F0001', review_sequence=1, scope_digest='1' * 64,
        source_commit=COMMIT, inventory_digest=inspection.inventory_digest,
        request={'operation': 'read_file', 'path': 'guide'})
    budget.scope_investigations = [binding]
    results = await investigate_scope_requests(inspection, [binding])
    before = [result.model_dump(mode='json') for result in results]
    messages = _messages(inspection, review=False, definition=None, evidence=[])
    [entry] = json.loads(messages[1]['content'])['investigation_evidence']
    assert entry['binding'] == binding.model_dump(mode='json')
    assert entry['result'] == before[0]
    assert entry['evidence'] == {'path': 'guide', 'sha256': inspection.inputs['guide'], 'content_ref': 'guide'}
    restored = {'path': entry['evidence']['path'], 'sha256': entry['evidence']['sha256'],
                'content': inspection.source_files[entry['evidence']['content_ref']]}
    assert hashlib.sha256(canonical(restored)).hexdigest() == entry['result']['evidence_digest']
    assert [result.model_dump(mode='json') for result in results] == before


@pytest.mark.asyncio
async def test_native_discovery_identity_describes_canonical_evidence_and_available_tools() -> None:
    budget = AttemptBudget(max_calls=30)
    captured = []
    async def fetch(path: str) -> str:
        return SOURCE
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Native source phases must not call legacy chat')
    class Runtime:
        async def run(self, request: Any) -> Any:
            if request.identity.phase == 'discovery':
                pages = request.evidence_catalog._pages
                packet = json.loads(''.join(page.content for page in pages if page.source == '@phase-packet'))
                context = json.loads(packet['context'][0]['content'])
                captured.append(context['evidence_rule'])
                raise CapturedPacketError
            answer = proposal(criterion(PATH)) if request.identity.phase == 'scope_author' else review(sources=[PATH])
            return await NareRuntime(transport=PhaseTransport(answer, read_path=PATH)).run(request)
    budget.agent_context = EnvironmentAgentContext(Runtime(), 'identity-contract', 'a' * 64)
    with pytest.raises(CapturedPacketError):
        await discover_and_review(symphony_name='fictional-suite', commit=COMMIT, paths=[PATH],
            fetch=fetch, chat=legacy, capabilities=CAPABILITIES, budget=budget)
    [guidance] = captured
    assert 'read_files' not in guidance
    assert 'not preloaded' not in guidance
    assert 'review_evidence' in guidance
    assert 'read_source' in guidance


@pytest.mark.asyncio
async def test_native_shard_author_packet_references_both_shard_and_spine_bodies() -> None:
    from tests.unit.test_353_shard_scope import ENTRIES, LIMITS, PLAN, SOURCES

    budget = AttemptBudget()
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    captured = []
    async def fetch(path: str) -> str:
        return SOURCES[path]
    async def legacy(*args: Any, **kwargs: Any) -> str:
        pytest.fail('Native shard author must not call legacy chat')
    class Runtime:
        async def run(self, request: Any) -> Any:
            pages = request.evidence_catalog._pages
            packet = json.loads(''.join(page.content for page in pages if page.source == '@phase-packet'))
            captured.append((json.loads(packet['context'][0]['content']), request.instructions))
            raise CapturedPacketError
    budget.agent_context = EnvironmentAgentContext(Runtime(), 'shard-packet', 'a' * 64)
    request = _Request(paths=list(SOURCES), fetch=fetch, chat=legacy, commit=COMMIT,
        limits=LIMITS, budget=budget, entries=ENTRIES)
    scope = ShardedScope(request, ENTRIES, PLAN, 1)
    await scope.load_spine()
    with pytest.raises(CapturedPacketError):
        await scope.author_shard(PLAN.shards[0])
    [(packet, instructions)] = captured
    records = [*packet['shard_evidence'], *packet['_shared_context']['spine_evidence']]
    assert records
    for item in records:
        assert item == {'path': item['path'], 'sha256': hashlib.sha256(SOURCES[item['path']].encode()).hexdigest(),
                        'content_ref': item['path']}
    assert 'shard_evidence already holds the content' not in instructions
    assert 'content_ref' in instructions
