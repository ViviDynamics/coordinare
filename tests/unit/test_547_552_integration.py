from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import (
    CoordinareDaemon,
    _compute_session_eligibilities,
    _persist_one_session,
    _restored_session_dict,
)
from coordinare.services.pipeline_budget import select_pipelines
from coordinare.state_store import CURRENT_SCHEMA_VERSION, PersistedSession, WorkflowSnapshot


def test_schema30_contract_covers_all_new_fields_and_six_versions():
    contract = json.loads(Path('specs/003-state-persistence/contracts/workflow-snapshot.schema.json').read_text())
    properties = contract['properties']['active_sessions']['additionalProperties']['properties']
    assert {'dispatched_feedback', 'pending_override', 'board_paused', 'board_pause_column', 'board_pause_resume_phase', 'pr_comment_tracking'} <= set(properties)
    assert contract['properties']['schema_version']['enum'] == list(range(1, 31))
    assert CURRENT_SCHEMA_VERSION == 30


def test_combined_feedback_pause_override_tracking_survive_snapshot_without_aliases():
    feedback = [{'id': 'request', 'body': 'Fix the regression'}]
    session = {
        'phase': 'monitoring_performer', 'performer_stage': 'implementing',
        'current_card': {'id': 'card'}, 'agent_dispatch': {'session_id': 'owned'},
        'dispatched_feedback': {'stage': 'implementing', 'items': feedback},
        'pending_override': {'action': 'restart-from', 'target_stage': 'implementing'},
        'board_paused': True, 'board_pause_column': 'BACKLOG', 'board_pause_resume_phase': 'dispatching',
        'pr_comment_tracking': {'pr_node_id': 'PR', 'versions': {'1': 'version'}},
        'processed_review_ids': {'request'}, 'relay_feedback': feedback,
    }
    persisted = _persist_one_session('card', session)
    loaded = PersistedSession.model_validate_json(persisted.model_dump_json())
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase='monitoring_performer')
    restored = _restored_session_dict('card', loaded, snapshot, session['current_card'])
    for key in ('dispatched_feedback', 'pending_override', 'board_paused', 'board_pause_column', 'board_pause_resume_phase', 'pr_comment_tracking', 'relay_feedback', 'processed_review_ids'):
        assert restored[key] == session[key]
    assert restored['agent_dispatch']['session_id'] == 'owned'
    restored['dispatched_feedback']['items'][0]['body'] = 'Edited after restore'
    assert session['dispatched_feedback']['items'][0]['body'] == 'Fix the regression'


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_actual_pause_reconciliation_releases_capacity_only_after_confirmed_stop(confirmed):
    service = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=confirmed))
    paused = {
        'current_card': {'id': 'paused', 'status': 'IN_PROGRESS'},
        'phase': 'monitoring_performer', 'performer_stage': 'implementing',
        'agent_dispatch': {'session_id': 'owned', 'performer_id': 'worker'},
        'pipeline_admitted': True,
    }
    sibling = {'current_card': {'id': 'sibling', 'status': 'TODO'}, 'phase': 'dispatching'}
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon._state.update(active_sessions={'paused': paused, 'sibling': sibling},
                         board_snapshot={'BACKLOG': ['paused'], 'TODO': ['sibling']},
                         performer_services_by_id={'worker': service})
    await daemon._reconcile_board_pauses()
    assert select_pipelines(daemon._state['active_sessions'], 1) == ({'sibling'} if confirmed else {'paused'})
    eligibility = _compute_session_eligibilities(daemon._state, daemon._state['active_sessions'], 1)
    assert not eligibility['paused'].eligible
    assert eligibility['sibling'].eligible is confirmed
    assert bool(paused['agent_dispatch']) is not confirmed
    assert paused['board_paused'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('confirmed', [True, False])
async def test_paused_side_writer_reserves_issue_until_its_stop_is_confirmed(confirmed):
    main = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    side = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=confirmed))
    paused = {
        'current_card': {'id': 'paused', 'status': 'IN_PROGRESS'},
        'phase': 'monitoring_performer', 'performer_stage': 'implementing',
        'agent_dispatch': {'session_id': 'main', 'performer_id': 'worker'},
        'documenting_side': {'session_id': 'side', 'status': 'running', 'writer_active': True},
        'pipeline_admitted': True,
    }
    sibling = {'current_card': {'id': 'sibling', 'status': 'TODO'}, 'phase': 'dispatching'}
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon._state.update(active_sessions={'paused': paused, 'sibling': sibling},
                         board_snapshot={'BACKLOG': ['paused'], 'TODO': ['sibling']},
                         performer_services_by_id={'worker': main},
                         performer_services={'documenting': side})
    await daemon._reconcile_board_pauses()
    assert paused['agent_dispatch'] == {}
    side.stop_session_confirmed.assert_awaited_once_with('side')
    eligibility = _compute_session_eligibilities(daemon._state, daemon._state['active_sessions'], 1)
    assert eligibility['sibling'].eligible is confirmed
    assert daemon._state['_pipeline_selected'] == ({'sibling'} if confirmed else {'paused'})
    assert paused['documenting_side']['writer_active'] is (not confirmed)
