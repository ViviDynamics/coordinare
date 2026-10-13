"""#96, #100: the closer no longer wedges on an approval it already has, and
every decision says why.

#96, as observed on conductor-e2e-sample PRs 68 and 69: a human approved, the
daemon's closing review then ran and finished after the approval without
pushing anything, and the closer stopped counting the approval because it
"predated the lifecycle". It waited for a second approval of the same commit.
An approval is now judged by the commit it was given on.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import structlog.testing

from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import (
    _WAIT_ESCALATE_AFTER,
    _WAIT_REPEAT_EVERY,
    _approval_is_current,
    _log_merge_decision,
    monitor_pr,
)
from coordinare.graph.state import initial_state

_HEAD = "a" * 40
_OLD = "b" * 40
_APPROVED_AT = "2026-10-12T23:22:41Z"
_CLOSER_DONE = "2026-10-12T23:23:37Z"   # after the approval


class _GitHub:
    def __init__(self, reviews: list[dict[str, Any]], *, head: str = _HEAD, decision: str = "APPROVED") -> None:
        self._ctx = {"reviews": reviews, "review_threads": [], "head_oid": head,
                     "review_decision": decision, "state": "OPEN"}

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx


def _approval(commit: str = _HEAD, at: str = _APPROVED_AT, login: str = "jason") -> dict[str, Any]:
    return {"id": f"RVW_{login}_{at}", "author_login": login, "state": "APPROVED",
            "commit_oid": commit, "submitted_at": at, "comments": []}


def _closer_comment(at: str = _CLOSER_DONE) -> dict[str, Any]:
    return {"id": "RVW_BOT", "author_login": "vivi-coordinare[bot]", "state": "COMMENTED",
            "commit_oid": _HEAD, "submitted_at": at, "body": "Closing review: all threads resolved.", "comments": []}


def _state(github: _GitHub, *, completed_at: str | None = _CLOSER_DONE) -> dict:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1", "id": "CARD_96", "title": "is_anagram"}
    state["human_reviewers"] = ["jason"]
    state["github_service"] = github
    state["lifecycle_completed_at"] = completed_at
    return state


@pytest.fixture(autouse=True)
def _clean_wait_counters():
    monitor_pr_module._merge_waits.clear()
    yield
    monitor_pr_module._merge_waits.clear()


def _decisions(logs: list[dict]) -> list[dict]:
    return [entry for entry in logs if entry["event"] == "monitor_pr.merge_decision"]


# --- #96: the wedge ------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_approval_of_the_current_head_merges_even_when_the_closer_finished_after_it() -> None:
    """The exact sequence from PR 68: approve, then the daemon's closing review."""
    state = _state(_GitHub([_approval(), _closer_comment()]))
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "merging"
    (decision,) = _decisions(logs)
    assert decision["outcome"] == "merge" and decision["reason"] == "approved"
    assert decision["approvals"] == 1 and decision["stale_approvals"] == 0


@pytest.mark.asyncio
async def test_approval_given_on_an_older_commit_does_not_merge_and_says_why() -> None:
    """Commits were pushed after the approval: the reviewer has not seen what would merge."""
    state = _state(_GitHub([_approval(commit=_OLD)]), completed_at=None)
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    (decision,) = _decisions(logs)
    assert decision["outcome"] == "waiting" and decision["reason"] == "approval_predates_head"
    assert decision["approved_commit"] == _OLD[:7] and decision["head"] == _HEAD[:7]
    assert decision["stale_approvals"] == 1


