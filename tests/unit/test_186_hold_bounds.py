"""#186 review follow-ups: a card may hold its slot in monitoring_pr only for a bounded time.

Each test drives the REAL ``monitor_pr`` node against a stub GitHub service, and the
REAL daemon fanout where the slot decision is made. The stub stands in for GitHub
only; the base-branch gate decision is stubbed in one test and noted there. The clock
is a fake so the bound can be crossed without sleeping.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from coordinare import daemon as daemon_module
from coordinare.daemon import CoordinareDaemon, _compute_session_eligibilities
from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services import pipeline_budget
from coordinare.services.pipeline_budget import (
    dispatch_has_pipeline_slot,
    expire_review_holds,
    has_started,
    is_parked_in_review,
    select_pipelines,
)
from coordinare.session import session_to_state, state_to_session

_HEAD = "a" * 40
_BOUND = 900


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(pipeline_budget, "utc_now", fake)
    return fake


@pytest.fixture(autouse=True)
def _forget_logged_waits() -> None:
    daemon_module._review_wait_logged.clear()
    monitor_pr_module._merge_waits.clear()


class _Github:
    """Stub GitHub service. ``pulls`` is keyed by PR node id, e.g. ``PR_81``."""

    def __init__(self, pulls: dict[str, dict[str, Any]]) -> None:
        self.pulls = pulls

    async def get_pr_review_context(self, pr_node_id: str) -> dict[str, Any]:
        pull = self.pulls[pr_node_id]
        if pull.get("error") is not None:
            raise pull["error"]
        return {"reviews": pull.get("reviews", []), "review_threads": [], "head_oid": _HEAD,
                "review_decision": pull.get("decision", "REVIEW_REQUIRED"), "state": "OPEN"}

    async def get_pr_conversation_comments(self, number: int, since: object = None) -> list[dict[str, Any]]:
        pull = self.pulls.get(f"PR_{number}", {})
        if pull.get("conversation_error") is not None:
            raise pull["conversation_error"]
        return []


def _approval(login: str = "jason") -> dict[str, Any]:
    return {"id": f"RVW_{login}", "author_login": login, "state": "APPROVED", "commit_oid": _HEAD,
            "submitted_at": "2026-10-12T23:22:41Z", "comments": []}


def _changes_requested() -> dict[str, Any]:
    return {"id": "RVW_CR", "author_login": "jason", "state": "CHANGES_REQUESTED", "commit_oid": _HEAD,
            "submitted_at": "2026-10-12T23:25:00Z", "comments": []}


def _card(cid: str, pr: int, status: str = "IN_REVIEW") -> dict[str, Any]:
    return {"id": cid, "content_id": cid, "status": status, "title": cid,
            "pr_node_id": f"PR_{pr}", "pr_number": pr}


def _in_review(cid: str, pr: int, **extra: Any) -> dict[str, Any]:
    return {"current_card": _card(cid, pr), "phase": "monitoring_pr", "performer_stage": "closing_review",
            "pipeline_admitted": False, **extra}


def _todo(cid: str) -> dict[str, Any]:
    return {"current_card": _card(cid, 0, "TODO") | {"pr_node_id": "", "pr_number": 0},
            "phase": "dispatching", "performer_stage": "assessing", "pipeline_admitted": False}


def _monitor_state(card: dict[str, Any], github: _Github) -> dict[str, Any]:
    state = initial_state()
    state["current_card"] = card
    state["active_card_id"] = card["id"]
    state["github_service"] = github
    state["human_reviewers"] = ["jason"]
    state["phase"] = "monitoring_pr"
    return state


# --- 1. a non-transient monitor_pr error parks the card at once -------------- #


@pytest.mark.asyncio
async def test_a_non_transient_error_is_a_wait_on_a_human_from_the_first_cycle() -> None:
    github = _Github({"PR_81": {"error": RuntimeError("Could not resolve to a node")}})
    state = _monitor_state(_card("stuck", 81), github)
    for _ in range(3):
        state = await monitor_pr(state)
        assert state["phase"] == "monitoring_pr"
    assert state["review_wait"] == "human"
    sessions = {"stuck": state_to_session(state), "next": _todo("next")}
    assert select_pipelines(sessions, 1) == {"next"}


# --- 2. an approved PR whose conversation poll cannot complete ----------------- #


@pytest.mark.asyncio
async def test_a_permanently_unreadable_conversation_is_a_wait_on_a_human() -> None:
    """pr_number 0 can never be read: the poll is incomplete for ever, so it is a human wait now."""
    github = _Github({"PR_0": {"reviews": [_approval()], "decision": "APPROVED"}})
    state = _monitor_state(_card("no-number", 0), github)
    state = await monitor_pr(state)
    assert state["review_wait"] == "human"
    assert is_parked_in_review(state_to_session(state) | {"current_card": _card("no-number", 0)})


@pytest.mark.asyncio
async def test_a_failing_conversation_poll_holds_only_until_the_bound(clock: _Clock) -> None:
    github = _Github({"PR_81": {"reviews": [_approval()], "decision": "APPROVED",
                                "conversation_error": TimeoutError("comments timed out")}})
    state = _monitor_state(_card("flaky", 81), github)
    state = await monitor_pr(state)
    assert state["review_wait"] == "automation"
    session = state_to_session(state)
    assert not is_parked_in_review(session | {"current_card": _card("flaky", 81)})

    clock.advance(_BOUND + 1)
    sessions = {"flaky": session}
    expire_review_holds(sessions, hold_seconds=_BOUND)
    assert is_parked_in_review(sessions["flaky"] | {"current_card": _card("flaky", 81)})


# --- 3. a red base branch is a wait on a human, not an automated hold --------- #


@pytest.mark.asyncio
async def test_a_red_base_branch_is_a_wait_on_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base-branch gate decision is stubbed (its GraphQL rollup is out of scope); monitor_pr's mapping is real."""
    from coordinare.graph.nodes import monitor_performer

    async def red_base(state: Any, card_id: str, pr_url: str) -> tuple[dict[str, Any], bool]:
        return {"phase": "monitoring_pr"}, True

    monkeypatch.setattr(monitor_performer, "_evaluate_baseline_prevention_gate", red_base)
    github = _Github({"PR_81": {"reviews": [_approval()], "decision": "APPROVED"}})
    state = await monitor_pr(_monitor_state(_card("base-red", 81), github))
    assert state["review_wait"] == "human"


