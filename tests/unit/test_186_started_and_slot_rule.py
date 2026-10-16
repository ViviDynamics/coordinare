"""#186 C and D: what "started" means, and one predicate for a live performer.

C. "Already started" was decided by an open PR alone. A card past assessing or
architecting with no PR yet, that lost its reservation, counted as new work and
was held by the review cap. Started now also covers the stage records the
session keeps: the assessment, the blueprint and recorded stage verdicts.

D. Pickup counting (check_board) and the budget (pipeline_budget) stated one rule
in two functions, and disagreed about a live ``monitoring_agent`` session. Both
now test liveness with ``has_live_work``.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.pipeline_budget import (
    dispatch_has_pipeline_slot,
    has_live_performer,
    has_live_work,
    has_started,
    select_pipelines,
    session_consumes_slot,
)


def _card(cid: str, status: str = "TODO", **extra: Any) -> dict[str, Any]:
    return {"id": cid, "content_id": cid, "status": status, **extra}


def _unstarted(cid: str, **extra: Any) -> dict[str, Any]:
    return {"current_card": _card(cid), "phase": "dispatching", "performer_stage": "assessing",
            "pipeline_admitted": False, **extra}


def _parked(cid: str) -> dict[str, Any]:
    return {"current_card": _card(cid, "IN_REVIEW", pr_number=81), "phase": "monitoring_pr",
            "performer_stage": "closing_review", "review_wait": "human", "pipeline_admitted": True}


# --- C: started ----------------------------------------------------------------- #


@pytest.mark.parametrize("record", [
    {"assessment": {"complexity": "small"}},
    {"blueprint": {"briefs": {}}},
    {"stage_verdicts": {"reviewing": {"head_sha": "a" * 40, "verdict": "pass"}}},
])
def test_a_card_past_its_first_stage_is_started_without_a_pr(record: dict[str, Any]) -> None:
    assert has_started(_unstarted("back", **record))


def test_a_card_with_only_its_first_stage_pending_is_not_started() -> None:
    assert not has_started(_unstarted("fresh"))
    assert not has_started({"current_card": _card("fresh"), "phase": "idle", "assessment": None,
                            "blueprint": None, "stage_verdicts": {}})


def test_an_open_pr_is_started() -> None:
    assert has_started(_parked("parked"))


def test_a_started_card_without_a_pr_is_returning_not_new_work() -> None:
    """Blocked then unblocked, or readopted after a restart without PR fields."""
    sessions = {"fresh": _unstarted("fresh"), "back": _unstarted("back", assessment={"complexity": "small"})}
    assert select_pipelines(sessions, 1) == {"back"}


def test_a_started_card_without_a_pr_is_not_held_by_the_review_cap() -> None:
    """The cap is full with one parked card; the started card is admitted, the new one is not."""
    sessions = {"parked": _parked("parked"), "back": _unstarted("back", blueprint={"briefs": {}}),
                "fresh": _unstarted("fresh")}
    assert select_pipelines(sessions, 2, review_limit=1) == {"back"}


def test_a_started_card_claims_a_slot_when_the_review_cap_holds_new_work() -> None:
    sessions = {"parked": _parked("parked"), "back": _unstarted("back", assessment={"complexity": "small"}),
                "fresh": _unstarted("fresh")}
    state: dict[str, Any] = {"active_sessions": sessions, "config": _config(1, 1), "_pipeline_selected": set()}
    assert dispatch_has_pipeline_slot(state, "back")
    assert not dispatch_has_pipeline_slot(state, "fresh")


def _config(limit: int, review_limit: int | None) -> Any:
    return SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=review_limit)


# --- D: one live-work predicate ------------------------------------------------- #


def _live_agent() -> dict[str, Any]:
    return {"current_card": _card("agent", "IN_PROGRESS"), "phase": "monitoring_agent",
            "performer_stage": "implementing", "agent_dispatch": {"session_id": "job-agent"}}


def _live_performer() -> dict[str, Any]:
    return {"current_card": _card("performer", "IN_PROGRESS"), "phase": "monitoring_performer",
            "performer_stage": "implementing", "agent_dispatch": {"session_id": "job-performer"}}


def _live_side_writer() -> dict[str, Any]:
    return {**_parked("writer"), "documenting_side": {"session_id": "side", "status": "running"}}


@pytest.mark.parametrize("session", [_live_agent(), _live_performer(), _live_side_writer()])
def test_pickup_and_the_budget_agree_on_a_live_session(session: dict[str, Any]) -> None:
    assert has_live_work(session)
    assert session_consumes_slot(session)


def test_a_monitoring_agent_with_a_live_session_is_running_work_in_the_budget() -> None:
    assert has_live_performer(_live_agent())
    sessions = {"todo": _unstarted("todo"), "agent": _live_agent()}
    assert select_pipelines(sessions, 1) == {"agent"}


def test_a_monitoring_agent_without_a_session_is_not_live() -> None:
    session = {**_live_agent(), "agent_dispatch": {}}
    assert not has_live_work(session)
    assert not has_live_performer(session)


def test_a_parked_card_is_live_for_neither_test() -> None:
    session = _parked("parked")
    assert not has_live_work(session)
    assert not session_consumes_slot(session, "IN_REVIEW")
