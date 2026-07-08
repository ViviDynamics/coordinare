"""129 (US1): check_board blocked-card auto-recovery (stale-review increment)."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.check_board import _attempt_blocked_card_recovery


class _FakeGitHub:
    def __init__(self, ctx: dict[str, Any], *, move_raises: bool = False) -> None:
        self._ctx = ctx
        self.moved: list[tuple[str, str]] = []
        self._move_raises = move_raises

    async def find_pr_for_issue(self, issue_node: str) -> dict[str, Any]:
        return {"pr_node_id": "PR1"}

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx

    async def move_card(self, card_id: str, column: str) -> None:
        if self._move_raises:
            raise RuntimeError("move failed")
        self.moved.append((card_id, column))


class _Notify:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def dispatch(self, e: Any) -> None:
        self.events.append(e)


def _ctx(threads, *, decision="CHANGES_REQUESTED", head="newsha", commit="oldsha"):
    return {
        "reviews": [{"id": "RVW_1", "author_login": "jason", "state": "CHANGES_REQUESTED", "commit_oid": commit}],
        "review_threads": threads,
        "head_oid": head,
        "review_decision": decision,
    }


def _state(notify):
    return {
        "human_reviewers": ["jason"],
        "notification_service": notify,
    }


_BOARD = {"content_node_ids": {"CARD_1": "ISSUE_1"}}


@pytest.mark.asyncio
async def test_disabled_by_default_no_op(monkeypatch) -> None:
    monkeypatch.delenv("COORDINARE_BLOCKED_RECOVERY", raising=False)
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    await _attempt_blocked_card_recovery(_state(notify), gh, ["CARD_1"], _BOARD)
    assert gh.moved == [] and notify.events == []  # gate off → nothing happens


@pytest.mark.asyncio
async def test_stale_addressed_recovers_to_in_review(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(notify)
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)
    assert ("CARD_1", "IN_REVIEW") in gh.moved
    assert len(notify.events) == 1 and notify.events[0].event_type.value == "card_auto_recovered"
    assert st["_recovery_attempts"].get("CARD_1") is True  # anti-thrash marker


@pytest.mark.asyncio
async def test_genuine_changes_requested_not_recovered(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    # threads unresolved → not stale-addressed → stays blocked (human-gate safety)
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": False, "review_id": "RVW_1"}]))
    notify = _Notify()
    await _attempt_blocked_card_recovery(_state(notify), gh, ["CARD_1"], _BOARD)
    assert gh.moved == [] and notify.events == []


@pytest.mark.asyncio
async def test_anti_thrash_one_attempt_per_run(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    st = _state(_Notify())
    st["_recovery_attempts"] = {"CARD_1": True}  # already attempted this run
    await _attempt_blocked_card_recovery(st, gh, ["CARD_1"], _BOARD)
    assert gh.moved == []  # skipped by anti-thrash


@pytest.mark.asyncio
async def test_failsafe_on_move_error(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]), move_raises=True)
    # must NOT raise
    await _attempt_blocked_card_recovery(_state(_Notify()), gh, ["CARD_1"], _BOARD)


@pytest.mark.asyncio
async def test_no_pr_no_recovery(monkeypatch) -> None:
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([]))
    # board without a content node for the card → can't find issue/PR → skip
    await _attempt_blocked_card_recovery(_state(_Notify()), gh, ["CARD_1"], {"content_node_ids": {}})
    assert gh.moved == []


@pytest.mark.asyncio
async def test_recovered_card_pruned_from_snapshot_and_blocked_list(monkeypatch) -> None:
    # Adversarial finding: a recovered card must be dropped from the in-memory
    # board snapshot AND the live blocked list so the downstream blocked-handling
    # branch does not re-adopt it as BLOCKED in the same cycle.
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(notify)
    st["board_snapshot"] = {"BLOCKED": ["CARD_1", "CARD_2"]}
    blocked = st["board_snapshot"]["BLOCKED"]  # caller aliases the snapshot list
    await _attempt_blocked_card_recovery(st, gh, blocked, _BOARD)
    assert ("CARD_1", "IN_REVIEW") in gh.moved
    assert "CARD_1" not in st["board_snapshot"]["BLOCKED"]  # pruned from snapshot
    assert "CARD_1" not in blocked  # pruned from the live list (identity preserved)
    assert "CARD_2" in st["board_snapshot"]["BLOCKED"]  # untouched card stays


@pytest.mark.asyncio
async def test_notification_target_matches_decision_stage(monkeypatch) -> None:
    # Adversarial finding: notification target must reflect the actual recovery
    # target stage, not a hardcoded "IN_REVIEW".
    monkeypatch.setenv("COORDINARE_BLOCKED_RECOVERY", "1")
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    await _attempt_blocked_card_recovery(_state(notify), gh, ["CARD_1"], _BOARD)
    assert len(notify.events) == 1
    moved_target = dict(gh.moved)["CARD_1"]
    assert notify.events[0].payload["target"] == moved_target
