"""#186 second review: the hold bound must not be defeated, and live work is never released.

Each scenario drives the REAL daemon fanout (``_invoke_multi_session``), the real
``monitor_pr`` and ``merge_pr`` nodes, and a stub GitHub service. The only stubs are
GitHub, the checks gate decision (one scenario), and the monotonic hold clock.
"""
from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare import daemon as daemon_module
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes import merge_pr as merge_module
from coordinare.graph.nodes import monitor_performer as performer_module
from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import _record_review_wait
from coordinare.services import pipeline_budget
from coordinare.services.pipeline_budget import dispatch_has_pipeline_slot, has_live_work

_HEAD = "a" * 40
_BOUND = 900
_CYCLE = 60
_BY_CYCLE = 20  # the bound is 15 cycles at _CYCLE; a release later than this is a wedge


class _Clock:
    """The monotonic hold clock, advanced by the test rather than by sleeping."""

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


def _card(cid: str, pr: int, status: str = "IN_REVIEW") -> dict[str, Any]:
    return {"id": cid, "content_id": cid, "status": status, "title": cid,
            "pr_node_id": f"PR_{pr}", "pr_number": pr,
            "pr_url": f"https://github.com/o/r/pull/{pr}" if pr else ""}


def _in_review(cid: str, pr: int, **extra: Any) -> dict[str, Any]:
    return {"current_card": _card(cid, pr), "phase": "monitoring_pr", "performer_stage": "closing_review",
            "pipeline_admitted": False, **extra}


def _todo(cid: str) -> dict[str, Any]:
    return {"current_card": _card(cid, 0, "TODO"), "phase": "dispatching",
            "performer_stage": "assessing", "pipeline_admitted": False}


def _approval() -> dict[str, Any]:
    return {"id": "RVW_jason", "author_login": "jason", "state": "APPROVED", "commit_oid": _HEAD,
            "submitted_at": "2026-10-12T23:22:41Z", "comments": []}


def _changes_requested(pr_head: str = _HEAD) -> dict[str, Any]:
    return {"id": "RVW_CR", "author_login": "jason", "state": "CHANGES_REQUESTED", "commit_oid": pr_head,
            "submitted_at": "2026-10-12T23:25:00Z", "comments": []}


class _Github:
    """Stub GitHub service. Each knob is read on every call, so a scenario can change it per cycle."""

    def __init__(self) -> None:
        self.head = _HEAD
        self.approved = True
        self.conv_error: BaseException | None = None
        self.merge_error: BaseException | None = None
        self.changes_requested_on: str | None = None
        self.merge_calls = 0

    async def get_pr_review_context(self, pr_node_id: str) -> dict[str, Any]:
        reviews: list[dict[str, Any]] = [_approval()] if self.approved else []
        decision = "APPROVED" if self.approved else "REVIEW_REQUIRED"
        if self.changes_requested_on == pr_node_id:
            reviews = [_changes_requested(self.head)]
            decision = "CHANGES_REQUESTED"
        return {"reviews": reviews, "review_threads": [], "head_oid": self.head,
                "review_decision": decision, "state": "OPEN"}

    async def get_pr_conversation_comments(self, number: int, since: object = None) -> list[dict[str, Any]]:
        if self.conv_error is not None:
            raise self.conv_error
        return []

    async def check_mergeability(self, pr_node_id: str) -> dict[str, Any]:
        self.merge_calls += 1
        if self.merge_error is not None:
            raise self.merge_error
        return {"mergeable": True, "mergeable_raw": "MERGEABLE", "review_decision": "APPROVED",
                "head_ref_oid": self.head}

    async def squash_merge(self, pr_node_id: str) -> dict[str, Any]:
        return {}


