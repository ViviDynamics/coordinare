"""Issue-level admission survives handoffs and restart without killing jobs."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinare.services.pipeline_budget import dispatch_has_pipeline_slot, select_pipelines
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import PersistedSession


def sessions(n=6):
    return {str(i): {'current_card': {'id': str(i), 'status': 'IN_PROGRESS'},
                     'phase': 'dispatching', 'performer_stage': 'assessing'} for i in range(n)}


def test_two_of_six_recovered_issues_keep_slots_across_handoffs():
    work = sessions()
    assert select_pipelines(work, 2) == {'0', '1'}
    work['0']['performer_stage'] = 'implementing'
    work['1']['phase'] = 'monitoring_pr'
    assert select_pipelines(work, 2) == {'0', '1'}
    work['0']['current_card']['status'] = 'DONE'
    assert select_pipelines(work, 2) == {'1', '2'}


def test_one_issue_runs_entire_pipeline_before_another():
    work = sessions()
    for stage in ['assessing', 'architecting', 'implementing', 'reviewing', 'qa', 'closing_review']:
        work['0']['performer_stage'] = stage
        assert select_pipelines(work, 1) == {'0'}
    work['0']['current_card']['status'] = 'DONE'
    assert select_pipelines(work, 1) == {'1'}


def test_live_over_budget_drains_without_new_dispatch():
    work = sessions()
    for cid in ['2', '3', '4']:
        work[cid].update(phase='monitoring_performer', agent_dispatch={'session_id': cid})
    assert select_pipelines(work, 1) == {'2', '3', '4'}
    work['2'].update(phase='dispatching', agent_dispatch={})
    assert select_pipelines(work, 1) == {'3', '4'}
    assert not work['2']['pipeline_admitted']


@pytest.mark.parametrize('side', [
    {'status': 'running', 'session_id': 'side'},
    {'status': 'failed', 'writer_active': True, 'session_id': 'side'},
    {'status': 'waiting', 'writer_active': False, 'session_id': 'side'},
    {'status': 'running', 'writer_active': True},
])
def test_only_owned_active_side_writer_retains_paused_issue_reservation(side):
    work = sessions(2)
    work['0'].update(phase='idle', board_paused=True, documenting_side=side)
    work['0']['current_card']['status'] = 'BACKLOG'
    active = bool(side.get('session_id')) and (side.get('status') == 'running' or side.get('writer_active'))
    assert select_pipelines(work, 1, {'1'}) == ({'0'} if active else {'1'})


def test_dependency_and_human_blocks_release_slots_without_losing_sessions():
    work = sessions()
    assert select_pipelines(work, 1, {'2', '3'}) == {'2'}
    work['2']['phase'] = 'blocked'
    assert select_pipelines(work, 1, {'2', '3'}) == {'3'}
    assert len(work) == 6


def test_bootstrap_and_recovery_dispatch_cannot_bypass_budget():
    state = {'active_sessions': sessions(), 'config': SimpleNamespace(max_concurrent_cards=1)}
    assert not dispatch_has_pipeline_slot(state, '5')
    assert dispatch_has_pipeline_slot(state, '0')
    state['_pipeline_selected'] = {'0'}
    state['active_sessions']['new'] = sessions(1)['0']
    assert not dispatch_has_pipeline_slot(state, 'new')


def test_reservation_round_trips_and_old_snapshots_default_safely():
    flat = {}
    session_to_state({'pipeline_admitted': True}, flat)
    assert state_to_session(flat)['pipeline_admitted'] is True
    # Minimal fields are supplied by the persistence model's existing defaults.
    assert PersistedSession(card_id='old').pipeline_admitted is False
    from coordinare.daemon import _persist_active_sessions
    work = sessions()
    select_pipelines(work, 1)
    persisted = _persist_active_sessions(work)
    assert persisted['0'].pipeline_admitted is True
    assert persisted['1'].pipeline_admitted is False
    reloaded = PersistedSession.model_validate_json(persisted['0'].model_dump_json())
    assert reloaded.pipeline_admitted is True


@pytest.mark.asyncio
async def test_dispatch_boundary_cannot_start_a_queued_issue(monkeypatch):
    from unittest.mock import AsyncMock

    from coordinare.graph.nodes.dispatch_performer import dispatch_performer

    body = AsyncMock()
    monkeypatch.setattr('coordinare.graph.nodes.dispatch_performer._dispatch_performer_body', body)
    state = {'active_sessions': sessions(), 'config': SimpleNamespace(max_concurrent_cards=1),
             'current_card': {'id': '5'}, 'performer_stage': 'assessing'}
    result = await dispatch_performer(state)
    assert result['phase'] == 'dispatching'
    assert result['pipeline_admitted'] is False
    body.assert_not_awaited()



def test_recovery_can_claim_vacant_slot_but_not_another_cards_reservation():
    work = sessions(2)
    work['0']['phase'] = 'blocked'
    state = {'active_sessions': work, 'config': SimpleNamespace(max_concurrent_cards=1),
             '_pipeline_selected': set()}
    assert dispatch_has_pipeline_slot(state, '0')
    assert not dispatch_has_pipeline_slot(state, '1')
    assert state['_pipeline_selected'] == {'0'}
