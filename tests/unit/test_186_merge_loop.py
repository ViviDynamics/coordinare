"""#186 round four: a merge that keeps failing is one hold episode, through the real production loop.

Every tick in production starts with the real ``check_board``, which sets an IN_REVIEW
card back to ``monitoring_pr``; ``monitor_pr`` then re-runs its approved branch, and a
failing ``merge_pr`` leaves the card in ``merging``. The loop is approved -> merging ->
check_board reset -> monitoring_pr -> approved. Each node here is the real one, driven
through the real daemon fanout, with a stub GitHub service that stands in for GitHub only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare import daemon as daemon_module
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes import merge_pr as merge_module
from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.services import pipeline_budget
from coordinare.services.pipeline_budget import dispatch_has_pipeline_slot

_HEAD = "a" * 40
_BOUND = 900
_CYCLE = 60
_STUCK = "X"
_NEXT = "next"


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(pipeline_budget, "hold_clock", fake)
    monkeypatch.setattr(monitor_pr_module, "github_operation_ready", lambda _state, _op: (True, 0.0))
    daemon_module._review_wait_logged.clear()
    return fake


def _pr(cid: str, number: int) -> dict[str, Any]:
    return {"id": cid, "content_id": cid, "title": cid, "status": "IN_REVIEW",
            "pr_node_id": f"PR_{number}", "pr_number": number,
            "pr_url": f"https://github.com/o/r/pull/{number}", "issue_number": number}


class _Github:
    """Stub GitHub service: the board, an approved PR whose merge keeps failing."""

    def __init__(self) -> None:
        self.merge_calls = 0

    async def poll_board(self) -> dict[str, Any]:
        snapshot = {"TODO": [_NEXT], "IN_REVIEW": [_STUCK], "IN_PROGRESS": [], "BACKLOG": [], "BLOCKED": [], "DONE": []}
        return {
            "snapshot": snapshot,
            "titles": {_STUCK: _STUCK, _NEXT: _NEXT},
            "descriptions": {_STUCK: "", _NEXT: "Desc"},
            "issue_numbers": {_STUCK: 81, _NEXT: 82},
            "issue_urls": {_STUCK: "https://github.com/o/r/issues/81", _NEXT: "https://github.com/o/r/issues/82"},
            "content_node_ids": {_STUCK: "node_X", _NEXT: "node_next"},
            "item_labels": {},
            "item_field_values": {},
            "pr_urls": {_STUCK: "https://github.com/o/r/pull/81"},
        }

    async def get_pr_review_context(self, pr_node_id: str) -> dict[str, Any]:
        return {"reviews": [{"id": "RVW_jason", "author_login": "jason", "state": "APPROVED",
                             "commit_oid": _HEAD, "submitted_at": "2026-10-12T23:22:41Z", "comments": []}],
                "review_threads": [], "head_oid": _HEAD, "review_decision": "APPROVED", "state": "OPEN"}

    async def get_pr_conversation_comments(self, number: int, since: object = None) -> list[dict[str, Any]]:
        return []

    async def check_mergeability(self, pr_node_id: str) -> dict[str, Any]:
        self.merge_calls += 1
        raise RuntimeError("502 Bad Gateway from upstream")

    async def squash_merge(self, pr_node_id: str) -> dict[str, Any]:
        return {}

    async def get_branch_head_sha(self, branch: str) -> str:
        return _HEAD

    async def get_branch_protection_status(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}


class _Graph:
    """The production tick: check_board first, then the node for the phase it left the card in."""

    async def ainvoke(self, state: Any) -> Any:
        card_id = str(state.get("active_card_id"))
        state = await check_board(state)
        if state.get("phase") == "monitoring_pr" and card_id == _STUCK:
            state = await monitor_pr(state)
        if state.get("phase") == "merging" and card_id == _STUCK:
            state = await merge_module.merge_pr(state)
        if state.get("phase") in {"relay_feedback", "dispatching"}:
            if dispatch_has_pipeline_slot(state, card_id):
                state["phase"] = "monitoring_performer"
                state["agent_dispatch"] = {"session_id": f"job-{card_id}"}
            else:
                state["phase"] = "dispatching"
        return state


def _daemon(github: _Github) -> CoordinareDaemon:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1, sleep_func=lambda _s: None)
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=1, max_cards_in_review=None,
                                              review_slot_hold_seconds=_BOUND, max_closed_pr_attempts_per_issue=3)
    daemon._state["github_service"] = github
    daemon._state["human_reviewers"] = ["jason"]
    daemon._state["active_sessions"] = {
        _STUCK: {"current_card": _pr(_STUCK, 81), "phase": "monitoring_pr", "performer_stage": "closing_review",
                 "pipeline_admitted": True},
        _NEXT: {"current_card": {"id": _NEXT, "content_id": _NEXT, "title": _NEXT, "status": "TODO"},
                "phase": "dispatching", "performer_stage": "assessing", "pipeline_admitted": False},
    }
    return daemon


def test_a_merge_that_keeps_failing_through_the_real_loop_releases_its_slot(clock: _Clock) -> None:
    github = _Github()
    daemon = _daemon(github)
    released_at: int | None = None
    calls_when_released = 0
    for cycle in range(40):
        clock.advance(_CYCLE)
        asyncio.run(daemon._invoke_multi_session())
        stuck = daemon._state["active_sessions"].get(_STUCK) or {}
        if stuck.get("review_wait") == "human" and released_at is None:
            released_at = cycle
            calls_when_released = github.merge_calls
        if released_at is not None and cycle > released_at:
            # The released card stays released (it does not come back to automation) ...
            assert stuck.get("review_wait") == "human", f"cycle {cycle}: released card came back as {stuck.get('review_wait')}"
    bound_cycles = _BOUND // _CYCLE + 1
    assert released_at is not None and released_at <= bound_cycles
    assert _next_dispatched(daemon), "the next card must get the slot once the merge has been held for the bound"
    # ... and it keeps retrying the merge while released.
    assert github.merge_calls > calls_when_released


def _next_dispatched(daemon: CoordinareDaemon) -> bool:
    return bool((daemon._state["active_sessions"].get(_NEXT) or {}).get("agent_dispatch"))
