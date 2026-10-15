"""#149: why each waiting card is waiting.

The conductor decides, in check_board, whether a To Do card with no session is
picked up. Each verdict written into the per-symphony state reports the
decision that holds the card: the pickup's own slot count, the dependency
filter, or the pickup's escalation and ownership tests (services/todo_waits.py).
The daemon carries the verdicts to the snapshot, and the review cap is named
only on cards that already have a session. The end-to-end checks against the
real check_board pickup are in tests/unit/test_149_review_fixes.py.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from coordinare import daemon as daemon_module
from coordinare.daemon import _GLOBAL_STATE_KEYS, _compute_session_eligibilities
from coordinare.dashboard import _DASHBOARD_JS_FILES, _DASHBOARD_JS_SOURCES, DashboardStore
from coordinare.graph.state import initial_state
from coordinare.models.dependency import CardDependency, DependencyGraph, DependencyStatus
from coordinare.services import todo_waits
from coordinare.services.card_ownership import OwnershipPolicy
from coordinare.services.todo_waits import (
    REASON_ASSIGNED_ELSEWHERE,
    REASON_ESCALATED,
    REASON_QUEUED,
    REASON_UNASSIGNED,
    REASON_WAITING_ON,
    open_slots,
    record_dependency_waits,
    record_pickup_outcome,
    refresh_todo_waits,
)
from coordinare.vocabulary import WAITS, payload
from tests.unit.dashboard_js_slices import label_helpers_js, shipped_function, time_helpers_js

_STRICT = OwnershipPolicy(login="coordinare-bot", include_unassigned=False)
_OPEN = OwnershipPolicy(login="coordinare-bot", include_unassigned=True)
_NO_FILTER = OwnershipPolicy()  # neither opt-in set: every card is eligible


def _state(sessions: dict[str, Any] | None = None, *, limit: int = 1) -> Any:
    state = initial_state()
    state["active_sessions"] = sessions or {}
    state["config"] = SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=None)
    return state


def _refresh(state, todo, *, blocked=(), active=(), assignees=None, labels=None,
             ownership=_OPEN, in_use=0, limit=1):
    refresh_todo_waits(
        state,
        board={"item_labels": labels or {}},
        todo=list(todo),
        blocked=list(blocked),
        active_card_ids=set(active),
        ownership=ownership,
        assignees=assignees or {},
        in_use=in_use,
        limit=limit,
    )
    return state["todo_waits"]


def _graph(**by_dependent: list[CardDependency]) -> DependencyGraph:
    return DependencyGraph(by_dependent=dict(by_dependent))


def _dep(card: str, number: int, status: DependencyStatus, repo: str = "") -> CardDependency:
    return CardDependency(dependent_item_id=card, blocker_issue_number=number, status=status, blocker_repo=repo)


def _entry(reason: str, blockers: list[str], verdict: str) -> dict[str, Any]:
    return {"reason": reason, "blockers": blockers, "verdict": verdict}


# --- the estimate, before the dependency filter has run ------------------- #


def test_open_slots_is_the_pickups_own_count() -> None:
    assert open_slots(0, 1) == 1
    assert open_slots(1, 1) == 0
    assert open_slots(3, 2) == 0


def test_a_full_board_estimates_a_queued_card() -> None:
    state = _state()
    waits = _refresh(state, ["PVTI_65", "PVTI_66"], in_use=1, limit=1)
    assert waits == {"PVTI_65": _entry(REASON_QUEUED, [], "estimate"),
                     "PVTI_66": _entry(REASON_QUEUED, [], "estimate")}
    assert state["todo_capacity"]["in_use"] == 1 and state["todo_capacity"]["hold"] == REASON_QUEUED


def test_a_free_slot_records_no_reason() -> None:
    state = _state()
    assert _refresh(state, ["PVTI_65"], in_use=0, limit=1) == {}
    assert state["todo_capacity"]["hold"] is None


def test_a_pending_blocker_is_named_even_with_a_free_slot() -> None:
    """Covers the early return in check_board that happens before the dependency filter."""
    state = _state()
    graph = _graph(PVTI_1=[_dep("PVTI_1", 42, DependencyStatus.PENDING)])
    state_graph = graph
    original = todo_waits._dependency_graph
    todo_waits._dependency_graph = lambda _state, _board: state_graph
    try:
        waits = _refresh(state, ["PVTI_1"], in_use=0, limit=1)
    finally:
        todo_waits._dependency_graph = original
    assert waits == {"PVTI_1": _entry(REASON_WAITING_ON, ["#42"], "estimate")}


def test_an_escalated_card_is_named_before_anything_else() -> None:
    state = _state()
    state["advocate_escalation_label"] = "needs-human"
    waits = _refresh(state, ["PVTI_1"], labels={"PVTI_1": ["needs-human"]}, in_use=1, limit=1)
    assert waits == {"PVTI_1": _entry(REASON_ESCALATED, [], "estimate")}


def test_an_answered_advocate_label_does_not_count_as_escalated() -> None:
    state = _state()
    state["advocate_escalation_label"] = "needs-human"
    assert _refresh(state, ["PVTI_1"], labels={"PVTI_1": ["answered"]}) == {}


def test_a_card_someone_else_holds_is_filtered_out_with_its_reason() -> None:
    waits = _refresh(_state(), ["PVTI_1", "PVTI_2"], assignees={"PVTI_1": ["alice"]}, ownership=_STRICT)
    assert waits == {"PVTI_1": _entry(REASON_ASSIGNED_ELSEWHERE, [], "estimate"),
                     "PVTI_2": _entry(REASON_UNASSIGNED, [], "estimate")}


def test_an_unassigned_card_is_admitted_when_the_filter_allows_it() -> None:
    assert _refresh(_state(), ["PVTI_2"]) == {}


def test_a_card_with_a_session_never_gets_a_todo_wait() -> None:
    assert _refresh(_state(), ["PVTI_1"], active=["PVTI_1"], in_use=1, limit=1) == {}


def test_no_filter_configured_admits_every_card_to_the_queue_check() -> None:
    waits = _refresh(_state(), ["PVTI_1"], ownership=_NO_FILTER, in_use=2, limit=2)
    assert waits == {"PVTI_1": _entry(REASON_QUEUED, [], "estimate")}


def test_a_card_that_leaves_the_todo_column_loses_its_estimate() -> None:
    state = _state()
    state["todo_waits"] = {"PVTI_1": _entry(REASON_QUEUED, [], "estimate")}
    assert _refresh(state, [], in_use=1, limit=1) == {}


def test_a_failed_dependency_read_leaves_the_dependency_reason_blank(monkeypatch) -> None:
    def broken(_state, _board):
        raise ValueError("board shape")

    monkeypatch.setattr(todo_waits, "_dependency_graph", broken)
    state = _state()
    state["advocate_escalation_label"] = "needs-human"
    waits = _refresh(state, ["PVTI_1", "PVTI_2"], labels={"PVTI_2": ["needs-human"]}, in_use=1, limit=1)
    assert waits == {"PVTI_2": _entry(REASON_ESCALATED, [], "estimate")}


# --- the dependency filter's verdict ------------------------------------- #


def test_a_card_held_back_by_a_pending_blocker_waits_on_it() -> None:
    state = _state()
    graph = _graph(PVTI_1=[_dep("PVTI_1", 42, DependencyStatus.PENDING)])
    record_dependency_waits(state, dep_graph=graph, filtered_ids=["PVTI_1"], passed_ids=[])
    assert state["todo_waits"] == {"PVTI_1": _entry(REASON_WAITING_ON, ["#42"], "dependency")}


def test_a_foreign_blocker_is_named_with_its_repository() -> None:
    state = _state()
    graph = _graph(PVTI_1=[_dep("PVTI_1", 7, DependencyStatus.PENDING, repo="org/other")])
    record_dependency_waits(state, dep_graph=graph, filtered_ids=["PVTI_1"], passed_ids=[])
    assert state["todo_waits"]["PVTI_1"]["blockers"] == ["org/other#7"]


def test_an_unresolvable_blocker_is_a_known_wait_after_the_filter_has_run() -> None:
    state = _state()
    graph = _graph(PVTI_1=[_dep("PVTI_1", 77, DependencyStatus.UNRESOLVABLE)])
    record_dependency_waits(state, dep_graph=graph, filtered_ids=["PVTI_1"], passed_ids=[])
    assert state["todo_waits"] == {"PVTI_1": _entry(REASON_WAITING_ON, ["#77"], "dependency")}


def test_a_card_the_filter_lets_through_has_no_dependency_wait() -> None:
    state = _state()
    state["todo_waits"] = {"PVTI_1": _entry(REASON_WAITING_ON, ["#42"], "estimate")}
    record_dependency_waits(state, dep_graph=_graph(), filtered_ids=[], passed_ids=["PVTI_1"])
    assert state["todo_waits"] == {}


def test_a_blocked_card_keeps_the_dependency_it_was_moved_for() -> None:
    state = _state()
    state["todo_waits"] = {
        "PVTI_1": _entry(REASON_WAITING_ON, ["#77"], "dependency"),
        "PVTI_2": _entry(REASON_WAITING_ON, ["#5"], "estimate"),
    }
    waits = _refresh(state, [], blocked=["PVTI_1", "PVTI_2"])
    assert waits == {"PVTI_1": _entry(REASON_WAITING_ON, ["#77"], "dependency")}


# --- the pickup's outcome ------------------------------------------------- #


def test_the_pickup_records_the_cards_it_held_back_as_queued() -> None:
    state = _state()
    record_pickup_outcome(state, held=["PVTI_2"], admitted=["PVTI_1"], in_use=1, limit=1)
    assert state["todo_waits"] == {"PVTI_2": _entry(REASON_QUEUED, [], "pickup")}
    assert state["todo_capacity"]["in_use"] == 1 and state["todo_capacity"]["hold"] == REASON_QUEUED


def test_an_admitted_card_has_no_todo_wait() -> None:
    state = _state()
    state["todo_waits"] = {"PVTI_1": _entry(REASON_QUEUED, [], "estimate")}
    record_pickup_outcome(state, held=[], admitted=["PVTI_1"], in_use=1, limit=2)
    assert state["todo_waits"] == {}
    assert state["todo_capacity"]["hold"] is None


# --- the daemon keeps them ------------------------------------------------ #


def test_the_fanout_merge_carries_the_todo_waits_across_session_ticks() -> None:
    assert "todo_waits" in _GLOBAL_STATE_KEYS and "todo_capacity" in _GLOBAL_STATE_KEYS


def test_the_skip_record_carries_the_numbers_behind_each_queue(monkeypatch) -> None:
    monkeypatch.setattr(daemon_module, "_review_wait_logged", {})
    sessions = {
        # #186: a card counts toward the review cap only once monitor_pr has said it waits on a human.
        "p1": {"current_card": {"id": "p1", "status": "IN_REVIEW", "pr_number": 95}, "phase": "monitoring_pr",
               "performer_stage": "closing_review", "pipeline_admitted": True, "review_wait": "human"},
        "p2": {"current_card": {"id": "p2", "status": "IN_REVIEW", "pr_number": 96}, "phase": "monitoring_pr",
               "performer_stage": "closing_review", "pipeline_admitted": True, "review_wait": "human"},
        "fresh": {"current_card": {"id": "fresh", "status": "TODO"}, "phase": "dispatching",
                  "performer_stage": "assessing"},
    }
    state = _state(sessions, limit=2)
    state["config"] = SimpleNamespace(max_concurrent_cards=2, max_cards_in_review=2)
    _compute_session_eligibilities(state, sessions, 2)
    record = state["session_skip_reasons"]["fresh"]
    assert record["reason"] == "review_capacity"
    assert record["waiting_on_review"] == 2 and record["review_limit"] == 2
    assert record["capacity"] == {"in_use": 0, "limit": 2}


def test_the_symphony_snapshot_carries_each_boards_waits_and_slots() -> None:
    sym_state = SimpleNamespace(
        board_snapshot={"TODO": ["PVTI_65"]},
        todo_waits={"PVTI_65": _entry(REASON_QUEUED, [], "pickup")},
        todo_capacity={"in_use": 1, "limit": 1, "waiting_on_review": 0, "review_limit": None, "hold": "queued"},
        session_skip_reasons={"run": {"reason": "pipeline_capacity", "capacity": {"in_use": 1, "limit": 1}}},
        active_sessions={}, active_card=None,
    )
    daemon = SimpleNamespace(state={
        "symphony_configs": {"board-a": SimpleNamespace()},
        "symphony_states": {"board-a": sym_state},
    })
    (entry,) = DashboardStore._build_symphonies_data(daemon)
    assert entry["state"]["todo_waits"] == {"PVTI_65": _entry(REASON_QUEUED, [], "pickup")}
    assert entry["state"]["todo_capacity"]["in_use"] == 1
    assert entry["state"]["session_skip_reasons"]["run"]["reason"] == "pipeline_capacity"


def test_a_symphony_with_no_waits_yet_has_an_empty_map_and_no_slots() -> None:
    daemon = SimpleNamespace(state={
        "symphony_configs": {"board-a": SimpleNamespace()},
        "symphony_states": {"board-a": SimpleNamespace(board_snapshot={})},
    })
    (entry,) = DashboardStore._build_symphonies_data(daemon)
    assert entry["state"]["todo_waits"] == {}
    assert entry["state"]["session_skip_reasons"] == {}
    assert entry["state"]["todo_capacity"] is None


# --- vocabulary ----------------------------------------------------------- #


def test_the_wait_words_come_from_the_glossary() -> None:
    assert [term.label for term in WAITS.values()] == ["Queued", "Waiting on", "Needs human", "Filtered out"]
    assert payload()["wait_labels"] == {code: term.label for code, term in WAITS.items()}
    assert "waits" in payload()


# --- client --------------------------------------------------------------- #


def test_the_waiting_script_is_shipped_before_the_application_script() -> None:
    assert "waiting" in _DASHBOARD_JS_FILES
    assert _DASHBOARD_JS_FILES[-1] == "app"
    assert _DASHBOARD_JS_FILES.index("waiting") < _DASHBOARD_JS_FILES.index("app")
    performers = _DASHBOARD_JS_SOURCES["performers"]
    assert "waitingLineHtml(waitingReason(" in performers
    assert "todoHeaderTitle(" in performers
    assert "queueLimitLabel(skip)" in performers
    assert "sessionSkipReasons(s)" in performers


def test_waiting_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "waiting.js"
    js_path.write_text(
        "\n\n".join([shipped_function("helpers", "esc"), time_helpers_js(), label_helpers_js(),
                     _DASHBOARD_JS_SOURCES["waiting"]]),
        encoding="utf-8",
    )
    checks = Path(__file__).resolve().parents[1] / "js" / "waiting_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    assert "FAIL" not in report, report
