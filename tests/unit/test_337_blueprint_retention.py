"""Completed planning survives pauses; only actual retirement discards it."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state
from coordinare.session import create_session_from_card, session_to_state
from tests.unit.graph.nodes.test_check_board import _GitHubBlockedNoNewComment, _GitHubCircularDeps


def planned_state(cid, status):
    state = initial_state()
    session = create_session_from_card({'id': cid, 'status': status})
    session.update(blueprint={'summary': 'saved plan'}, blueprint_signature='same requirements',
                   performer_stage='implementing', phase='blocked', last_issue_comment_id=42)
    state['active_sessions'] = {cid: session}
    state['active_card_id'] = cid
    session_to_state(session, state)
    return state


def assert_plan_retained(state, cid):
    session = state['active_sessions'][cid]
    assert session['blueprint'] == {'summary': 'saved plan'}
    assert session['blueprint_signature'] == 'same requirements'
    assert session['performer_stage'] == 'implementing'
    assert session['last_issue_comment_id'] == 42


@pytest.mark.asyncio
async def test_notified_system_block_keeps_completed_plan():
    state = planned_state('ITEM_B', 'BLOCKED')
    state['github_service'] = _GitHubBlockedNoNewComment()
    state['system_error_notified'] = True
    result = await check_board(state)
    assert_plan_retained(result, 'ITEM_B')
    assert result['phase'] == 'blocked'


@pytest.mark.asyncio
async def test_dependency_ineligible_card_keeps_completed_plan():
    state = planned_state('ITEM_A', 'TODO')
    state['github_service'] = _GitHubCircularDeps()
    result = await check_board(state)
    assert_plan_retained(result, 'ITEM_A')
    assert result['phase'] != 'dispatching'



@pytest.mark.asyncio
async def test_all_blocked_daemon_fallback_does_not_overwrite_plan_with_flat_defaults():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from langgraph.graph import END, START, StateGraph

    from coordinare.daemon import CoordinareDaemon
    from coordinare.graph.state import CoordinareState

    builder = StateGraph(CoordinareState)
    builder.add_node('identity', lambda state: state)
    builder.add_edge(START, 'identity')
    builder.add_edge('identity', END)
    daemon = CoordinareDaemon(builder.compile())
    daemon._state = planned_state('ITEM', 'BLOCKED')
    # The symphony swap restores the card pointer, but flat session fields
    # still contain initial/default values from the aggregate daemon state.
    daemon._state['performer_stage'] = 'assessing'
    daemon._state['blueprint'] = None
    daemon._state['blueprint_signature'] = None
    daemon._state['last_issue_comment_id'] = None
    daemon._state['github_service'] = SimpleNamespace(
        poll_board=AsyncMock(return_value={'snapshot': {'BLOCKED': ['ITEM']}}))
    await daemon._invoke_multi_session()
    assert_plan_retained(daemon._state, 'ITEM')


@pytest.mark.asyncio
async def test_notified_system_block_still_accepts_operator_answer():
    from datetime import UTC, datetime

    from tests.unit.graph.nodes.test_check_board import _GitHubBlockedWithNewComment

    state = planned_state('ITEM_B', 'BLOCKED')
    github = _GitHubBlockedWithNewComment()
    state['github_service'] = github
    state['system_error_notified'] = True
    state['last_blocked_notified_at'] = datetime(2026, 2, 24, tzinfo=UTC)
    result = await check_board(state)
    assert_plan_retained(result, 'ITEM_B')
    assert result['phase'] == 'dispatching'
    assert github.moved_to == 'IN_PROGRESS'
