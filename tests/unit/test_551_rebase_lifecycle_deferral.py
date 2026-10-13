"""551 — an unreadable or closed PR lifecycle never produces a divergence request.

555: the request replaces the daemon-side rebase the original tests guarded.
The invariant is the same: no marker is stamped for a deferral, so recovery of
the lifecycle read can act on the very same (main, head).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.graph.nodes.check_board import _request_rebase_for_diverged_branches
from coordinare.graph.nodes.dispatch_performer import _pre_dispatch_rebase_guard
from coordinare.services.rebase import should_attempt_rebase


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
    config = SimpleNamespace(github_org='acme', project_name='repo',
                             implementer_rebase=SimpleNamespace(enabled=True))
    state = {'current_card': card, 'phase': 'dispatching', 'performer_stage': 'implementing',
             'github_service': github, 'config': config,
             'workspace_branch': 'coordinare/card', 'last_known_main_sha': 'main'}
    return state, github


@pytest.mark.asyncio
@pytest.mark.parametrize('lifecycle', ['CLOSED', 'ERROR', ''])
async def test_guard_dispatches_without_request_or_marker_on_lifecycle_deferral(lifecycle):
    state, github = setup_state(lifecycle)
    # 555: fail open — the dispatch goes ahead; it just carries no instruction.
    assert await _pre_dispatch_rebase_guard(state, 'card') is True
    assert 'last_rebase_attempt' not in state
    assert not state.get('relay_feedback')
    # Recovery of the same lifecycle/head can immediately issue the request.
    github.get_pr_review_context.side_effect = None
    github.get_pr_review_context.return_value = {'state': 'OPEN'}
    assert await _pre_dispatch_rebase_guard(state, 'card') is True
    assert state['last_rebase_attempt']['outcome'] == 'delegated'
    assert state['relay_feedback']
    assert should_attempt_rebase(state, 'main', 'head') is False


@pytest.mark.asyncio
async def test_sweep_does_not_stamp_marker_for_lifecycle_deferral():
    session, github = setup_state('ERROR')
    session['phase'] = 'monitoring_pr'
    original_marker = {'main_sha': 'old-main', 'head_sha': 'old-head', 'outcome': 'clean'}
    session['last_rebase_attempt'] = dict(original_marker)
    state = {}
    await _request_rebase_for_diverged_branches(state, github, {'card': session}, 'main', 'old-main')
    assert state['last_rebase_round']['jobs'][0]['outcome'] == 'deferred'
    assert session['last_rebase_attempt'] == original_marker
    assert not session.get('relay_feedback')
    github.get_pr_review_context.side_effect = None
    github.get_pr_review_context.return_value = {'state': 'OPEN'}
    await _request_rebase_for_diverged_branches(state, github, {'card': session}, 'main', 'old-main')
    assert session['last_rebase_attempt']['outcome'] == 'delegated'
    assert state['last_rebase_round']['jobs'][0]['outcome'] == 'delegated'


@pytest.mark.asyncio
async def test_active_worker_skip_and_lifecycle_deferral_remain_distinct():
    deferred, github = setup_state('ERROR')
    deferred['phase'] = 'monitoring_pr'
    active, _ = setup_state('OPEN')
    active['phase'] = 'monitoring_performer'
    state = {}
    await _request_rebase_for_diverged_branches(state, github, {'deferred': deferred, 'active': active}, 'main', 'main')
    # The active worker is skipped before any platform read; only the deferral is recorded.
    assert [job['card_id'] for job in state['last_rebase_round']['jobs']] == ['deferred']
    github.check_mergeability.assert_awaited_once()
    github.get_pr_review_context.assert_awaited_once()
    assert active['phase'] == 'monitoring_performer' and not active.get('relay_feedback')