class _Graph:
    """The real node for each phase; a card that needs a performer tries for a slot."""

    async def ainvoke(self, state: Any) -> Any:
        card_id = str(state.get("active_card_id"))
        if state.get("phase") == "monitoring_pr":
            state = await monitor_pr_module.monitor_pr(state)
        if state.get("phase") == "merging":
            state = await merge_module.merge_pr(state)
        if state.get("phase") in {"relay_feedback", "dispatching"}:
            if dispatch_has_pipeline_slot(state, card_id):
                state["phase"] = "monitoring_performer"
                state["agent_dispatch"] = {"session_id": f"job-{card_id}"}
            else:
                state["phase"] = "dispatching"
        return state


def _daemon(sessions: dict[str, Any], github: _Github, *, limit: int = 1,
            review_limit: int | None = None) -> CoordinareDaemon:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1, sleep_func=lambda _s: None)
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=review_limit,
                                              review_slot_hold_seconds=_BOUND)
    daemon._state["active_sessions"] = sessions
    daemon._state["github_service"] = github
    daemon._state["human_reviewers"] = ["jason"]
    daemon._state["board_snapshot"] = {"TODO": ["next"], "IN_REVIEW": ["X"]}
    return daemon


def _cycle(daemon: daemon_module.CoordinareDaemon, clock: _Clock) -> None:
    clock.advance(_CYCLE)
    asyncio.run(daemon._invoke_multi_session())


def _live(daemon: CoordinareDaemon) -> int:
    return sum(1 for s in daemon._state["active_sessions"].values() if has_live_work(s))


def _next_dispatched(daemon: CoordinareDaemon) -> bool:
    return bool((daemon._state["active_sessions"].get("next") or {}).get("agent_dispatch"))


# --- 1. a live side writer keeps its slot through a human wait ---------------- #


