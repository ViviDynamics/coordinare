from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import _persist_active_sessions, _restored_session_dict
from coordinare.graph.nodes.check_board import _collect_blocked_clarification, _reset_and_rehydrate
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import _set_current_card, initial_state
from coordinare.services.activity_log import ActivityLog
from coordinare.services.github import GET_PR_REVIEWS_QUERY, GitHubService
from coordinare.services.pipeline_budget import select_pipelines
from coordinare.session import create_session_from_card, state_to_session
from coordinare.state_store import WorkflowSnapshot


def make_state(pr_state="CLOSED"):
    state = initial_state()
    github = AsyncMock()
    github.get_pr_review_context.return_value = {
        "state": pr_state, "reviews": [], "review_decision": "APPROVED",
    }
    state["github_service"] = github
    state["activity_log"] = ActivityLog()
    card = {"id": "card", "issue_id": "issue", "status": "IN_REVIEW",
            "pr_node_id": "PR1", "pr_url": "https://github.com/acme/repo/pull/1"}
    state["active_sessions"] = {"card": create_session_from_card(card)}
    state["active_card_id"] = "card"
    _set_current_card(state, card)
    state["phase"] = "monitoring_pr"
    state["pipeline_admitted"] = True
    state["active_sessions"]["card"]["pipeline_admitted"] = True
    return state, github


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_phase", ["dispatching", "monitoring_performer", "monitoring_pr"])
async def test_closed_pr_parks_before_ci_gate(monkeypatch, gate_phase):
    state, github = make_state()
    gate = AsyncMock(return_value=({"phase": gate_phase}, gate_phase != "monitoring_pr"))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    await monitor_pr(state)
    assert state["phase"] == "blocked"
    assert state["current_card"]["status"] == "BLOCKED"
    assert state["active_sessions"]["card"]["current_card"] is state["current_card"]
    assert "closed without merging" in state["system_error_reason"]
    assert "Reopen" in state["system_error_reason"] and "Todo" in state["system_error_reason"]
    assert not state["pipeline_admitted"]
    gate.assert_not_awaited()
    github.move_card.assert_awaited_once_with("card", "BLOCKED")
    github.merge_pr.assert_not_awaited()
    queued = create_session_from_card({"id": "queued", "status": "TODO"})
    state["active_sessions"]["queued"] = queued
    assert select_pipelines(state["active_sessions"], 1) == {"queued"}
    assert list(state["activity_log"].snapshot())


@pytest.mark.asyncio
async def test_pr_context_exposes_state():
    service = GitHubService(token="test", org="acme", project_number=1)
    service._guarded_execute = AsyncMock(return_value={"node": {"state": "CLOSED"}})
    assert "state" in GET_PR_REVIEWS_QUERY.split("... on PullRequest {")[1].split("reviews(")[0]
    assert (await service.get_pr_review_context("PR1"))["state"] == "CLOSED"


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_state", ["CLOSED", "OPEN"])
async def test_restart_and_explicit_reopening_resume(monkeypatch, pr_state):
    state, github = make_state()
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    state["active_sessions"]["card"] = state_to_session(state)
    persisted = _persist_active_sessions(state["active_sessions"])["card"]
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked", active_card_id="card"), state["current_card"])
    assert restored["phase"] == "blocked"
    assert "closed without merging" in restored["system_error_reason"]
    assert not restored["pipeline_admitted"]
    state["active_sessions"] = {"card": restored}
    github.get_pr_review_context.return_value["state"] = pr_state
    await _reset_and_rehydrate(state, {}, ["card"], 1, state["active_sessions"])
    if pr_state == "OPEN":
        assert restored["phase"] == "monitoring_pr"
        assert restored["current_card"]["status"] == "IN_REVIEW"
        assert restored["current_card"]["pr_node_id"] == "PR1"
        assert not restored["system_error_reason"]
        github.move_card.assert_awaited_with("card", "IN_REVIEW")
    else:
        assert restored["phase"] == "blocked"
        assert restored["current_card"]["status"] == "BLOCKED"
        github.move_card.assert_awaited_with("card", "BLOCKED")


