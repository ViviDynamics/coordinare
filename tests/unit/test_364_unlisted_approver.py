"""#364: an approval from a login outside ``human_reviewers`` is no longer dropped in silence.

The allow-list stays the policy: only listed logins can approve a merge. What changes is that
a current approval from any other login gives the wait its own reason, names the login in the
decision log and on the card, and that a saved reviewer list reaches the running daemon.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from coordinare.config import (
    CoordinareConfiguration,
    OrchestraConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import UNLISTED_APPROVER_REASON, monitor_pr
from coordinare.graph.state import SymphonyRuntimeState, initial_state

_HEAD = "a" * 40
_OLD = "b" * 40


class _GitHub:
    def __init__(self, reviews: list[dict[str, Any]], *, decision: str = "APPROVED") -> None:
        self._ctx = {"reviews": reviews, "review_threads": [], "head_oid": _HEAD,
                     "review_decision": decision, "state": "OPEN",
                     "merge_state_status": "CLEAN", "checks_state": "SUCCESS"}

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx


def _review(login: str, state: str = "APPROVED", *, commit: str = _HEAD,
            at: str = "2026-10-23T10:00:00Z") -> dict[str, Any]:
    return {"id": f"RVW_{login}_{state}_{at}", "author_login": login, "state": state,
            "commit_oid": commit, "submitted_at": at, "comments": []}


def _state(reviews: list[dict[str, Any]], *, decision: str = "APPROVED") -> dict:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1", "id": "CARD_364", "title": "approved PR"}
    state["human_reviewers"] = ["jason"]
    state["github_service"] = _GitHub(reviews, decision=decision)
    return state


@pytest.fixture(autouse=True)
def _clean_wait_counters():
    monitor_pr_module._merge_waits.clear()
    yield
    monitor_pr_module._merge_waits.clear()


def _decision(logs: list[dict]) -> dict:
    (entry,) = [e for e in logs if e["event"] == "monitor_pr.merge_decision"]
    return entry


# --- the closer ---------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_current_approval_from_an_unlisted_login_names_the_login() -> None:
    """The reproduce from the issue: GitHub says APPROVED, the approver is not listed."""
    state = _state([_review("Alex-Teammate")])
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    decision = _decision(logs)
    assert decision["outcome"] == "waiting"
    assert decision["reason"] == UNLISTED_APPROVER_REASON == "approval_from_unlisted_reviewer"
    assert decision["unlisted_approvers"] == ["Alex-Teammate"]
    assert decision["review_decision"] == "APPROVED"
    assert result["review_wait_reason"] == UNLISTED_APPROVER_REASON
    detail = result["review_wait_detail"]
    assert "Alex-Teammate" in detail and "human_reviewers" in detail


@pytest.mark.asyncio
async def test_a_listed_approval_still_merges_beside_an_unlisted_one() -> None:
    state = _state([_review("alex"), _review("Jason", at="2026-10-23T10:05:00Z")])
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "merging"
    assert _decision(logs)["reason"] == "approved"
    assert result["review_wait_detail"] is None


@pytest.mark.asyncio
async def test_an_unlisted_approval_of_an_older_commit_is_not_reported() -> None:
    state = _state([_review("alex", commit=_OLD)], decision="REVIEW_REQUIRED")
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert _decision(logs)["reason"] == "no_human_approval"
    assert result["review_wait_detail"] is None


@pytest.mark.asyncio
async def test_an_unlisted_approval_its_author_withdrew_is_not_reported() -> None:
    reviews = [_review("alex"), _review("alex", "CHANGES_REQUESTED", at="2026-10-23T11:00:00Z")]
    state = _state(reviews, decision="CHANGES_REQUESTED")
    with structlog.testing.capture_logs() as logs:
        await monitor_pr(state)
    assert _decision(logs)["reason"] == "no_human_approval"


@pytest.mark.asyncio
async def test_the_named_login_clears_once_a_listed_reviewer_approves() -> None:
    state = _state([_review("alex")])
    await monitor_pr(state)
    assert state["review_wait_reason"] == UNLISTED_APPROVER_REASON
    state["github_service"] = _GitHub([_review("alex"), _review("jason", at="2026-10-23T10:05:00Z")])
    result = await monitor_pr(state)
    assert result["phase"] == "merging"
    assert result["review_wait_detail"] is None


# --- the daemon: a saved reviewer list reaches the graph ------------------------ #


def _make_daemon() -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})

    async def _no_sleep(_: int) -> None:
        return None

    return CoordinareDaemon(graph, poll_interval_seconds=1, heartbeat_interval_seconds=1,
                            max_cycles=1, sleep_func=_no_sleep)


@pytest.mark.asyncio
async def test_a_saved_reviewer_list_reaches_the_running_daemon_on_reload() -> None:
    """The meeting's case: the login was saved in global settings and Check now was pressed."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(
            "github_org: acme\n"
            "github_token: token\n"
            "human_reviewers: [alice, alex-teammate]\n"
            "trusted_bot_reviewers: [helper-bot]\n"
            "symphonies:\n"
            "  - name: sym-0\n"
            "    github_project_number: 100\n"
            "orchestra:\n"
            "  mode: shared_pool\n",
        )
        config_file = Path(f.name)
    try:
        daemon = _make_daemon()
        daemon._state["config_path"] = config_file
        daemon._state["human_reviewers"] = ["alice"]
        daemon._state["trusted_bot_reviewers"] = []
        daemon._state["symphony_configs"] = {"sym-0": SymphonyConfig(name="sym-0", github_project_number=100)}
        daemon._state["symphony_states"] = {"sym-0": SymphonyRuntimeState(name="sym-0")}

        await daemon._handle_config_reload()

        assert daemon._state["human_reviewers"] == ["alice", "alex-teammate"]
        assert daemon._state["trusted_bot_reviewers"] == ["helper-bot"]
    finally:
        config_file.unlink(missing_ok=True)


def test_a_symphony_reviewer_override_applies_to_its_own_cycle_only() -> None:
    global_cfg = ProjectConfiguration(project_name="Demo", github_org="acme", github_project_number=1,
                                      github_token="token", human_reviewers=["alice"])
    sym = SymphonyConfig(name="sym-0", github_project_number=100,
                         overrides={"human_reviewers": ["carol"]})
    CoordinareConfiguration(global_config=global_cfg, symphonies=[sym],
                            orchestra=OrchestraConfig(mode="shared_pool"))
    daemon = _make_daemon()
    daemon._state["config"] = global_cfg
    daemon._state["human_reviewers"] = ["alice"]
    daemon._state["trusted_bot_reviewers"] = []

    swapped = daemon._swap_symphony_state_in("sym-0", None, {}, sym.effective_config(global_cfg))
    assert daemon._state["human_reviewers"] == ["carol"]

    daemon._restore_symphony_swaps("sym-0", *swapped)
    assert daemon._state["human_reviewers"] == ["alice"]


@pytest.mark.asyncio
async def test_a_merge_held_by_an_earlier_github_error_says_so() -> None:
    """Candidate 3 from the issue: the deferred merge used to return with no log line."""
    from coordinare.graph.nodes.github_retry import defer_github_operation

    state = _state([_review("jason")])
    defer_github_operation(state, operation=f"merge_pr:PR_1:{_HEAD}", error=RuntimeError("502 from GitHub"))
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    decision = _decision(logs)
    assert decision["outcome"] == "waiting" and decision["reason"] == "merge_retry_backoff"
    assert decision["approvals"] == 1 and decision["retry_in_seconds"] > 0
