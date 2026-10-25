"""171: a documenter side run is a claim on a pipeline slot.

The budget (max_concurrent_cards) counts cards. A side run on a card that already
holds a slot shares that slot. A side run on a parked card (waiting on a human
review, holding nothing) takes a free slot, and waits while none is free. It
never starts beside its own card's live lifecycle performer, it ranks above
cards that have not started, and it can never be starved by them.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.services import documenting_side as ds
from coordinare.services.pipeline_budget import (
    admitted_side_runs,
    dispatch_has_pipeline_slot,
    select_pipelines,
)

_BP: dict[str, Any] = {
    "summary": "s",
    "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}],
    "modules": [{"path": "app/", "note": "n"}],
    "data_model": {"changes": []}, "interfaces": [], "risks": [],
    "criteria": [{"surface": "/", "action": "a", "expected": "e", "kind": "functional"}],
    "docs": [{"topic": "t", "location": "docs/wiki/x.md", "say": "s"}],
    "size": "small", "blueprint_hash": "h1", "created_at": "t",
}


def _card(cid: str, status: str) -> dict[str, Any]:
    return {"id": cid, "status": status, "title": "T", "body": "B", "issue_number": 5, "base_branch": "main"}


def _session(cid: str, status: str, phase: str, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "card_id": cid,
        "performer_stage": "implementing",
        "current_symphony": "sampleweb",
        "workspace_branch": f"coordinare/{cid}/x",
        "current_card": _card(cid, status),
        "blueprint": _BP,
        "documenting_side": None,
        "phase": phase,
    }
    base.update(extra)
    return base


def _parked(cid: str) -> dict[str, Any]:
    """Waiting on a human review: holds no slot, is still ticked for merge."""
    return _session(cid, "IN_REVIEW", "monitoring_pr", review_wait="human",
                    pr_number=7, pr_url="https://github.com/o/r/pull/7", pr_node_id="PR_7")


def _busy(cid: str) -> dict[str, Any]:
    """A lifecycle performer is running for this card right now."""
    return _session(cid, "IN_PROGRESS", "monitoring_performer", agent_dispatch={"session_id": f"perf-{cid}"})


def _holding(cid: str) -> dict[str, Any]:
    """Waiting on CI checks: keeps its reservation, runs no performer."""
    return _session(cid, "IN_PROGRESS", "monitoring_pr", review_wait="automation",
                    pr_number=8, pr_url="https://github.com/o/r/pull/8", pr_node_id="PR_8")


def _fresh(cid: str) -> dict[str, Any]:
    """Not started: no PR, no blueprint, nothing accepted."""
    return {"card_id": cid, "phase": "assessing", "current_card": _card(cid, "IN_PROGRESS"),
            "blueprint": None, "documenting_side": None, "performer_stage": ""}


def _writer(cid: str) -> dict[str, Any]:
    return _parked(cid) | {"documenting_side": {"status": "running", "session_id": f"side-{cid}",
                                                "blueprint_hash": "h1", "writer_active": False}}


class _Svc:
    """Documenter service double: records dispatches, reports every run as still going."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    async def dispatch_card(self, ctx: dict[str, Any], workspace_info: Any = None) -> dict[str, Any]:
        self.dispatched.append(ctx["card_id"])
        return {"session_id": f"side-{ctx['card_id']}"}

    async def check_status(self, session_id: str) -> dict[str, Any]:
        return {"status": "working"}


class _WM:
    async def get_fresh_github_token(self) -> str:
        return "ghs_fresh"


class _Role:
    backend = "codex"
    effort = None
    temperature = None
    max_tokens = None


class _Performers:
    def resolved_role(self, role: str) -> _Role:
        return _Role()


class _Cfg:
    personas = None
    performers = _Performers()
    max_cards_in_review = None

    def __init__(self, limit: int) -> None:
        self.max_concurrent_cards = limit

    def resolve_performer_dispatch_model(self, role: str) -> dict[str, str]:
        return {"model": "example/model"}


class _GitHub:
    org = "ExampleOrg"
    _project_name = "sampleweb"