# --- 4. a declined assessment is not started work ------------------------------ #


def test_a_declined_assessment_is_not_started_work() -> None:
    declined = {"current_card": _card("declined", 0, "TODO") | {"pr_node_id": "", "pr_number": 0},
                "phase": "dispatching", "assessment": {"goal": "x", "declined": "not_work"}}
    assert not has_started(declined)


def test_an_accepted_assessment_is_started_work() -> None:
    accepted = {"current_card": {"id": "ok", "status": "TODO"}, "phase": "dispatching",
                "assessment": {"goal": "x", "recorded_at": "2026-10-13T12:00:00+00:00"}}
    assert has_started(accepted)


def test_a_verdict_for_the_assessing_stage_alone_is_not_started_work() -> None:
    assessing = {"current_card": {"id": "a", "status": "TODO"}, "phase": "dispatching",
                 "stage_verdicts": {"assessing": {"head_sha": _HEAD, "verdict": "pass"}}}
    past = {"current_card": {"id": "b", "status": "TODO"}, "phase": "dispatching",
            "stage_verdicts": {"reviewing": {"head_sha": _HEAD, "verdict": "pass"}}}
    assert not has_started(assessing)
    assert has_started(past)


def test_a_declined_card_that_returns_to_todo_does_not_jump_the_queue_or_the_cap() -> None:
    declined = {"current_card": {"id": "declined", "status": "TODO"}, "phase": "dispatching",
                "assessment": {"goal": "x", "declined": "needs_split"}, "pipeline_admitted": False}
    fresh = {"current_card": {"id": "fresh", "status": "TODO"}, "phase": "dispatching", "pipeline_admitted": False}
    parked = _in_review("parked", 81, review_wait="human", pipeline_admitted=True)
    sessions = {"parked": parked, "declined": declined, "fresh": fresh}
    # The review cap is full, and the declined card is not started work, so nothing is admitted.
    assert select_pipelines(sessions, 2, review_limit=1) == set()
    # A declined card is not returning work, so it does not go ahead of a card that has not started.
    assert select_pipelines({"fresh": fresh, "declined": declined}, 1) == {"fresh"}


