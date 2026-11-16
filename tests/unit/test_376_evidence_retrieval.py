"""Compacted evidence remains discoverable without remembering lost page IDs."""
from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import replace

import pytest

from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceSource, EvidenceStore
from coordinare.services.environment_agent import run_environment_phase
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from tests.unit.test_353_review_findings_flow import finding, review
from tests.unit.test_376_required_reads import EagerProvider, experiment, tool_reply


def store_for(text, *, cap=30000):
    source = EvidenceSource('opaque/input', text, hashlib.sha256(text.encode()).hexdigest())
    return EvidenceStore((source,), required=frozenset({source.key}), output_cap=cap)


@pytest.mark.asyncio
async def test_locator_finds_cross_page_unicode_match_without_claiming_delivery():
    store = store_for('x' * 4094 + '漢字needle' + 'y' * 9000)
    result = json.loads(await store.locator_tool().call({'query': '漢字needle'}))
    assert result['matches'][0]['pages'] == [0, 1]
    assert result['matches'][0]['offset'] == 4094
    assert result['matches'][0]['end'] == 4102
    assert result['matches'][0]['source'] == 'opaque/input'
    assert store.delivered == frozenset()
    batch = await store.read(EvidenceRead(tuple(result['matches'][0]['pages'])))
    assert '漢字needle' in ''.join(page.content for page in batch.pages)


@pytest.mark.asyncio
async def test_catalog_and_search_are_bounded_complete_and_cursor_addressable():
    store = store_for('needle ' * 2000, cap=900)
    catalog = json.loads(await store.locator_tool().call({}))
    assert catalog['matches'][0]['source'] == 'opaque/input'
    assert catalog['matches'][0]['first_page'] == 0
    offsets = []
    cursor = 0
    for _ in range(10):
        raw = await store.locator_tool().call({'source': 'opaque/input', 'query': 'needle', 'cursor': cursor})
        assert len(raw) <= 900
        result = json.loads(raw)
        offsets.extend(item['offset'] for item in result['matches'])
        assert result['next_cursor'] > cursor
        cursor = result['next_cursor']
    assert offsets == list(range(0, len(offsets) * 7, 7))
    absent = json.loads(await store.locator_tool().call({'source': 'different/input', 'query': 'needle'}))
    assert absent['matches'] == []
    assert store.delivered == frozenset()


class RecoveringReader(EagerProvider):
    """Inference double uses only the current request, not remembered evidence."""

    def __init__(self, verdict='accept', *, need_inventory=False):
        super().__init__(compact=True)
        self.answer = review(verdict=verdict, findings=[] if verdict == 'accept' else [finding()])
        self.lost_manifest = False
        self.located = False
        self.reread = False
        self.need_inventory = need_inventory

    async def turn(self, messages, tools, **kwargs):
        if kwargs.get('tool_choice') == 'required':
            return await super().turn(messages, tools, **kwargs)
        self.sent.append({'tools': tools})
        payloads = []
        for message in messages:
            for block in message.content:
                if block.get('type') == 'tool_result':
                    with suppress(ValueError, TypeError):
                        payloads.append(json.loads(block['content']))
        present = any('fixture-decision-marker' in p.get('content', '')
                      for payload in payloads for p in payload.get('pages', []))
        if present:
            self.reread = self.lost_manifest
            return await self.final_reply()
        self.lost_manifest = True
        if self.need_inventory:
            try:
                inventory = json.loads(messages[0].content[0]['text'].splitlines()[-1])
            except (ValueError, KeyError):
                inventory = {}
            if 'manifest' not in inventory.get('packet_fields', []):
                self.answer = review(verdict='indeterminate', findings=[finding()])
                return await self.final_reply()
        for payload in reversed(payloads):
            if payload.get('matches'):
                self.located = True
                return tool_reply('reread_evidence', {'pages': payload['matches'][0]['pages']}, len(self.sent))
        if any(tool['name'] == 'locate_evidence' for tool in tools):
            return tool_reply('locate_evidence', {'source': '@phase-packet', 'query': '"manifest"'}, len(self.sent))
        self.answer = review(verdict='indeterminate', findings=[finding()])
        return await self.final_reply()

    async def final_reply(self):
        from nare import Reply, Usage
        return Reply([{'type': 'text', 'text': json.dumps(self.answer)}], [], Usage(input=100, output=10), 'end_turn')


