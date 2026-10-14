"""#149: why each waiting card is waiting.

The conductor decides, in check_board, whether a To Do card with no session is
picked up. Those decisions are written into the per-symphony state where they
are made (services/todo_waits.py), and the daemon carries them to the snapshot.
The budget verdict for a new card is the dispatch gate's own (pipeline_budget),
so a card parked in review does not count as a slot, and the review cap is
named when it is what holds the card. This covers the recorder, the dependency
verdict that overrides it, the daemon plumbing and the snapshot shape. The
browser side is covered by tests/js/waiting_checks.js and
tests/e2e/test_149_waiting_reasons.py.
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
    REASON_REVIEW_CAPACITY,
    REASON_UNASSIGNED,
    REASON_WAITING_ON,
    record_dependency_waits,
    refresh_todo_waits,
)
from coordinare.vocabulary import WAITS, payload
from tests.unit.dashboard_js_slices import label_helpers_js, shipped_function

_STRICT = OwnershipPolicy(login="coordinare-bot", include_unassigned=False)
_OPEN = OwnershipPolicy(login="coordinare-bot", include_unassigned=True)
_NO_FILTER = OwnershipPolicy()  # neither opt-in set: every card is eligible


def _parked(cid: str) -> dict[str, Any]:
    return {"current_card": {"id": cid, "status": "IN_REVIEW", "pr_number": 95},
            "phase": "monitoring_pr", "performer_stage": "closing_review", "pipeline_admitted": True}


def _state(sessions: dict[str, Any] | None = None, *, limit: int = 1, review_limit: int | None = None,
           selected: set[str] | None = None) -> Any:
    state = initial_state()
    state["active_sessions"] = sessions or {}
    state["config"] = SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=review_limit)
    if selected is not None:
        state["_pipeline_selected"] = selected
    return state


def _refresh(state, todo, *, active=(), assignees=None, labels=None, ownership=_OPEN, limit=1):
    refresh_todo_waits(
        state,
        board={"item_labels": labels or {}},
        todo=list(todo),
        active_card_ids=set(active),
        ownership=ownership,
        assignees=assignees or {},
        limit=limit,
    )
    return state["todo_waits"]


def _graph(**by_dependent: list[CardDependency]) -> DependencyGraph:
    return DependencyGraph(by_dependent=dict(by_dependent))


def _dep(card: str, number: int, status: DependencyStatus, repo: str = "") -> CardDependency:
    return CardDependency(dependent_item_id=card, blocker_issue_number=number, status=status, blocker_repo=repo)


def _full(state: Any, limit: int = 1) -> Any:
    """Fill every slot with a running card, as the dispatch gate would have."""
    state["_pipeline_selected"] = {f"run{i}" for i in range(limit)}
    return state


# --- the pre-pickup estimate: slots --------------------------------------- #


def test_a_full_board_queues_every_card_that_is_otherwise_free() -> None:
    state = _full(_state())
    waits = _refresh(state, ["PVTI_65", "PVTI_66"])
    assert waits == {"PVTI_65": {"reason": REASON_QUEUED, "blockers": [], "detail": None},
                     "PVTI_66": {"reason": REASON_QUEUED, "blockers": [], "detail": None}}
    assert state["todo_capacity"]["in_use"] == 1 and state["todo_capacity"]["limit"] == 1
    assert state["todo_capacity"]["hold"] == REASON_QUEUED


def test_a_free_slot_records_no_queue() -> None:
    state = _state(selected=set())
    assert _refresh(state, ["PVTI_65"]) == {}
    assert state["todo_capacity"]["hold"] is None


def test_an_escalated_card_is_named_before_anything_else() -> None:
    state = _full(_state())
    state["advocate_escalation_label"] = "needs-human"
    waits = _refresh(state, ["PVTI_1"], labels={"PVTI_1": ["needs-human"]})
    assert waits == {"PVTI_1": {"reason": REASON_ESCALATED, "blockers": [], "detail": None}}


def test_an_answered_advocate_label_does_not_count_as_escalated() -> None:
    state = _state(selected=set())
    state["advocate_escalation_label"] = "needs-human"
    assert _refresh(state, ["PVTI_1"], labels={"PVTI_1": ["answered"]}) == {}


def test_a_card_someone_else_holds_is_filtered_out_with_its_reason() -> None:
    state = _state(selected=set())
    waits = _refresh(state, ["PVTI_1", "PVTI_2"], assignees={"PVTI_1": ["alice"]}, ownership=_STRICT)
    assert waits == {"PVTI_1": {"reason": REASON_ASSIGNED_ELSEWHERE, "blockers": [], "detail": None},
                     "PVTI_2": {"reason": REASON_UNASSIGNED, "blockers": [], "detail": None}}


def test_an_unassigned_card_is_admitted_when_the_filter_allows_it() -> None:
    assert _refresh(_state(selected=set()), ["PVTI_2"]) == {}


def test_a_card_with_a_session_never_gets_a_todo_wait() -> None:
    state = _full(_state())
    assert _refresh(state, ["PVTI_1"], active=["PVTI_1"]) == {}


def test_no_filter_configured_admits_every_card_to_the_queue_check() -> None:
    state = _state(selected={"a", "b"}, limit=2)
    waits = _refresh(state, ["PVTI_1"], ownership=_NO_FILTER, limit=2)
    assert waits == {"PVTI_1": {"reason": REASON_QUEUED, "blockers": [], "detail": None}}


def test_a_card_that_leaves_the_todo_column_loses_its_wait() -> None:
    state = _full(_state())
    state["todo_waits"] = {"PVTI_1": {"reason": REASON_QUEUED, "blockers": []}}
    assert _refresh(state, []) == {}


# --- the pre-pickup estimate: the review cap (#179) ----------------------- #


def test_a_parked_card_does_not_use_a_slot() -> None:
    """With one slot, a card waiting on review leaves it free for a To Do card."""
    sessions = {"reviewed": _parked("reviewed")}
    state = _state(sessions, selected=set())
    assert _refresh(state, ["PVTI_9"]) == {}
    assert state["todo_capacity"]["in_use"] == 0


def test_a_reached_review_cap_holds_a_free_slot_and_says_so() -> None:
    sessions = {"p1": _parked("p1"), "p2": _parked("p2")}
    state = _state(sessions, limit=2, review_limit=2, selected=set())
    waits = _refresh(state, ["PVTI_9"], limit=2)
    assert waits == {"PVTI_9": {"reason": REASON_REVIEW_CAPACITY, "blockers": [],
                                "detail": "max_cards_in_review is 2: 2 waiting on review"}}
    assert state["todo_capacity"] == {"in_use": 0, "limit": 2, "waiting_on_review": 2,
                                      "review_limit": 2, "hold": REASON_REVIEW_CAPACITY}


def test_slots_full_takes_precedence_over_the_review_cap() -> None:
    sessions = {"p1": _parked("p1"), "p2": _parked("p2")}
    state = _state(sessions, limit=2, review_limit=2, selected={"a", "b"})
    waits = _refresh(state, ["PVTI_9"], limit=2)
    assert waits["PVTI_9"]["reason"] == REASON_QUEUED


def test_the_review_cap_is_read_from_the_budget_not_recomputed() -> None:
    """Uses configured_review_limit, which never drops below max_concurrent_cards."""
    sessions = {"p1": _parked("p1")}
    state = _state(sessions, limit=3, review_limit=1, selected=set())
    assert _refresh(state, ["PVTI_9"], limit=3) == {}
    assert state["todo_capacity"]["review_limit"] == 3


# --- the dependency filter's verdict -------------------------------------- #


def test_a_card_held_back_by_a_pending_blocker_waits_on_it() -> None:
    state = _state(selected=set())
    graph = _graph(PVTI_1=[_dep("PVTI_1", 42, DependencyStatus.PENDING)])
    record_dependency_waits(state, dep_graph=graph, filtered_ids=["PVTI_1"], passed_ids=[])
    assert state["todo_waits"] == {"PVTI_1": {"reason": REASON_WAITING_ON, "blockers": ["#42"], "detail": None}}


def test_a_foreign_blocker_is_named_with_its_repository() -> None:
    state = _state(selected=set())
    graph = _graph(PVTI_1=[_dep("PVTI_1", 7, DependencyStatus.PENDING, repo="org/other")])
    record_dependency_waits(state, dep_graph=graph, filtered_ids=["PVTI_1"], passed_ids=[])
    assert state["todo_waits"]["PVTI_1"]["blockers"] == ["org/other#7"]


def test_an_unresolvable_blocker_is_not_claimed_as_a_wait() -> None:
    state = _state(selected=set())
    state["todo_waits"] = {"PVTI_1": {"reason": REASON_WAITING_ON, "blockers": ["#9"]}}
    graph = _graph(PVTI_1=[_dep("PVTI_1", 9, DependencyStatus.UNRESOLVABLE)])
    record_dependency_waits(state, dep_graph=graph, filtered_ids=["PVTI_1"], passed_ids=[])
    assert state["todo_waits"] == {}


def test_a_card_that_clears_its_dependencies_takes_the_budget_hold() -> None:
    state = _state(selected=set())
    state["todo_waits"] = {"PVTI_1": {"reason": REASON_WAITING_ON, "blockers": ["#42"]}}
    state["todo_capacity"] = {"in_use": 1, "limit": 1, "waiting_on_review": 0, "review_limit": None,
                              "hold": REASON_QUEUED}
    record_dependency_waits(state, dep_graph=_graph(), filtered_ids=[], passed_ids=["PVTI_1"])
    assert state["todo_waits"] == {"PVTI_1": {"reason": REASON_QUEUED, "blockers": [], "detail": None}}

    state["todo_capacity"]["hold"] = None
    record_dependency_waits(state, dep_graph=_graph(), filtered_ids=[], passed_ids=["PVTI_1"])
    assert state["todo_waits"] == {}


def test_a_review_hold_carries_its_detail_through_the_dependency_verdict() -> None:
    state = _state(selected=set())
    state["todo_capacity"] = {"in_use": 0, "limit": 2, "waiting_on_review": 2, "review_limit": 2,
                              "hold": REASON_REVIEW_CAPACITY}
    record_dependency_waits(state, dep_graph=_graph(), filtered_ids=[], passed_ids=["PVTI_1"])
    assert state["todo_waits"]["PVTI_1"]["detail"] == "max_cards_in_review is 2: 2 waiting on review"


def test_a_full_board_says_waiting_on_for_a_card_with_a_pending_blocker(monkeypatch) -> None:
    graph = _graph(PVTI_1=[_dep("PVTI_1", 42, DependencyStatus.PENDING)])
    monkeypatch.setattr(todo_waits, "_dependency_graph", lambda _state, _board: graph)
    waits = _refresh(_full(_state()), ["PVTI_1", "PVTI_2"])
    assert waits == {"PVTI_1": {"reason": REASON_WAITING_ON, "blockers": ["#42"], "detail": None},
                     "PVTI_2": {"reason": REASON_QUEUED, "blockers": [], "detail": None}}


def test_a_failed_dependency_read_leaves_the_queue_reason_blank(monkeypatch) -> None:
    def broken(_state, _board):
        raise ValueError("board shape")

    monkeypatch.setattr(todo_waits, "_dependency_graph", broken)
    state = _full(_state())
    state["advocate_escalation_label"] = "needs-human"
    waits = _refresh(state, ["PVTI_1", "PVTI_2"], labels={"PVTI_2": ["needs-human"]})
    assert waits == {"PVTI_2": {"reason": REASON_ESCALATED, "blockers": [], "detail": None}}


# --- the daemon keeps them ------------------------------------------------ #


def test_the_fanout_merge_carries_the_todo_waits_across_session_ticks() -> None:
    assert "todo_waits" in _GLOBAL_STATE_KEYS and "todo_capacity" in _GLOBAL_STATE_KEYS


def test_the_skip_record_carries_the_numbers_behind_each_queue(monkeypatch) -> None:
    monkeypatch.setattr(daemon_module, "_review_wait_logged", {})
    sessions = {"p1": _parked("p1"), "p2": _parked("p2"), "fresh": {
        "current_card": {"id": "fresh", "status": "TODO"}, "phase": "dispatching", "performer_stage": "assessing"}}
    state = _state(sessions, limit=2, review_limit=2)
    _compute_session_eligibilities(state, sessions, 2)
    record = state["session_skip_reasons"]["fresh"]
    assert record["reason"] == "review_capacity"
    assert record["waiting_on_review"] == 2 and record["review_limit"] == 2
    assert record["capacity"] == {"in_use": 0, "limit": 2}


def test_the_symphony_snapshot_carries_each_boards_waits_and_slots() -> None:
    sym_state = SimpleNamespace(
        board_snapshot={"TODO": ["PVTI_65"]}, todo_waits={"PVTI_65": {"reason": REASON_QUEUED, "blockers": []}},
        todo_capacity={"in_use": 1, "limit": 1, "waiting_on_review": 0, "review_limit": None, "hold": "queued"},
        active_sessions={}, active_card=None,
    )
    daemon = SimpleNamespace(state={
        "symphony_configs": {"board-a": SimpleNamespace()},
        "symphony_states": {"board-a": sym_state},
    })
    (entry,) = DashboardStore._build_symphonies_data(daemon)
    assert entry["state"]["todo_waits"] == {"PVTI_65": {"reason": REASON_QUEUED, "blockers": []}}
    assert entry["state"]["todo_capacity"]["in_use"] == 1


def test_a_symphony_with_no_waits_yet_has_an_empty_map_and_no_slots() -> None:
    daemon = SimpleNamespace(state={
        "symphony_configs": {"board-a": SimpleNamespace()},
        "symphony_states": {"board-a": SimpleNamespace(board_snapshot={})},
    })
    (entry,) = DashboardStore._build_symphonies_data(daemon)
    assert entry["state"]["todo_waits"] == {}
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


def test_waiting_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "waiting.js"
    js_path.write_text(
        "\n\n".join([shipped_function("helpers", "esc"), label_helpers_js(),
                     _DASHBOARD_JS_SOURCES["waiting"]]),
        encoding="utf-8",
    )
    checks = Path(__file__).resolve().parents[1] / "js" / "waiting_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    assert "FAIL" not in report, report