def test_the_decline_path_records_that_the_assessment_was_declined() -> None:
    from coordinare.graph.nodes.monitor.ui import _apply_assessor_decline

    async def run() -> dict[str, Any]:
        state = initial_state()
        card = {"id": "declined", "title": "t"}
        result = await _apply_assessor_decline(
            state, card, "declined", "assessment_not_work",
            {"report": {"assessment": {"goal": "not a task"}}}, None, None,
        )
        return dict(result["assessment"])

    recorded = asyncio.run(run())
    assert recorded["declined"] == "not_work"
    assert not has_started({"current_card": {"id": "declined", "status": "TODO"}, "assessment": recorded})


# --- 5. a parked card in the same pass releases its place at once -------------- #


class _Graph:
    """Runs the real monitor_pr for a card in monitoring_pr; a card that needs a performer tries for a slot."""

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def ainvoke(self, state: Any) -> Any:
        card_id = str(state.get("active_card_id"))
        self.invoked.append(card_id)
        if state.get("phase") == "monitoring_pr":
            state = await monitor_pr(state)
        if state.get("phase") in {"relay_feedback", "dispatching"}:
            if dispatch_has_pipeline_slot(state, card_id):
                state["phase"] = "monitoring_performer"
                state["agent_dispatch"] = {"session_id": f"job-{card_id}"}
            else:
                state["phase"] = "dispatching"
        return state


def _daemon(sessions: dict[str, Any], github: _Github, graph: _Graph, *, limit: int = 1) -> CoordinareDaemon:
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=lambda _s: None)
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=None,
                                              review_slot_hold_seconds=_BOUND)
    daemon._state["active_sessions"] = sessions
    daemon._state["github_service"] = github
    daemon._state["human_reviewers"] = ["jason"]
    daemon._state["board_snapshot"] = {"TODO": ["next"], "IN_REVIEW": ["parker", "returner"]}
    return daemon


@pytest.mark.asyncio
async def test_a_started_card_that_parks_in_its_own_tick_frees_its_place_for_a_returning_card() -> None:
    """Real fanout, real monitor_pr: 'parker' is admitted unevaluated, finds no approval, and parks.

    'returner' has review feedback to act on. Before the fix, parker's admission held the
    only slot for the rest of the pass and returner was refused.
    """
    github = _Github({
        "PR_81": {"reviews": [], "decision": "REVIEW_REQUIRED"},
        "PR_82": {"reviews": [_changes_requested()], "decision": "CHANGES_REQUESTED"},
    })
    sessions = {
        "parker": _in_review("parker", 81, pipeline_admitted=True),
        "returner": _in_review("returner", 82, performer_stage="implementing"),
    }
    graph = _Graph()
    daemon = _daemon(sessions, github, graph)

    await daemon._invoke_multi_session()

    live = daemon._state["active_sessions"]
    assert live["returner"]["agent_dispatch"] == {"session_id": "job-returner"}
    assert live["parker"]["review_wait"] == "human"
    assert "parker" not in (daemon._state["_pipeline_selected"] or set())


# --- 6. the bound: a hold that outlives it is treated as parked, once, and recovers ---- #