@pytest.mark.asyncio
@pytest.mark.parametrize('verdict', ['accept', 'revise', 'indeterminate'])
async def test_real_compaction_allows_locating_and_rereading_decision_input(tmp_path, verdict):
    provider = RecoveringReader(verdict)
    request, _ = experiment(tmp_path, provider)
    packet = {**request.packet, 'zz_observations': 'untrusted noise 漢字 ' * 13000}
    packet['manifest']['empty_reason'] = 'fixture-decision-marker'
    request = replace(request, packet=packet)
    result = await run_environment_phase(request)
    assert request.budget.agent_compactions > 0
    assert provider.lost_manifest and provider.located and provider.reread
    assert result.value.verdict == verdict
    assert result.evidence.remaining_required == 0
    assert request.budget.schema_repairs == request.budget.agent_evidence_errors == 0
    assert request.budget.calls == request.budget.calls_completed == len(provider.sent)


@pytest.mark.asyncio
async def test_packet_presence_inventory_survives_actual_elision(tmp_path):
    provider = RecoveringReader(need_inventory=True)
    request, _ = experiment(tmp_path, provider)
    packet = {**request.packet, 'zz_observations': 'untrusted noise 漢字 ' * 13000}
    packet['manifest']['empty_reason'] = 'fixture-decision-marker'
    result = await run_environment_phase(replace(request, packet=packet))
    assert request.budget.agent_compactions > 0
    assert provider.lost_manifest and provider.reread
    assert result.value.verdict == 'accept'


def test_packet_inventory_never_inlines_large_values_or_partial_field_names():
    from coordinare.services.agent_runtime.evidence_navigation import packet_inventory

    packet = {'manifest': 'PRIVATE_VALUE' * 100000, **{'x' * 100 + str(n): None for n in range(300)}}
    text = packet_inventory(packet)
    value = json.loads(text)
    assert len(text) <= 2048
    assert 'PRIVATE_VALUE' not in text
    assert 'manifest' in value['packet_fields']
    assert all(key in packet for key in value['packet_fields'])
    assert value['omitted_fields'] == 301 - len(value['packet_fields'])


@pytest.mark.asyncio
@pytest.mark.parametrize('arguments', [{'query': ''}, {'query': 'a' * 257}, {'cursor': -1}, {'cursor': True}])
async def test_invalid_lookup_never_changes_read_receipts(arguments):
    store = store_for('immutable input')
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await store.locator_tool().call(arguments)
    assert caught.value.code == 'invalid_tool_arguments'
    assert not store.delivered


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [None, 'payload'])
async def test_cursor_beyond_possible_matches_is_empty_without_integer_overflow(query):
    store = store_for('payload')
    arguments = {'cursor': 10**100, **({'query': query} if query is not None else {})}
    assert json.loads(await store.locator_tool().call(arguments)) == {'matches': [], 'next_cursor': None}
    assert not store.delivered


@pytest.mark.asyncio
async def test_locator_invalid_calls_use_existing_attempt_wide_allowance(tmp_path):
    class InvalidLocator(EagerProvider):
        async def turn(self, messages, tools, **kwargs):
            if kwargs.get('tool_choice') == 'required':
                return await super().turn(messages, tools, **kwargs)
            self.sent.append({'tools': tools})
            return tool_reply('locate_evidence', {'query': ''}, len(self.sent))

    provider = InvalidLocator()
    request, _ = experiment(tmp_path, provider)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await run_environment_phase(request)
    assert caught.value.code == 'agent_evidence_call_limit'
    assert request.budget.agent_evidence_errors == 8
    assert request.budget.schema_repairs == 0
    assert request.budget.calls_completed == request.budget.calls == len(provider.sent)