def test_a_live_side_writer_keeps_its_slot_through_a_human_wait(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """X is in review with a running documenter side writer and no approval, so monitor_pr says
    human. Z needs feedback. X's writer is live work, so Z must not be admitted beside it."""
    async def gate(_state: Any, _card_id: str, _pr_url: str) -> tuple[dict[str, Any], bool]:
        return {}, False

    monkeypatch.setattr(performer_module, "_evaluate_pr_checks_gate", gate)
    github = _Github()
    github.approved = False
    github.changes_requested_on = "PR_82"
    sessions = {
        "X": _in_review("X", 81, documenting_side={"session_id": "doc-1", "status": "running"}),
        "Z": _in_review("Z", 82, performer_stage="implementing"),
    }
    daemon = _daemon(sessions, github, limit=1)
    _cycle(daemon, clock)
    assert _live(daemon) <= 1
    assert not daemon._state["active_sessions"]["Z"].get("agent_dispatch")


# --- 2. the fuzz: running work never exceeds the limit ----------------------- #


class _FuzzGraph:
    """Random outcomes per tick. ``side_cases`` collects the cases that matter for the guard:
    a human wait recorded on a card in monitoring_pr with a live side writer, while another
    card is waiting for a slot. Without that, the fuzz never reaches the guard."""

    def __init__(self, rng: random.Random, side_cases: list[str]) -> None:
        self.rng = rng
        self.side_cases = side_cases

    async def ainvoke(self, state: Any) -> Any:
        cid = str(state.get("active_card_id"))
        roll = self.rng.random()
        if state.get("phase") == "monitoring_pr":
            if roll < 0.3:
                if has_live_work(state) and any(
                    s.get("phase") == "dispatching" and not has_live_work(s)
                    for s in (state.get("active_sessions") or {}).values()
                ):
                    self.side_cases.append(cid)
                _record_review_wait(state, cid, "human", reason="human", head=_HEAD)
            elif roll < 0.6:
                _record_review_wait(state, cid, "automation", reason=self.rng.choice(["a", "b"]),
                                    head=self.rng.choice([_HEAD, "b" * 40]))
            elif roll < 0.75:
                state["phase"] = "relay_feedback"
                _record_review_wait(state, cid, None, reason="actionable_feedback", head=_HEAD)
        if state.get("phase") in {"relay_feedback", "dispatching"}:
            if dispatch_has_pipeline_slot(state, cid):
                state["phase"] = "monitoring_performer"
                state["agent_dispatch"] = {"session_id": f"j-{cid}"}
            else:
                state["phase"] = "dispatching"
        if state.get("phase") == "monitoring_performer" and roll > 0.7:
            state["phase"] = "monitoring_pr"
            state["agent_dispatch"] = None
        return state


_FUZZ_SEEDS = 200
_FUZZ_MIN_SIDE_CASES = 100  # 154 observed with the fix; a generator that drops below this has stopped covering the guard


def _run_fuzz(clock: _Clock) -> tuple[list[tuple[int, int, int, int]], int, list[str]]:
    violations: list[tuple[int, int, int, int]] = []
    side_cases: list[str] = []
    runs = 0
    for seed in range(_FUZZ_SEEDS):
        rng = random.Random(seed)
        limit = rng.randint(1, 3)
        sessions: dict[str, Any] = {}
        for i in range(rng.randint(2, 7)):
            cid = f"c{i}"
            kind = rng.choice(["mon", "side", "side", "todo", "started_todo"])
            if kind == "mon":
                extra = {"review_wait": rng.choice(["human", "automation", None])} if rng.random() < 0.8 else {}
                sessions[cid] = _in_review(cid, 100 + i, pipeline_admitted=rng.random() < 0.5, **extra)
            elif kind == "side":
                sessions[cid] = _in_review(cid, 100 + i, pipeline_admitted=True,
                                           documenting_side={"session_id": "d", "status": "running"})
            elif kind == "todo":
                sessions[cid] = _todo(cid)
            else:
                sessions[cid] = _todo(cid) | {"assessment": {"goal": "x"}, "pipeline_admitted": rng.random() < 0.5}
        if sum(has_live_work(s) for s in sessions.values()) > limit:
            continue
        runs += 1
        daemon = CoordinareDaemon(_FuzzGraph(rng, side_cases), max_cycles=1, sleep_func=lambda _s: None)
        daemon._state["config"] = SimpleNamespace(max_concurrent_cards=limit,
                                                  max_cards_in_review=rng.choice([None, 1, 2]),
                                                  review_slot_hold_seconds=_BOUND)
        daemon._state["active_sessions"] = sessions
        daemon._state["github_service"] = _Github()
        # The board must agree with each card's column, or the fallback check_board
        # moves the card to idle and the case under test never happens.
        daemon._state["board_snapshot"] = {
            "TODO": [c for c, s in sessions.items() if s["current_card"]["status"] == "TODO"],
            "IN_REVIEW": [c for c, s in sessions.items() if s["current_card"]["status"] == "IN_REVIEW"],
        }
        for cycle in range(6):
            clock.advance(_CYCLE)
            asyncio.run(daemon._invoke_multi_session())
            live = _live(daemon)
            if live > limit:
                violations.append((seed, cycle, limit, live))
                break
    assert runs > 100
    return violations, runs, side_cases


def test_running_work_never_exceeds_the_limit_under_random_outcomes(clock: _Clock) -> None:
    """Seeded fuzz over the real fanout and selection, with enough live-side-writer human waits to matter."""
    violations, _runs, side_cases = _run_fuzz(clock)
    assert len(side_cases) >= _FUZZ_MIN_SIDE_CASES, (
        f"the generator produced {len(side_cases)} human waits on a live side writer with a card waiting; "
        f"the guard is not being exercised"
    )
    assert violations == []


# --- 3. a merge that keeps failing must not hold the slot for ever ---------------- #


def test_a_merge_that_keeps_failing_releases_its_slot_and_is_still_retried(clock: _Clock) -> None:
    github = _Github()
    github.merge_error = RuntimeError("502 Bad Gateway from upstream")
    sessions = {"X": _in_review("X", 81, phase="merging"), "next": _todo("next")}
    daemon = _daemon(sessions, github, limit=1)
    calls_at_release: list[int] = []
    for _ in range(_BY_CYCLE):
        _cycle(daemon, clock)
        if daemon._state["active_sessions"]["X"].get("review_wait") == "human" and not calls_at_release:
            calls_at_release.append(github.merge_calls)
    assert _next_dispatched(daemon), "the next card must get the slot once the merge has been held for the bound"
    assert calls_at_release, "the stuck merge must be released, not left holding the slot"
    # Still ticked every cycle after release, so a merge that starts working is not stranded.
    assert github.merge_calls > calls_at_release[0]


# --- 4. a hold the head keeps moving under still expires ---------------------- #


def test_a_checks_hold_whose_head_keeps_moving_still_expires(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    async def hold(_state: Any, _card_id: str, _pr_url: str) -> tuple[dict[str, Any], bool]:
        return {}, True

    monkeypatch.setattr(performer_module, "_evaluate_pr_checks_gate", hold)
    github = _Github()
    daemon = _daemon({"X": _in_review("X", 81), "next": _todo("next")}, github, limit=1)
    released_at: int | None = None
    for cycle in range(_BY_CYCLE * 2):
        if cycle % 5 == 0:  # a new head every five minutes, checks still pending
            github.head = f"{cycle:040x}"[-40:]
        _cycle(daemon, clock)
        if daemon._state["active_sessions"]["X"].get("review_wait") == "human" and released_at is None:
            released_at = cycle
    assert released_at is not None and released_at < _BY_CYCLE
    assert _next_dispatched(daemon)


# --- 5. alternating reasons do not restart the bound -------------------------- #


def test_alternating_reasons_do_not_restart_the_bound(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    flip = {"hold": True}

    async def gate(_state: Any, _card_id: str, _pr_url: str) -> tuple[dict[str, Any], bool]:
        return ({}, True) if flip["hold"] else ({}, False)

    monkeypatch.setattr(performer_module, "_evaluate_pr_checks_gate", gate)
    github = _Github()
    daemon = _daemon({"X": _in_review("X", 81), "next": _todo("next")}, github, limit=1)
    released_at: int | None = None
    for cycle in range(_BY_CYCLE * 2):
        flip["hold"] = cycle % 2 == 0
        github.conv_error = None if flip["hold"] else TimeoutError("comments timed out")
        _cycle(daemon, clock)
        if daemon._state["active_sessions"]["X"].get("review_wait") == "human" and released_at is None:
            released_at = cycle
    assert released_at is not None and released_at < _BY_CYCLE
    assert _next_dispatched(daemon)


# --- 6. each new head does not re-arm the bound ------------------------------- #


def test_each_new_head_does_not_rearm_the_bound(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    async def hold(_state: Any, _card_id: str, _pr_url: str) -> tuple[dict[str, Any], bool]:
        return {}, True

    monkeypatch.setattr(performer_module, "_evaluate_pr_checks_gate", hold)
    github = _Github()
    daemon = _daemon({"X": _in_review("X", 81), "next": _todo("next")}, github, limit=1)
    released_at: int | None = None
    for cycle in range(_BY_CYCLE * 2):
        if cycle and cycle % 20 == 0:  # a new head every twenty minutes, each one a fresh reason to wait
            github.head = f"{cycle:040x}"[-40:]
        _cycle(daemon, clock)
        if daemon._state["active_sessions"]["X"].get("review_wait") == "human" and released_at is None:
            released_at = cycle
    assert released_at is not None and released_at < _BY_CYCLE
    assert _next_dispatched(daemon)


# --- the clock is monotonic and recorded as a float ----------------------------- #


def test_the_hold_is_timed_on_the_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wall-clock jump must not shorten or stretch a hold, so the hold uses time.monotonic."""
    import time

    monkeypatch.setattr(time, "time", lambda: 0.0)  # a wall clock stuck in the past changes nothing
    first = pipeline_budget.hold_clock()
    assert isinstance(first, float)
    assert abs(first - time.monotonic()) < 1.0
