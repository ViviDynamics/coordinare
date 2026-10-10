from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback
from coordinare.graph.nodes.dispatch_performer import _base_card_context
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import PersistedSession, WorkflowSnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("review_state", ["COMMENTED", "CHANGES_REQUESTED"])
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("restart", [False, True])
async def test_review_arriving_before_completion_is_dispatched_once(review_state, inline, restart) -> None:
    completed = datetime.now(UTC)
    review = {"id": "during-turn", "author_login": "alice", "state": review_state,
              "body": "Add a single-word boundary regression.",
              "submitted_at": (completed - timedelta(minutes=1)).isoformat(),
              "comments": [{"body": "Cover mixed case", "path": "tests/test_sample.py", "line": 128}] if inline else []}
    state = initial_state()
    card = {"id": "card19", "pr_node_id": "PR21", "status": "IN_REVIEW"}
    state.update(current_card=card, phase="monitoring_pr", human_reviewers=["alice"],
                 lifecycle_completed_at=completed, processed_review_ids={"previously-addressed"})
    if restart:
        persisted = _persist_one_session("card19", state_to_session(state))
        persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
        restored = _restored_session_dict("card19", persisted, WorkflowSnapshot(snapshot_at=completed, phase="monitoring_pr", active_card_id="card19"), card)
        state = initial_state()
        session_to_state(restored, state)
        state["human_reviewers"] = ["alice"]
    github = SimpleNamespace(get_pr_reviews=AsyncMock(return_value=[review]))
    state["github_service"] = github
    backend = AsyncMock()
    backend.prompt.return_value = {"data": [], "text": "[]"}
    state["conducting_backend"] = backend
    state = await monitor_pr(state)
    assert state["phase"] == "relay_feedback"
    state = await classify_human_feedback(state)
    assert state["phase"] == "dispatching"
    assert state["performer_stage"] == "implementing"
    payload, _ = _base_card_context(state, card, "card19", "implementing")
    assert payload["relay_feedback"][0]["id"] == "during-turn"
    assert payload["relay_feedback"][0]["body"] == review["body"]
    assert payload["relay_feedback"][0]["comments"] == review["comments"]
    assert "during-turn" in state["processed_review_ids"]
    state["phase"] = "monitoring_pr"
    state = await monitor_pr(state)
    assert state["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_older_approval_supersedes_change_request_without_authorizing_new_cycle_merge() -> None:
    completed = datetime.now(UTC)
    state = initial_state()
    state.update(current_card={"id": "card", "pr_node_id": "PR"}, human_reviewers=["alice"], lifecycle_completed_at=completed)
    state["github_service"] = SimpleNamespace(get_pr_reviews=AsyncMock(return_value=[
        {"id": "request", "author_login": "alice", "state": "CHANGES_REQUESTED", "body": "Fix", "submitted_at": (completed - timedelta(minutes=2)).isoformat()},
        {"id": "approval", "author_login": "alice", "state": "APPROVED", "submitted_at": (completed - timedelta(minutes=1)).isoformat()},
    ]))
    state = await monitor_pr(state)
    assert state["phase"] == "monitoring_pr"
    assert state["pending_reviews"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("as_string", [False, True])
async def test_naive_completion_timestamp_keeps_approval_comparison_valid(as_string) -> None:
    cutoff = datetime(2026, 10, 8, 20, 0)
    state = initial_state()
    state.update(current_card={"id": "card", "pr_node_id": "PR"}, human_reviewers=["alice"], lifecycle_completed_at=cutoff.isoformat() if as_string else cutoff)
    state["github_service"] = SimpleNamespace(get_pr_reviews=AsyncMock(return_value=[
        {"id": "approval", "author_login": "alice", "state": "APPROVED", "submitted_at": "2026-10-08T20:01:00"},
    ]))
    assert (await monitor_pr(state))["phase"] == "merging"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/coordinare skip-implementer", "/coordinare restart-from implementing"])
async def test_accepted_command_is_durable_and_deduplicated_without_losing_other_reviews(command) -> None:
    now = datetime.now(UTC)
    command_review = {"id": "command", "author_login": "alice", "state": "COMMENTED", "body": command, "submitted_at": now.isoformat()}
    unrelated = {"id": "other", "author_login": "bob", "state": "COMMENTED", "body": "Fix the bug", "submitted_at": now.isoformat()}
    state = initial_state()
    card = {"id": "card", "pr_node_id": "PR", "status": "IN_REVIEW"}
    state.update(current_card=card, lifecycle_sequence=["implementing", "reviewing"],
                 pending_reviews=[command_review, unrelated], human_reviewers=["alice", "bob"])
    await classify_human_feedback(state)
    assert "command" in state["processed_review_ids"]
    assert "other" not in state["processed_review_ids"]
    persisted = _persist_one_session("card", state_to_session(state))
    persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=now, phase="dispatching", active_card_id="card"), card)
    fresh = initial_state()
    session_to_state(restored, fresh)
    assert fresh["pending_override"] == state["pending_override"]
    fresh["pending_override"] = None  # command has executed
    fresh["phase"] = "monitoring_pr"
    fresh["human_reviewers"] = ["alice", "bob"]
    fresh["github_service"] = SimpleNamespace(get_pr_reviews=AsyncMock(return_value=[command_review, unrelated]))
    await monitor_pr(fresh)
    assert [review["id"] for review in fresh["pending_reviews"]] == ["other"]


def test_override_snapshot_contract_advances_with_empty_legacy_default() -> None:
    from coordinare.state_store import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 34
    assert PersistedSession(card_id="legacy").pending_override is None