@pytest.mark.asyncio
async def test_an_expired_hold_releases_the_slot_logs_once_and_regains_it_when_it_needs_a_performer(
    clock: _Clock,
) -> None:
    github = _Github({"PR_81": {"reviews": [_approval()], "decision": "APPROVED",
                                "conversation_error": TimeoutError("comments timed out")}})
    sessions = {"flaky": _in_review("flaky", 81, pipeline_admitted=True), "next": _todo("next")}
    graph = _Graph()
    daemon = _daemon(sessions, github, graph)

    await daemon._invoke_multi_session()
    assert daemon._state["active_sessions"]["flaky"]["review_wait"] == "automation"
    assert not daemon._state["active_sessions"]["next"].get("agent_dispatch")

    clock.advance(_BOUND + 1)
    with structlog.testing.capture_logs() as logs:
        await daemon._invoke_multi_session()
        await daemon._invoke_multi_session()
    live = daemon._state["active_sessions"]
    assert live["flaky"]["review_wait"] == "human"
    assert live["next"]["agent_dispatch"] == {"session_id": "job-next"}
    assert "flaky" in graph.invoked  # still ticked every cycle
    expiries = [e for e in logs if e["event"] == "monitor_pr.slot_hold_expired"]
    assert [(e["card_id"], e["reason"]) for e in expiries] == [("flaky", "pr_comments_not_fully_read")]
    assert expiries[0]["log_level"] == "warning"

    # The PR now needs a performer: the card takes the slot through the returning path.
    github.pulls["PR_81"] = {"reviews": [_changes_requested()], "decision": "CHANGES_REQUESTED"}
    del daemon._state["active_sessions"]["next"]  # the job that took the slot has finished and retired
    await daemon._invoke_multi_session()
    assert daemon._state["active_sessions"]["flaky"]["agent_dispatch"] == {"session_id": "job-flaky"}


def test_an_unevaluated_card_that_never_gets_evaluated_releases_after_the_bound(clock: _Clock) -> None:
    sessions = {"absent": {"current_card": _card("absent", 81), "phase": "monitoring_pr",
                           "performer_stage": "closing_review", "pipeline_admitted": False},
                "next": _todo("next")}
    state = initial_state()
    state["config"] = SimpleNamespace(max_concurrent_cards=1, max_cards_in_review=None,
                                      review_slot_hold_seconds=_BOUND)
    _compute_session_eligibilities(state, sessions, 1)
    assert state["_pipeline_selected"] == {"absent"}

    clock.advance(_BOUND + 1)
    _compute_session_eligibilities(state, sessions, 1)
    assert state["_pipeline_selected"] == {"next"}


# --- state: the hold fields mirror the session and clear when the card leaves review -- #


def test_the_hold_fields_round_trip_and_clear_when_the_card_leaves_monitoring_pr() -> None:
    since = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    state = initial_state()
    state.update(phase="monitoring_pr", current_card={"id": "x"}, review_wait="automation",
                 review_wait_since=since, review_wait_head=_HEAD, review_wait_reason="checks",
                 review_wait_expired=True)
    session = state_to_session(state)
    assert session["review_wait_since"] == since and session["review_wait_expired"] is True
    restored = initial_state()
    session_to_state(session, restored)
    assert restored["review_wait_reason"] == "checks" and restored["review_wait_head"] == _HEAD

    state["phase"] = "blocked"
    gone = state_to_session(state)
    assert not {"review_wait", "review_wait_since", "review_wait_head", "review_wait_reason",
                "review_wait_expired"} & set(gone)


def test_the_hold_bound_is_configurable_and_defaults_to_fifteen_minutes() -> None:
    from coordinare.config import ProjectConfiguration

    field = ProjectConfiguration.model_fields["review_slot_hold_seconds"]
    assert field.default == 900
    assert pipeline_budget.configured_hold_seconds(SimpleNamespace(review_slot_hold_seconds=120)) == 120
    assert pipeline_budget.configured_hold_seconds(object()) == 900
