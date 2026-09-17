"""Restored history must reach the first per-symphony graph invocation."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_active_sessions
from coordinare.graph.state import SymphonyRuntimeState
from coordinare.services.pipeline_budget import select_pipelines
from coordinare.state_store import WorkflowSnapshot


def saved_sessions():
    return {str(i): {'performer_stage': 'implementing', 'phase': 'idle',
                     'last_issue_comment_id': 100 + i,
                     'processed_issue_comment_ids': [100 + i],
                     'blueprint': {'summary': 'keep plan'}, 'blueprint_signature': 'requirements'}
            for i in range(6)}


def snapshot():
    return WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase='idle',
                            active_sessions=_persist_active_sessions(saved_sessions()))


@pytest.mark.asyncio
async def test_unfocused_sessions_survive_first_symphony_cycle(monkeypatch):
    daemon = CoordinareDaemon(AsyncMock())
    runtime = SymphonyRuntimeState(name='website')
    daemon._state['symphony_states'] = {'website': runtime}
    daemon._restore_from_snapshot(snapshot())
    observed = []

    async def first_tick(*args, **kwargs):
        sessions = daemon._state['active_sessions']
        assert len(sessions) == 6
        for cid, session in sessions.items():
            assert session['performer_stage'] == 'implementing'
            assert session['last_issue_comment_id'] == 100 + int(cid)
            assert session['processed_issue_comment_ids'] == {100 + int(cid)}
            assert session['blueprint_signature'] == 'requirements'
            assert session['blueprint'] is not None
        observed.append(select_pipelines(sessions, 1, {'0', '1'}))

    monkeypatch.setattr(daemon, '_invoke_multi_session', first_tick)
    await daemon._conduct_single_symphony('website', SimpleNamespace())
    assert observed == [{'0'}]
    assert len(runtime.active_sessions) == 6


@pytest.mark.asyncio
async def test_multisymphony_claims_only_matching_project_item_ids(monkeypatch):
    daemon = CoordinareDaemon(AsyncMock())
    a, b = SymphonyRuntimeState(name='a'), SymphonyRuntimeState(name='b')
    daemon._state['symphony_states'] = {'a': a, 'b': b}
    daemon._restore_from_snapshot(snapshot())
    assert not a.active_sessions and not b.active_sessions
    service = SimpleNamespace(poll_board=AsyncMock(return_value={'snapshot': {'TODO': ['0']}}))
    daemon._state['symphony_github_services'] = {'a': service}
    monkeypatch.setattr(daemon, '_invoke_multi_session', AsyncMock())
    await daemon._conduct_single_symphony('a', SimpleNamespace())
    assert set(a.active_sessions) == {'0'}
    assert not b.active_sessions
    assert set(daemon._unassigned_restored_sessions) == {'1', '2', '3', '4', '5'}
    assert len(daemon._build_snapshot().active_sessions) == 6


@pytest.mark.asyncio
async def test_daemon_start_runs_real_board_graph_with_unfocused_history(monkeypatch):
    from langgraph.graph import END, START, StateGraph

    from coordinare.graph.nodes.check_board import check_board
    from coordinare.graph.state import CoordinareState
    from coordinare.services.pipeline_budget import dispatch_has_pipeline_slot

    dispatched = []

    async def fake_dispatch(state):
        cid = state['current_card']['id']
        if state['phase'] == 'dispatching' and dispatch_has_pipeline_slot(state, cid):
            dispatched.append(cid)
            state['phase'] = 'monitoring_performer'
            state['agent_dispatch'] = {'session_id': 'fake-job'}
        return state

    builder = StateGraph(CoordinareState)
    builder.add_node('board', check_board)
    builder.add_node('dispatch', fake_dispatch)
    builder.add_edge(START, 'board')
    builder.add_edge('board', 'dispatch')
    builder.add_edge('dispatch', END)
    store = SimpleNamespace(load=AsyncMock(return_value=snapshot()), save=AsyncMock())
    daemon = CoordinareDaemon(builder.compile(), state_store=store, max_cycles=1,
                             sleep_func=AsyncMock())
    runtime = SymphonyRuntimeState(name='website')
    board = {'snapshot': {'TODO': list(saved_sessions())},
             'titles': dict.fromkeys(saved_sessions(), 'Implement feature'),
             'descriptions': dict.fromkeys(saved_sessions(), 'Deliver feature')}
    github = SimpleNamespace(project_id='project', poll_board=AsyncMock(return_value=board))
    daemon._state.update(symphony_states={'website': runtime},
                         symphony_configs={'website': SimpleNamespace(enabled=True)},
                         symphony_github_services={'website': github})
    monkeypatch.setattr('coordinare.services.reconciliation.run_startup_reconciliation',
                        AsyncMock(return_value=SimpleNamespace(docker_unreachable=False)))
    monkeypatch.setattr(daemon, '_install_signal_handlers', lambda: None)
    await daemon.start()
    assert len(dispatched) == 1
    assert len(runtime.active_sessions) == 6
    for cid, session in runtime.active_sessions.items():
        assert session['performer_stage'] == 'implementing'
        assert session['last_issue_comment_id'] == 100 + int(cid)
        assert session['blueprint_signature'] == 'requirements'
