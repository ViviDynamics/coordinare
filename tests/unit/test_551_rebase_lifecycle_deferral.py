from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.graph.nodes.check_board import (
    _proactive_rebase_stale_branches,
    _run_edge_rebase_round,
)
from coordinare.graph.nodes.dispatch_performer import _pre_dispatch_rebase_guard
from coordinare.models.rebase import RebaseJob, RebaseOutcome
from coordinare.services.rebase import run_rebase_round, should_attempt_rebase


def setup_state(lifecycle):
    card = {'id': 'card', 'pr_url': 'https://github.com/acme/repo/pull/1', 'pr_node_id': 'PR1'}
    github = SimpleNamespace(
        _current_token=AsyncMock(return_value='dummy-token'),
        check_mergeability=AsyncMock(return_value={'mergeable_raw': 'CONFLICTING', 'merge_state_status': 'DIRTY',
                                                   'head_ref_oid': 'head', 'head_ref_name': 'coordinare/card'}),
        get_pr_review_context=AsyncMock(),
    )
    if lifecycle == 'ERROR':
        github.get_pr_review_context.side_effect = RuntimeError('temporary lifecycle outage')
    else:
        github.get_pr_review_context.return_value = {'state': lifecycle}
    state = {'current_card': card, 'phase': 'dispatching', 'performer_stage': 'implementing',
             'github_service': github, 'config': SimpleNamespace(github_org='acme', project_name='repo'),
             'workspace_branch': 'coordinare/card', 'last_known_main_sha': 'main'}
    return state, github


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle', ['CLOSED', 'ERROR', ''])
async def test_actual_guard_defers_lifecycle_without_rebase_or_anti_thrash_marker(monkeypatch, lifecycle):
    state, github = setup_state(lifecycle)
    rebasing = AsyncMock()
    monkeypatch.setattr('coordinare.services.rebase.rebase_branch', rebasing)
    assert await _pre_dispatch_rebase_guard(state, 'card') is False
    rebasing.assert_not_awaited()
    assert 'last_rebase_attempt' not in state
    assert not state.get('relay_feedback')
    # Recovery of the same lifecycle/head can immediately try a real rebase.
    github.get_pr_review_context.side_effect = None
    github.get_pr_review_context.return_value = {'state': 'OPEN'}
    rebasing.return_value = RebaseJob(card_id='card', branch='coordinare/card', target_main_sha='main', outcome=RebaseOutcome.SKIPPED)
    assert await _pre_dispatch_rebase_guard(state, 'card') is True
    rebasing.assert_awaited_once()
    assert state['last_rebase_attempt']['outcome'] == 'skipped'
    assert should_attempt_rebase(state, 'main', 'head') is True


@pytest.mark.asyncio
@pytest.mark.parametrize('caller', ['proactive', 'edge'])
async def test_round_callers_do_not_stamp_marker_for_lifecycle_deferral(monkeypatch, caller):
    session, github = setup_state('ERROR')
    original_marker = {'main_sha': 'old-main', 'head_sha': 'old-head', 'outcome': 'clean'}
    session['last_rebase_attempt'] = dict(original_marker)
    state = {}
    rebasing = AsyncMock(return_value=RebaseJob(card_id='card', branch='coordinare/card', target_main_sha='main', outcome=RebaseOutcome.SKIPPED))
    monkeypatch.setattr('coordinare.services.rebase.rebase_branch', rebasing)
    invoke = _proactive_rebase_stale_branches if caller == 'proactive' else _run_edge_rebase_round
    await invoke(state, github, {'card': session}, 'main', 'old-main', 'https://github.com/acme/repo.git', 'dummy-token')
    assert state['last_rebase_round']['jobs'][0]['outcome'] == 'deferred'
    assert session['last_rebase_attempt'] == original_marker
    rebasing.assert_not_awaited()
    github.get_pr_review_context.side_effect = None
    github.get_pr_review_context.return_value = {'state': 'OPEN'}
    await invoke(state, github, {'card': session}, 'main', 'old-main', 'https://github.com/acme/repo.git', 'dummy-token')
    rebasing.assert_awaited_once()
    assert session['last_rebase_attempt']['outcome'] == 'skipped'


@pytest.mark.asyncio
async def test_active_worker_skip_and_lifecycle_deferral_remain_distinct(monkeypatch):
    paused, github = setup_state('ERROR')
    active, _ = setup_state('OPEN')
    active['phase'] = 'monitoring_performer'
    rebasing = AsyncMock()
    notification = SimpleNamespace(notify=AsyncMock())
    monkeypatch.setattr('coordinare.services.rebase.rebase_branch', rebasing)
    result = await run_rebase_round({'paused': paused, 'active': active}, 'main', 'https://github.com/acme/repo.git', 'dummy-token',
                                    github=github, notification_service=notification)
    assert [job.outcome.value for job in result.jobs] == ['deferred', 'skipped']
    assert result.summary == '1 skipped, 1 deferred'
    github.get_pr_review_context.assert_awaited_once()
    rebasing.assert_not_awaited()
    notification.notify.assert_not_awaited()
