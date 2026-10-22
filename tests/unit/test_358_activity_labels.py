"""358: an activity stage change names its card by number, and a symphony swap is not a pickup.

A session's ``current_card`` can be missing, so the label falls back to the board's issue
numbers and titles. While the daemon runs one symphony's cycle it swaps ``active_sessions``
down to that symphony's sessions; the watcher used to read the others as departed, then as
"picked up" again when the aggregate came back, seconds apart, every cycle.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from coordinare.dashboard import DashboardStore


def _session(stage: str, card: dict[str, Any] | None = None) -> dict[str, Any]:
    sess: dict[str, Any] = {"phase": "monitoring_performer", "performer_stage": stage}
    if card is not None:
        sess["current_card"] = card
    return sess


def _daemon(state: dict[str, Any]) -> MagicMock:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0, **state}
    return daemon


def _symphony(sessions: dict[str, Any], numbers: dict[str, int] | None = None, titles: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(active_sessions=sessions, board_issue_numbers=numbers or {}, board_titles=titles or {})


def _stage_changes(store: DashboardStore) -> list[dict[str, Any]]:
    return [e for e in store.activity_log.snapshot() if e["activity_type"] == "stage_change"]


def _tick(store: DashboardStore, daemon: MagicMock, previous: tuple[Any, ...] | None) -> tuple[Any, ...]:
    current = store._active_sessions_fingerprint(daemon)
    store._record_stage_changes(daemon, previous, current)
    return current


def test_a_card_without_current_card_takes_its_number_from_the_symphony_board() -> None:
    sessions = {"PVTI_lADOabc": _session("assessing")}
    daemon = _daemon({
        "active_sessions": sessions,
        "symphony_states": {"alpha": _symphony(sessions, {"PVTI_lADOabc": 341}, {"PVTI_lADOabc": "Fix the poller"})},
    })
    store = DashboardStore()
    _tick(store, daemon, None)
    entry = _stage_changes(store)[-1]
    assert entry["card_id"] == "PVTI_lADOabc"
    assert entry["card_number"] == 341
    assert entry["card_title"] == "Fix the poller"


def test_a_single_symphony_board_map_on_the_flat_state_also_resolves_the_number() -> None:
    daemon = _daemon({
        "active_sessions": {"PVTI_x": _session("implementing", {"id": "PVTI_x"})},
        "_board_issue_numbers": {"PVTI_x": 12},
        "_board_titles": {"PVTI_x": "Retry budget"},
    })
    store = DashboardStore()
    _tick(store, daemon, None)
    entry = _stage_changes(store)[-1]
    assert (entry["card_number"], entry["card_title"]) == (12, "Retry budget")


def test_the_session_card_wins_over_the_board_map() -> None:
    daemon = _daemon({
        "active_sessions": {"PVTI_x": _session("implementing", {"id": "PVTI_x", "issue_number": 7, "title": "Own"})},
        "_board_issue_numbers": {"PVTI_x": 99},
        "_board_titles": {"PVTI_x": "Board"},
    })
    store = DashboardStore()
    _tick(store, daemon, None)
    entry = _stage_changes(store)[-1]
    assert (entry["card_number"], entry["card_title"]) == (7, "Own")


def test_an_unknown_card_keeps_a_missing_number() -> None:
    daemon = _daemon({"active_sessions": {"PVTI_x": _session("assessing")}})
    store = DashboardStore()
    _tick(store, daemon, None)
    assert _stage_changes(store)[-1]["card_number"] is None


def test_a_symphony_swap_does_not_log_a_second_pickup() -> None:
    alpha = {"PVTI_a": _session("assessing", {"id": "PVTI_a", "issue_number": 1, "title": "A"})}
    beta = {"PVTI_b": _session("implementing", {"id": "PVTI_b", "issue_number": 2, "title": "B"})}
    states = {"alpha": _symphony(alpha), "beta": _symphony(beta)}
    daemon = _daemon({"active_sessions": {**alpha, **beta}, "symphony_states": states})
    store = DashboardStore()
    fp = _tick(store, daemon, None)
    assert len(_stage_changes(store)) == 2

    # beta's cycle: only beta's sessions are in the flat map while it runs.
    daemon.state["active_sessions"] = dict(beta)
    daemon.state["current_symphony"] = "beta"
    fp = _tick(store, daemon, fp)
    # The cycle ends and the aggregate comes back.
    daemon.state["active_sessions"] = {**alpha, **beta}
    daemon.state["current_symphony"] = None
    _tick(store, daemon, fp)

    texts = [e["text"] for e in _stage_changes(store)]
    assert texts.count("picked up in assessing") == 1, texts
    assert len(texts) == 2, texts


def test_a_real_stage_change_inside_a_swapped_cycle_is_still_recorded() -> None:
    alpha = {"PVTI_a": _session("assessing", {"id": "PVTI_a", "issue_number": 1})}
    beta = {"PVTI_b": _session("implementing", {"id": "PVTI_b", "issue_number": 2})}
    daemon = _daemon({"active_sessions": {**alpha, **beta}, "symphony_states": {"alpha": _symphony(alpha), "beta": _symphony(beta)}})
    store = DashboardStore()
    fp = _tick(store, daemon, None)

    swapped = {"PVTI_b": _session("reviewing", {"id": "PVTI_b", "issue_number": 2})}
    daemon.state["active_sessions"] = swapped
    daemon.state["current_symphony"] = "beta"
    _tick(store, daemon, fp)

    last = _stage_changes(store)[-1]
    assert (last["card_number"], last["text"]) == (2, "stage → reviewing")


def test_a_card_that_really_leaves_is_still_forgotten() -> None:
    alpha = {"PVTI_a": _session("assessing", {"id": "PVTI_a", "issue_number": 1})}
    daemon = _daemon({"active_sessions": dict(alpha), "symphony_states": {"alpha": _symphony(alpha)}})
    store = DashboardStore()
    fp = _tick(store, daemon, None)
    assert "PVTI_a" in store.activity_log._seen

    daemon.state["active_sessions"] = {}
    daemon.state["symphony_states"] = {"alpha": _symphony({})}
    _tick(store, daemon, fp)
    assert "PVTI_a" not in store.activity_log._seen