@pytest.mark.asyncio
async def test_old_commit_approval_is_refused_even_when_it_is_newer_than_the_lifecycle() -> None:
    """The time rule alone would have merged this one; the commit rule does not."""
    state = _state(_GitHub([_approval(commit=_OLD, at="2026-10-12T23:59:00Z")]))
    assert (await monitor_pr(state))["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_a_second_approval_is_no_longer_needed() -> None:
    """Before the fix, only an approval submitted after the closer's comment merged."""
    first_only = _state(_GitHub([_approval(), _closer_comment()]))
    with_second = _state(_GitHub([_approval(), _closer_comment(), _approval(at="2026-10-12T23:30:00Z")]))
    assert (await monitor_pr(first_only))["phase"] == "merging"
    assert (await monitor_pr(with_second))["phase"] == "merging"


@pytest.mark.asyncio
async def test_changes_requested_still_blocks_a_current_approval() -> None:
    reviews = [_approval(), {"id": "RVW_CR", "author_login": "sam", "state": "CHANGES_REQUESTED",
                              "commit_oid": _HEAD, "submitted_at": "2026-10-12T23:25:00Z", "comments": []}]
    state = _state(_GitHub(reviews, decision="CHANGES_REQUESTED"))
    state["human_reviewers"] = ["jason", "sam"]
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "relay_feedback"
    assert _decisions(logs)[0]["reason"] == "actionable_feedback"


@pytest.mark.parametrize(("review", "cutoff", "head", "expected"), [
    # Both commits known: only the commit decides.
    ({"commit_oid": _HEAD, "submitted_at": "2020-01-01T00:00:00Z"}, datetime(2026, 1, 1, tzinfo=UTC), _HEAD, True),
    ({"commit_oid": _OLD, "submitted_at": "2030-01-01T00:00:00Z"}, datetime(2026, 1, 1, tzinfo=UTC), _HEAD, False),
    # Either unknown: the time rule, as before.
    ({"submitted_at": "2020-01-01T00:00:00Z"}, datetime(2026, 1, 1, tzinfo=UTC), _HEAD, False),
    ({"submitted_at": "2030-01-01T00:00:00Z"}, datetime(2026, 1, 1, tzinfo=UTC), _HEAD, True),
    ({"commit_oid": _HEAD, "submitted_at": "2020-01-01T00:00:00Z"}, datetime(2026, 1, 1, tzinfo=UTC), "", False),
    ({"commit_oid": "", "submitted_at": "2020-01-01T00:00:00Z"}, None, _HEAD, True),
    ({"commit_oid": "  ", "submitted_at": "not a date"}, datetime(2026, 1, 1, tzinfo=UTC), _HEAD, True),
])
def test_approval_is_current(review, cutoff, head, expected) -> None:
    assert _approval_is_current(review, cutoff, head) is expected


# --- #100: every decision says why --------------------------------------------- #


@pytest.mark.asyncio
async def test_waiting_with_no_approval_says_so_with_the_values_it_saw() -> None:
    state = _state(_GitHub([_closer_comment()], decision="REVIEW_REQUIRED"))
    with structlog.testing.capture_logs() as logs:
        await monitor_pr(state)
    (decision,) = _decisions(logs)
    assert decision["outcome"] == "waiting" and decision["reason"] == "no_human_approval"
    assert decision["review_decision"] == "REVIEW_REQUIRED"
    assert decision["card_id"] == "CARD_96" and decision["head"] == _HEAD[:7]


@pytest.mark.asyncio
async def test_merge_held_by_githubs_aggregate_review_state_is_named() -> None:
    state = _state(_GitHub([_approval()], decision="CHANGES_REQUESTED"))
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert [d["reason"] for d in _decisions(logs)] == ["changes_requested_outstanding"]


def _repeat(times: int, reason: str = "no_human_approval", **observed) -> list[dict]:
    with structlog.testing.capture_logs() as logs:
        for _ in range(times):
            _log_merge_decision("CARD", "waiting", reason, **observed)
    return [entry for entry in logs if entry["event"] == "monitor_pr.merge_wait_repeated"]


def test_an_identical_wait_escalates_once_then_periodically() -> None:
    assert _repeat(_WAIT_ESCALATE_AFTER - 1, head="aaaaaaa") == []
    (first,) = _repeat(1, head="aaaaaaa")
    assert first["log_level"] == "warning"
    assert first["consecutive_checks"] == _WAIT_ESCALATE_AFTER and first["reason"] == "no_human_approval"
    assert _repeat(_WAIT_REPEAT_EVERY - 1, head="aaaaaaa") == []
    (again,) = _repeat(1, head="aaaaaaa")
    assert again["consecutive_checks"] == _WAIT_ESCALATE_AFTER + _WAIT_REPEAT_EVERY


def test_a_change_in_reason_or_observed_values_restarts_the_count() -> None:
    assert _repeat(_WAIT_ESCALATE_AFTER - 1, head="aaaaaaa") == []
    assert _repeat(_WAIT_ESCALATE_AFTER - 1, head="bbbbbbb") == []          # the head moved
    assert _repeat(_WAIT_ESCALATE_AFTER - 1, "approval_predates_head", head="bbbbbbb") == []  # the reason changed
    assert len(_repeat(1, "approval_predates_head", head="bbbbbbb")) == 1


def test_a_merge_or_relay_clears_the_count() -> None:
    assert _repeat(_WAIT_ESCALATE_AFTER - 1) == []
    _log_merge_decision("CARD", "merge", "approved")
    assert "CARD" not in monitor_pr_module._merge_waits
    assert _repeat(_WAIT_ESCALATE_AFTER - 1) == []


def test_counts_are_kept_per_card() -> None:
    for _ in range(_WAIT_ESCALATE_AFTER - 1):
        _log_merge_decision("A", "waiting", "no_human_approval")
        _log_merge_decision("B", "waiting", "no_human_approval")
    with structlog.testing.capture_logs() as logs:
        _log_merge_decision("A", "waiting", "no_human_approval")
    assert [entry["card_id"] for entry in logs if entry["event"] == "monitor_pr.merge_wait_repeated"] == ["A"]


def test_checks_gate_forward_log_carries_its_reason() -> None:
    import inspect

    from coordinare.graph.nodes.monitor import gates

    source = inspect.getsource(gates)
    forward = source[source.index('action="FORWARD",'):]
    assert "reason=decision.reason," in forward[:400]


def _change_request(at: str, login: str = "jason") -> dict[str, Any]:
    return {"id": f"RVW_CR_{login}", "author_login": login, "state": "CHANGES_REQUESTED",
            "commit_oid": _HEAD, "submitted_at": at, "body": "Please fix the empty input case.", "comments": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["", "CHANGES_REQUESTED"])
async def test_an_approval_its_author_replaced_with_a_change_request_does_not_merge(decision: str) -> None:
    """Approve, then request changes on the same commit. The change request was
    already relayed, so the batch no longer holds it; the approval must not
    come back as the reviewer's last word, whatever the aggregate state says."""
    change_request = _change_request("2026-10-12T23:30:00Z")
    state = _state(_GitHub([_approval(), change_request], decision=decision), completed_at="2026-10-12T23:40:00Z")
    state["processed_review_ids"] = {change_request["id"]}
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    if not decision:
        assert _decisions(logs)[-1]["reason"] == "approval_withdrawn_by_reviewer"


@pytest.mark.asyncio
async def test_a_fresh_approval_after_a_relayed_change_request_merges() -> None:
    change_request = _change_request("2026-10-12T23:00:00Z")
    state = _state(_GitHub([change_request, _approval()]))
    state["processed_review_ids"] = {change_request["id"]}
    result = await monitor_pr(state)
    assert result["phase"] == "merging"


@pytest.mark.asyncio
async def test_another_reviewers_change_request_does_not_withdraw_this_approval() -> None:
    """Withdrawal is per reviewer. GitHub's aggregate review state is what holds
    a merge for someone else's open change request (covered above)."""
    other = _change_request("2026-10-12T23:30:00Z", login="sam")
    state = _state(_GitHub([_approval(), other], decision=""))
    state["human_reviewers"] = ["jason", "sam"]
    state["processed_review_ids"] = {other["id"]}
    result = await monitor_pr(state)
    assert result["phase"] == "merging"
