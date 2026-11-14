"""Evidence is counted only when whole bytes cross the native tool boundary."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from nare import Policy, Reply, ToolCall, Usage, new_session, run
from nare.tools import MAX_TOOL_OUTPUT

from coordinare.services.environment_attempt import EnvironmentDiscoveryError


def source(text: str, key: str = 'nested/custom.input') -> Any:
    from coordinare.services.agent_runtime.evidence import EvidenceSource

    return EvidenceSource(key=key, content=text, sha256=hashlib.sha256(text.encode()).hexdigest())


@pytest.mark.asyncio
async def test_requested_but_unreturned_page_is_unread() -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore

    store = EvidenceStore((source('x' * 40000),), required=frozenset({'nested/custom.input'}), output_cap=6000)
    batch = await store.read(EvidenceRead(pages=tuple(range(store.page_count))))
    payload = json.loads(batch.serialized)
    returned = {page['id'] for page in payload['pages']}
    assert returned
    assert len(returned) < store.page_count
    assert store.delivered == frozenset(returned)
    assert payload['unread_count'] == store.page_count - len(returned)
    assert payload['next_unread'] not in returned


@pytest.mark.asyncio
async def test_unicode_and_escaping_respect_serialized_cap() -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore

    text = '\u0000😀"\\\n字' * 5000
    store = EvidenceStore((source(text),), required=frozenset({'nested/custom.input'}), output_cap=2500)
    chunks = []
    while store.remaining_required:
        batch = await store.read(EvidenceRead())
        assert len(batch.serialized) <= 2500
        assert len(batch.serialized) <= MAX_TOOL_OUTPUT
        for page in json.loads(batch.serialized)['pages']:
            assert page['sha256'] == hashlib.sha256(page['content'].encode()).hexdigest()
            chunks.append(page['content'])
    assert ''.join(chunks) == text


def test_changed_source_hash_refused() -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceSource, EvidenceStore

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        EvidenceStore((EvidenceSource(key='custom', content='changed', sha256='0' * 64),), required=frozenset({'custom'}))
    assert caught.value.code == 'agent_evidence_changed'


@pytest.mark.asyncio
async def test_scoped_requirements_do_not_expand_to_optional_sources() -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore

    store = EvidenceStore((source('needed', 'scope/a'), source('optional', 'other/b')), required=frozenset({'scope/a'}))
    batch = await store.read(EvidenceRead())
    assert [p['source'] for p in json.loads(batch.serialized)['pages']] == ['scope/a']
    assert store.remaining_required == 0
    assert store.delivered == frozenset({0})


class EvidenceTransport:
    def __init__(self, store: Any, *, compact: bool) -> None:
        self.store = store
        self.compact = compact
        self.turns = 0
        self.received: dict[int, dict[str, Any]] = {}
        self.refusals = 0

    async def context_window(self) -> int:
        return 24000 if self.compact else 262144

    async def turn(self, messages: Any, tools: Any) -> Reply:
        self.turns += 1
        measured_input = max(1, sum(len(json.dumps(message.content, ensure_ascii=False)) for message in messages) // 4)
        for message in messages:
            for block in message.content:
                if block.get('type') != 'tool_result':
                    continue
                content = block['content']
                if not isinstance(content, str) or not content.startswith('{'):
                    continue
                payload = json.loads(content)
                assert len(content) <= MAX_TOOL_OUTPUT
                for page in payload['pages']:
                    self.received[page['id']] = page
        # Force premature finalization, then recover by native tool delivery.
        if self.turns == 1 or not self.store.remaining_required:
            return Reply([{'type': 'text', 'text': '{"accepted":true}'}], [], Usage(input=measured_input, output=10), 'end_turn')
        call = ToolCall(str(self.turns), 'review_evidence', {})
        return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                     [call], Usage(input=measured_input, output=10), 'tool_use')


@pytest.mark.asyncio
@pytest.mark.parametrize('compact', [False, True], ids=['full-delivery', 'elision-preserves-coverage'])
async def test_whole_pages_survive_actual_transport_boundary(compact: bool) -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceStore

    text = 'arbitrary nested payload: 漢字 " \\ \n' * 7000
    store = EvidenceStore((source(text),), required=frozenset({'nested/custom.input'}))
    transport = EvidenceTransport(store, compact=compact)
    checks = []

    async def check(answer: Any) -> list[Any]:
        checks.append(store.remaining_required)
        return [{'code': 'review_evidence_unread', 'message': 'Read every required page.'}] if store.remaining_required else []

    session = new_session('Read the immutable evidence using review_evidence, then assess it.')
    events = []
    async for event in run(session, transport=transport,
        policy=Policy(tools=frozenset({'review_evidence'}), pending=frozenset({'review_evidence'})),
        tools=(store.tool(),), schema={'type': 'object', 'properties': {'accepted': {'type': 'boolean'}},
            'required': ['accepted'], 'additionalProperties': False}, check=check, max_turns=80,
        compaction='elide', compaction_trigger=0.6, compaction_target=0.4):
        events.append(event)
    assert session.status == 'done', (session.status, session.error)
    assert checks[0] > 0
    assert checks[-1] == 0
    assert set(transport.received) == set(range(store.page_count))
    assert ''.join(transport.received[i]['content'] for i in range(store.page_count)) == text
    if compact:
        assert any(e.type == 'progress' and 'compaction' in e.detail for e in events)
    before = store.delivered
    from coordinare.services.agent_runtime.evidence import EvidenceRead

    reread = await store.read(EvidenceRead(pages=(0,)))
    assert json.loads(reread.serialized)['pages'][0] == transport.received[0]
    assert store.delivered == before


@pytest.mark.asyncio
async def test_invalid_mixed_request_delivers_nothing() -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore

    store = EvidenceStore((source('unchanged'),), required=frozenset({'nested/custom.input'}))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await store.read(EvidenceRead(pages=(0, 999)))
    assert caught.value.code == 'agent_evidence_page'
    assert store.delivered == frozenset()
    assert store.remaining_required == 1


@pytest.mark.asyncio
async def test_empty_required_source_still_needs_delivery() -> None:
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore

    store = EvidenceStore((source(''),), required=frozenset({'nested/custom.input'}))
    assert store.remaining_required == 1
    payload = json.loads((await store.read(EvidenceRead())).serialized)
    assert payload['pages'][0]['content'] == ''
    assert payload['pages'][0]['offset'] == payload['pages'][0]['end'] == 0
    assert payload['unread_count'] == 0