@pytest.fixture(autouse=True)
def _persona(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinare.services.persona_service.get_effective_instructions", lambda role, personas: "p")


def _daemon(sessions: dict[str, Any], svc: _Svc, limit: int) -> CoordinareDaemon:
    d = object.__new__(CoordinareDaemon)
    d._state = {
        "active_sessions": sessions,
        "performer_services": {"documenting": svc},
        "symphony_workspace_managers": {"sampleweb": _WM()},
        "config": _Cfg(limit),
    }
    d._documenting_side_tasks = set()
    return d


async def _run(d: CoordinareDaemon, sessions: dict[str, Any], dispatchable: set[str]) -> None:
    await d._dispatch_documenting_side_runs(
        {"sampleweb": _GitHub()}, sessions=sessions, dispatchable_card_ids=dispatchable,
    )


async def _drain(d: CoordinareDaemon) -> None:
    """Stop the spawned completion polls, which never finish against the fake service."""
    tasks = list(d._documenting_side_tasks)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _live_performers(sessions: dict[str, Any]) -> int:
    """Containers the budget is paying for: a lifecycle performer or a live side writer each count one."""
    return sum(
        1 for s in sessions.values()
        if (s.get("agent_dispatch") or {}).get("session_id") or (s.get("documenting_side") or {}).get("status") == "running"
    )


@pytest.mark.asyncio
async def test_full_budget_holds_the_side_run_without_failing_it() -> None:
    sessions = {"A": _busy("A"), "B": _parked("B")}
    svc = _Svc()
    d = _daemon(sessions, svc, limit=1)
    await _run(d, sessions, {"A", "B"})
    await _drain(d)
    assert svc.dispatched == [], "the one slot is held by A's live performer"
    assert sessions["B"]["documenting_side"] is None, "a held run is not recorded as failed"
    assert _live_performers(sessions) <= 1


@pytest.mark.asyncio
async def test_side_run_starts_once_a_slot_frees() -> None:
    sessions = {"A": _busy("A"), "B": _parked("B")}
    svc = _Svc()
    d = _daemon(sessions, svc, limit=1)
    await _run(d, sessions, {"A", "B"})
    assert svc.dispatched == []
    sessions["A"]["phase"] = "done"
    sessions["A"]["agent_dispatch"] = {}
    await _run(d, sessions, {"A", "B"})
    await _drain(d)
    assert svc.dispatched == ["B"]
    assert sessions["B"]["documenting_side"]["status"] == "running"
    assert _live_performers(sessions) == 1


@pytest.mark.asyncio
async def test_side_run_shares_the_slot_of_a_card_that_already_holds_one() -> None:
    sessions = {"H": _holding("H"), "P": _parked("P")}
    svc = _Svc()
    d = _daemon(sessions, svc, limit=1)
    await _run(d, sessions, {"H", "P"})
    await _drain(d)
    assert svc.dispatched == ["H"], "H already holds the one slot, so its side run rides it"
    assert sessions["P"]["documenting_side"] is None, "P needs a slot of its own, and none is free"


@pytest.mark.asyncio
async def test_parked_card_takes_a_free_slot_for_its_side_run() -> None:
    sessions = {"P": _parked("P"), "F": _fresh("F")}
    svc = _Svc()
    d = _daemon(sessions, svc, limit=1)
    await _run(d, sessions, {"P"})
    await _drain(d)
    assert svc.dispatched == ["P"]
    assert _live_performers(sessions) == 1


@pytest.mark.asyncio
async def test_side_run_never_starts_beside_its_own_live_performer() -> None:
    sessions = {"A": _busy("A")}
    svc = _Svc()
    d = _daemon(sessions, svc, limit=2)
    await _run(d, sessions, {"A"})
    await _drain(d)
    assert svc.dispatched == [], "the card's own lifecycle performer runs first"
    assert sessions["A"]["documenting_side"] is None


@pytest.mark.asyncio
async def test_no_more_side_runs_than_free_slots_in_one_cycle() -> None:
    sessions = {"A": _busy("A"), "B": _parked("B"), "C": _parked("C")}
    svc = _Svc()
    d = _daemon(sessions, svc, limit=2)
    await _run(d, sessions, {"A", "B", "C"})
    await _drain(d)
    assert svc.dispatched == ["B"], "A holds one slot, B takes the other, C waits"
    assert _live_performers(sessions) == 2


def test_a_returning_card_keeps_its_slot_ahead_of_a_side_run() -> None:
    sessions = {"H": _holding("H"), "P": _parked("P")}
    assert select_pipelines(sessions, 1) == {"H"}
    assert admitted_side_runs(sessions, {"P"}, limit=1, eligible={"H", "P"}) == set()


def test_a_side_run_ranks_ahead_of_a_card_that_has_not_started() -> None:
    sessions = {"P": _parked("P"), "F": _fresh("F")}
    assert admitted_side_runs(sessions, {"P"}, limit=1, eligible={"P", "F"}) == {"P"}
    assert admitted_side_runs(sessions, set(), limit=1, eligible={"P", "F"}) == set()


def test_a_running_side_writer_holds_the_slot_and_a_fresh_card_waits_then_gets_it() -> None:
    sessions = {"P": _writer("P"), "F": _fresh("F")}
    state: dict[str, Any] = {"active_sessions": sessions, "config": _Cfg(1)}
    assert select_pipelines(sessions, 1) == {"P"}, "the side writer is running work, so F waits"
    # Its own lifecycle dispatch is not blocked by the side writer: it shares the slot.
    assert dispatch_has_pipeline_slot(state, "P") is True
    sessions["P"]["documenting_side"]["status"] = "done"
    assert select_pipelines(sessions, 1) == {"F"}, "the slot frees when the side run ends"


@pytest.mark.asyncio
async def test_run_cycle_with_no_admission_dispatches_nothing_and_records_nothing() -> None:
    sessions = {"P": _parked("P")}
    svc = _Svc()

    async def resolve(cid: str, session: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        return {"card_id": cid, "role": "documenting"}, None

    spawned: list[Any] = []

    def spawn(coro: Any) -> None:
        spawned.append(coro)
        coro.close()

    dispatched = await ds.run_cycle(sessions, svc=svc, resolve=resolve, spawn=spawn, admit=set())
    assert dispatched == 0 and svc.dispatched == []
    assert sessions["P"]["documenting_side"] is None
