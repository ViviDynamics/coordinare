"""Native evidence calls own sequencing and preserve explicit reread identity."""
from __future__ import annotations

import json
from typing import Any

import pytest
from nare import Policy, Tool, ToolCall
from nare.caller_tools import external_tools
from nare.tools import dispatch

from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore
from coordinare.services.environment_agent import ObservationTools, PinnedSourceTools
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from tests.unit.test_376_agent_evidence import source


async def invoke(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
    policy = Policy(tools=frozenset({tool.name}), pending=frozenset({tool.name}),
                    external=external_tools((tool,)))
    return await dispatch(ToolCall('call-1', tool.name, arguments), policy)


@pytest.mark.asyncio
async def test_continuation_cannot_be_redirected_to_explicit_pages() -> None:
    store = EvidenceStore((source('x' * 40000),), required=frozenset({'nested/custom.input'}))
    rejected = await invoke(store.tool(), {'pages': [0]})
    assert rejected['is_error'] is True
    assert not store.delivered
    delivered = await invoke(store.tool(), {})
    payload = json.loads(delivered['content'])
    assert payload['page_count'] == 10
    assert payload['pages'][0]['id'] == 0
    assert store.delivered == frozenset(p['id'] for p in payload['pages'])


@pytest.mark.asyncio
async def test_explicit_reread_keeps_bytes_and_reports_recovery_without_delivery() -> None:
    store = EvidenceStore((source('x' * 40000),), required=frozenset({'nested/custom.input'}))
    first = await store.read(EvidenceRead())
    before = store.delivered
    reread = await invoke(store.reread_tool(), {'pages': [0]})
    assert json.loads(reread['content'])['pages'][0] == json.loads(first.serialized)['pages'][0]
    assert store.delivered == before
    invalid = await invoke(store.reread_tool(), {'pages': [0, 999]})
    assert invalid['is_error'] is True
    assert store.delivered == before
    # Guidance consists of bounded trusted metadata, never the submitted list.
    text = invalid['content']
    assert 'review_evidence' in text
    assert 'page_count' in text and '10' in text
    assert 'next_unread' in text
    assert '999' not in text


@pytest.mark.asyncio
async def test_source_continuation_and_reread_share_pinned_identity() -> None:
    async def fetch(path: str) -> str:
        return 'abc' * 20000

    navigation = PinnedSourceTools(SourceInspection(['unfamiliar/nested'], fetch, DiscoveryLimits()))
    tools = {t.name: t for t in navigation.tools()}
    rejected = await invoke(tools['read_source'], {'path': 'unfamiliar/nested', 'pages': [0]})
    assert rejected['is_error'] and not navigation.catalogs
    first = await invoke(tools['read_source'], {'path': 'unfamiliar/nested'})
    reread = await invoke(tools['reread_source'], {'path': 'unfamiliar/nested', 'pages': [0]})
    assert json.loads(first['content'])['pages'][0] == json.loads(reread['content'])['pages'][0]
    invalid = await invoke(tools['reread_source'], {'path': 'unfamiliar/nested', 'pages': [999]})
    assert invalid['is_error'] and 'read_source' in invalid['content']


@pytest.mark.asyncio
async def test_observation_continuation_cannot_skip_required_pages() -> None:
    observations = ObservationTools()
    identity = observations.register({'output': 'sample' * 20000})
    rejected = await invoke(observations.tool(), {'observation_id': identity, 'pages': [0]})
    assert rejected['is_error'] and not observations.catalogs[identity].delivered
    first = await invoke(observations.tool(), {'observation_id': identity})
    reread = await invoke(observations.reread_tool(), {'observation_id': identity, 'pages': [0]})
    assert json.loads(first['content'])['pages'][0] == json.loads(reread['content'])['pages'][0]
    assert observations.issues()