@pytest.mark.asyncio
async def test_closed_pr_ignores_generic_clarification_resume(monkeypatch):
    state, github = make_state()
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    state["active_sessions"]["card"] = state_to_session(state)
    state["active_sessions"]["card"]["last_blocked_notified_at"] = datetime.now(UTC)
    github.get_card.return_value = {"comments": {"nodes": [{"body": "please retry", "author": {"login": "human"}}]}}
    assert await _collect_blocked_clarification(state, github, {}, "card", {"card": "issue"}) is None
    github.get_card.assert_not_awaited()
    assert state["phase"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_pr", [True, False])
async def test_legacy_nonfocused_restart_only_resumes_same_reopened_pr(monkeypatch, same_pr):
    state, github = make_state()
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    persisted = _persist_active_sessions(state["active_sessions"])["card"]
    # Schema30 did not store per-card PR artefacts; retain that recovery coverage.
    from coordinare.state_store import PersistedSession

    persisted = PersistedSession.model_validate({
        key: value for key, value in persisted.model_dump().items() if key != "pr_artefacts"
    })
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle"), None)
    state["active_sessions"] = {"card": restored}
    github.find_pr_for_issue.return_value = {
        "pr_node_id": "PR1" if same_pr else "PR2",
        "pr_url": "https://github.com/acme/repo/pull/1" if same_pr else "https://github.com/acme/repo/pull/2",
    }
    github.get_pr_review_context.return_value["state"] = "OPEN"
    board = {"content_node_ids": {"card": "issue"}}
    await _reset_and_rehydrate(state, board, ["card"], 1, state["active_sessions"])
    assert restored["phase"] == ("monitoring_pr" if same_pr else "blocked")
    assert restored["current_card"]["status"] == ("IN_REVIEW" if same_pr else "BLOCKED")
    if same_pr:
        assert restored["current_card"]["pr_node_id"] == "PR1"


@pytest.mark.asyncio
async def test_closed_pr_block_cannot_requeue_from_old_answered_questions(monkeypatch):
    from coordinare.graph.nodes.handle_blocked import handle_blocked

    state, github = make_state()
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    state["card_clarifications"] = [
        {"author": "human", "body": "answered", "created_at": "2026-10-08T01:00:00Z"},
        {"author": "vivi-coordinare", "body": "old question", "created_at": "2026-10-08T02:00:00Z"},
    ]
    await handle_blocked(state)
    assert state["phase"] == "blocked"
    assert state["current_card"]["status"] == "BLOCKED"
    assert all(call.args[1] == "BLOCKED" for call in github.move_card.await_args_list)


@pytest.mark.asyncio
async def test_merged_pr_is_not_parked_as_unmerged(monkeypatch):
    state, _github = make_state("MERGED")
    gate = AsyncMock(return_value=({}, False))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    await monitor_pr(state)
    assert state["phase"] != "blocked"
    assert state["current_card"]["status"] != "BLOCKED"
    gate.assert_awaited_once()


@pytest.mark.asyncio
async def test_closed_pr_is_excluded_from_background_rebase(monkeypatch):
    from coordinare.services.rebase import detect_stale_branches

    state, _github = make_state()
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    state["active_sessions"]["card"]["workspace_branch"] = "coordinare/card/implementing"
    assert detect_stale_branches(state["active_sessions"], "main-sha") == []


@pytest.mark.asyncio
async def test_another_cards_closed_pr_reason_does_not_suppress_comment_poll(monkeypatch):
    state, github = make_state()
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    state["active_sessions"]["other"] = create_session_from_card({"id": "other", "status": "BLOCKED"})
    state["active_sessions"]["other"]["last_blocked_notified_at"] = datetime.now(UTC)
    github.get_card.return_value = {"comments": {"nodes": []}}
    await _collect_blocked_clarification(state, github, {}, "other", {"other": "other-issue"})
    github.get_card.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_state", ["CLOSED", "OPEN", "MERGED", "", "ERROR"])
async def test_edge_rebase_reads_live_pr_state_before_conflict_dispatch(monkeypatch, live_state):
    from coordinare.graph.nodes.check_board import _run_edge_rebase_round
    from coordinare.models.rebase import RebaseJob, RebaseOutcome

    state, github = make_state(live_state)
    session = state["active_sessions"]["card"]
    session["phase"] = "monitoring_pr"
    session["workspace_branch"] = "coordinare/card/implementing"
    job = RebaseJob(card_id="card", branch=session["workspace_branch"],
                    target_main_sha="main-new", outcome=RebaseOutcome.BLOCKED,
                    conflicted_files=["src/app.py"])
    rebase = AsyncMock(return_value=job)
    monkeypatch.setattr("coordinare.services.rebase.rebase_branch", rebase)
    if live_state == "ERROR":
        github.get_pr_review_context.side_effect = RuntimeError("GitHub unavailable")
    await _run_edge_rebase_round(state, github, state["active_sessions"], "main-new", "main-old", "https://github.com/acme/repo.git", "test")
    github.get_pr_review_context.assert_awaited_once_with("PR1")
    if live_state in {"OPEN", "MERGED"}:
        rebase.assert_awaited_once()
        assert session["phase"] == "dispatching"
    else:
        rebase.assert_not_awaited()
        assert session["phase"] == "monitoring_pr"
        assert state["last_rebase_round"]["jobs"][0]["outcome"] == "deferred"
        assert session["last_rebase_attempt"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("same_node", [True, False])
async def test_legacy_node_only_closed_reason_resumes_same_pr_after_nonfocus_restart(monkeypatch, same_node):
    state, github = make_state()
    state["current_card"].pop("pr_url")
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", AsyncMock(return_value=({}, False)))
    await monitor_pr(state)
    assert state["system_error_reason"].startswith("PR closed without merging: PR1. ")
    persisted = _persist_active_sessions(state["active_sessions"])["card"]
    # Schema30 did not store per-card PR artefacts; retain that recovery coverage.
    from coordinare.state_store import PersistedSession

    persisted = PersistedSession.model_validate({
        key: value for key, value in persisted.model_dump().items() if key != "pr_artefacts"
    })
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle"), None)
    state["active_sessions"] = {"card": restored}
    github.find_pr_for_issue.return_value = {
        "pr_node_id": "PR1" if same_node else "PR2",
        "pr_url": "https://github.com/acme/repo/pull/1" if same_node else "https://github.com/acme/repo/pull/2",
    }
    github.get_pr_review_context.return_value["state"] = "OPEN"
    await _reset_and_rehydrate(state, {"content_node_ids": {"card": "issue"}}, ["card"], 1, state["active_sessions"])
    assert restored["phase"] == ("monitoring_pr" if same_node else "blocked")
    if same_node:
        assert restored["current_card"]["pr_node_id"] == "PR1"
        assert restored["current_card"]["pr_url"].endswith("/pull/1")
    else:
        assert not restored["current_card"].get("pr_node_id")
