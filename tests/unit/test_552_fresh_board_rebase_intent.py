from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import coordinare.daemon as daemon_module
import coordinare.graph.nodes.check_board as check_board_module
from coordinare.graph.state import initial_state
from coordinare.models.rebase import RebaseJob, RebaseOutcome
from coordinare.services import rebase


def session(card_id):
    return {"current_card": {"id": card_id, "pr_node_id": f"PR-{card_id}",
                              "pr_url": "https://github.com/acme/repo/pull/1"},
            "phase": "monitoring_pr", "workspace_branch": f"coordinare/{card_id}/work",
            "relay_feedback": [{"message": "retain instruction"}],
            "dispatched_feedback": {"stage": "implementing", "items": [{"message": "retained"}]},
            "last_rebase_attempt": {"main_sha": "earlier", "head_sha": "old-head", "outcome": "clean"}}


def setup(monkeypatch, column, edge):
    state = initial_state()
    parked = session("parked")
    sibling = session("sibling")
    state.update(active_sessions={"parked": parked, "sibling": sibling},
                 last_known_main_sha="old-main" if edge else "new-main",
                 board_snapshot={"IN_REVIEW": ["parked", "sibling"]})
    github = SimpleNamespace(
        poll_board=AsyncMock(return_value={"snapshot": {column: ["parked"], "IN_REVIEW": ["sibling"]}}),
        _current_token=AsyncMock(return_value="test-token"),
        get_pr_review_context=AsyncMock(return_value={"state": "OPEN"}),
        check_mergeability=AsyncMock(return_value={"mergeable_raw": "CONFLICTING", "head_ref_oid": "head"}),
    )
    config = SimpleNamespace(max_concurrent_cards=1)
    state.update(github_service=github, config=config)
    monkeypatch.setattr(check_board_module, "repo_url_from_config", lambda _: "https://github.com/acme/repo")
    monkeypatch.setattr(daemon_module, "repo_url_from_config", lambda _: "https://github.com/acme/repo")
    monkeypatch.setattr(rebase, "fetch_main_sha", AsyncMock(return_value="new-main"))
    monkeypatch.setattr(daemon_module, "fetch_main_sha", AsyncMock(return_value="new-main"))

    async def rebase_branch(repo, branch, sha, token):
        return RebaseJob(card_id=branch.split("/")[1], branch=branch,
                         target_main_sha=sha, outcome=RebaseOutcome.SKIPPED)

    rebasing = AsyncMock(side_effect=rebase_branch)
    monkeypatch.setattr(rebase, "rebase_branch", rebasing)
    return state, github, parked, rebasing


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["TODO", "BACKLOG", "DONE", "BLOCKED"])
@pytest.mark.parametrize("edge", [True, False])
async def test_actual_single_card_poll_filters_fresh_board_before_rebase(monkeypatch, column, edge):
    state, github, parked, rebasing = setup(monkeypatch, column, edge)
    before = deepcopy(parked)

    # Stop after the real board poll and rebase path, before unrelated admission.
    class AfterRebaseError(Exception):
        pass

    def checkpoint(*args):
        raise AfterRebaseError

    monkeypatch.setattr(check_board_module, "_refresh_current_card_metadata", checkpoint)
    with pytest.raises(AfterRebaseError):
        await check_board_module.check_board(state)
    github.poll_board.assert_awaited_once()
    assert state["board_snapshot"] == {column: ["parked"], "IN_REVIEW": ["sibling"]}
    rebasing.assert_awaited_once_with("https://github.com/acme/repo", "coordinare/sibling/work", "new-main", "test-token")
    assert parked == before


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["DONE", "BLOCKED"])
async def test_actual_daemon_preflight_protects_terminal_board_before_reconcile(monkeypatch, column):
    state, github, parked, rebasing = setup(monkeypatch, column, True)
    before = deepcopy(parked)
    await daemon_module._preflight_poll_board(state, github, state["active_sessions"])
    rebasing.assert_awaited_once_with("https://github.com/acme/repo", "coordinare/sibling/work", "new-main", "test-token")
    assert parked == before


@pytest.mark.parametrize("snapshot", [None, {}, {"DONE": "parked"}, {"TODO": {"parked": True}}, {"BLOCKED": None}])
@pytest.mark.parametrize("persisted_pause", [False, True])
def test_malformed_board_columns_do_not_infer_intent_or_clear_durable_pause(snapshot, persisted_pause):
    parked = session("parked")
    parked["board_paused"] = persisted_pause
    candidates = rebase.detect_stale_branches({"parked": parked}, "new-main", board_snapshot=snapshot)
    assert [item["card_id"] for item in candidates] == ([] if persisted_pause else ["parked"])
