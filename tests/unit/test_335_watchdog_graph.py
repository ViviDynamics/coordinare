"""Watchdog state survives the real LangGraph channel and snapshot boundaries."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from langgraph.graph import END, START, StateGraph

from coordinare.daemon import CoordinareDaemon, _persist_active_sessions
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import CoordinareState
from coordinare.services.github import GitHubService
from coordinare.state_store import WorkflowSnapshot
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer, _stalled_state


@pytest.mark.asyncio
async def test_watchdog_trips_through_compiled_graph():
    builder = StateGraph(CoordinareState)
    builder.add_node('monitor', monitor_performer)
    builder.add_edge(START, 'monitor')
    builder.add_edge('monitor', END)
    graph = builder.compile()
    state = _stalled_state(_Performer({'status': 'working'}), stall=900)
    with patch('coordinare.services.dispatch_guard.drain_or_reap', new=AsyncMock()) as drain:
        result = await graph.ainvoke(state)
    drain.assert_awaited_once()
    assert result['phase'] == 'dispatching'
    assert result['last_progress_at'] is None


def test_watchdog_snapshot_round_trip():
    at = datetime.now(UTC) - timedelta(seconds=1000)
    sessions = {'card': {'last_progress_at': at, 'last_progress_fingerprint': 'stable'}}
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase='idle',
                                active_sessions=_persist_active_sessions(sessions))
    snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    daemon = CoordinareDaemon(AsyncMock())
    daemon._restore_from_snapshot(snapshot)
    restored = daemon._state['active_sessions']['card']
    assert restored['last_progress_at'] == at
    assert restored['last_progress_fingerprint'] == 'stable'


@pytest.mark.asyncio
async def test_token_limit_posts_to_real_github_service_contract():
    github = create_autospec(GitHubService, instance=True, spec_set=True)
    state = _make_state(service=_Performer({'status': 'token_limit'}), stage='architecting',
                        card={'id': 'ITEM', 'issue_id': 'ISSUE_NODE', 'issue_number': 160,
                              'status': 'IN_PROGRESS'}, github=github)
    result = await monitor_performer(state)
    github.add_comment.assert_awaited_once()
    subject, body = github.add_comment.await_args.args
    assert subject == 'ISSUE_NODE'
    assert 'output token limit reached' in body
    assert result['phase'] == 'blocked'
