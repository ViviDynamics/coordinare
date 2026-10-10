"""Dashboard history must use the actual session that owns a human answer."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coordinare.daemon import CoordinareDaemon, _finalize_multi_session
from coordinare.dashboard import DashboardStore
from coordinare.session import create_session_from_card
from coordinare.state_store import WorkflowSnapshot


def dashboard(daemon):
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return DashboardStore().build_snapshot(daemon, metrics, health)


def owner(card_id, number, stage, answer):
    session = create_session_from_card({"id": card_id, "issue_number": number, "title": f"Story {number}",
                                        "issue_url": f"https://github.com/example/sample/issues/{number}", "status": "BLOCKED"})
    session.update(phase="blocked", performer_stage=stage, card_clarifications=[{"questions": ["Human question"], "answer": answer}])
    return session


@pytest.mark.parametrize("focus", [False, True])
@pytest.mark.parametrize("restore", [False, True])
def test_authoritative_history_survives_stale_flat_mirror(focus, restore):
    a = owner("A", 1, "assessing", "Return an empty string.")
    b = owner("B", 2, "implementing", "No, tests only.")
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon.state.update(active_sessions={"A": a, "B": b}, active_card_id="A",
                        card_clarifications=deepcopy(b["card_clarifications"]))
    _finalize_multi_session(daemon.state, daemon.state["active_sessions"])
    if not focus:
        daemon.state.update(active_card_id=None, current_card=None)
    snapshot = daemon._build_snapshot()
    if restore:
        snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    daemon._state_store = SimpleNamespace(last_snapshot=snapshot)
    before = deepcopy(daemon.state["active_sessions"])
    rounds = dashboard(daemon)["card_clarifications"]
    assert [(entry.get("card_id"), entry.get("answer")) for entry in rounds] == [
        ("A", "Return an empty string."), ("B", "No, tests only.")]
    assert [(entry["card_number"], entry["card_title"], entry["stage"], entry["issue_url"]) for entry in rounds] == [
        (1, "Story 1", "assessing", "https://github.com/example/sample/issues/1"),
        (2, "Story 2", "implementing", "https://github.com/example/sample/issues/2")]
    assert daemon.state["active_sessions"] == before
    assert snapshot.card_clarifications == b["card_clarifications"]


def test_empty_live_history_does_not_resurrect_stale_snapshot_rounds():
    a = owner("A", 1, "assessing", "Current answer")
    a["card_clarifications"] = []
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon.state.update(active_sessions={"A": a})
    daemon._state_store = SimpleNamespace(last_snapshot=WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked",
                                            card_clarifications=[{"answer": "Foreign stale answer"}]))
    assert dashboard(daemon)["card_clarifications"] == []


def test_live_history_is_available_before_any_saved_snapshot():
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon.state.update(active_sessions={"A": owner("A", 1, "assessing", "New answer")})
    rounds = dashboard(daemon)["card_clarifications"]
    assert [(entry.get("card_id"), entry.get("answer")) for entry in rounds] == [("A", "New answer")]


@pytest.mark.parametrize("live", [False, True])
def test_explicit_round_attribution_and_content_are_preserved(live):
    round_ = {"card_id": "original-owner", "card_number": 9, "card_title": "Original story", "stage": "reviewing",
              "issue_url": "https://github.com/example/sample/issues/9", "body": "Exact Unicode λ\nanswer", "comment_id": 12}
    daemon = CoordinareDaemon(SimpleNamespace())
    a = owner("A", 1, "assessing", "Unused answer")
    a["card_clarifications"] = [deepcopy(round_)]
    daemon.state.update(active_sessions={"A": a} if live else {})
    daemon._state_store = SimpleNamespace(last_snapshot=WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked",
                                              card_clarifications=[deepcopy(round_)]))
    assert dashboard(daemon)["card_clarifications"] == [round_]
    assert a["card_clarifications"] == [round_]


@pytest.mark.parametrize("focus", [False, True])
@pytest.mark.parametrize("second_restart", [False, True])
def test_actual_daemon_restart_preserves_visible_history_owners(focus, second_restart):
    a = owner("A", 1, "assessing", "Return an empty string.")
    b = owner("B", 2, "implementing", "No, tests only.")
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon.state.update(active_sessions={"A": a, "B": b}, active_card_id="A",
                        card_clarifications=deepcopy(b["card_clarifications"]))
    _finalize_multi_session(daemon.state, daemon.state["active_sessions"])
    if not focus:
        daemon.state.update(active_card_id=None, current_card=None)
    before = deepcopy(daemon.state["active_sessions"])
    snapshot = WorkflowSnapshot.model_validate_json(daemon._build_snapshot().model_dump_json())
    assert daemon.state["active_sessions"] == before
    for _ in range(2 if second_restart else 1):
        restored = CoordinareDaemon(SimpleNamespace())
        restored._restore_from_snapshot(snapshot)
        restored._state_store = SimpleNamespace(last_snapshot=snapshot)
        rounds = dashboard(restored)["card_clarifications"]
        assert [(r["card_id"], r["card_number"], r["card_title"], r["issue_url"], r["stage"], r["answer"]) for r in rounds] == [
            ("A", 1, "Story 1", "https://github.com/example/sample/issues/1", "assessing", "Return an empty string."),
            ("B", 2, "Story 2", "https://github.com/example/sample/issues/2", "implementing", "No, tests only.")]
        snapshot = WorkflowSnapshot.model_validate_json(restored._build_snapshot().model_dump_json())


def test_snapshot_preserves_explicit_history_metadata_and_exact_content():
    round_ = {"card_id": "historical-owner", "card_number": None, "card_title": "Historical title",
              "stage": "reviewing", "issue_url": None, "answer": "Exact Unicode λ\nanswer", "comment_id": 12}
    daemon = CoordinareDaemon(SimpleNamespace())
    a = owner("A", 1, "assessing", "Unused")
    a["card_clarifications"] = [deepcopy(round_)]
    daemon.state.update(active_sessions={"A": a})
    snapshot = daemon._build_snapshot()
    assert snapshot.active_sessions["A"].card_clarifications == [round_]
    assert a["card_clarifications"] == [round_]


@pytest.mark.parametrize("known_key, missing_keys", [
    ("issue_number", ["card_title", "issue_url"]),
    ("title", ["card_number", "issue_url"]),
])
def test_partial_owner_metadata_can_be_filled_after_board_refresh(known_key, missing_keys):
    session = owner("A", 1, "assessing", "Exact answer")
    complete_card = deepcopy(session["current_card"])
    session["current_card"] = {"id": "A", known_key: complete_card[known_key]}
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon.state.update(active_sessions={"A": session})
    snapshot = WorkflowSnapshot.model_validate_json(daemon._build_snapshot().model_dump_json())
    for key in missing_keys:
        assert key not in snapshot.active_sessions["A"].card_clarifications[0]
    restored = CoordinareDaemon(SimpleNamespace())
    restored._restore_from_snapshot(snapshot)
    restored.state["active_sessions"]["A"]["current_card"] = complete_card
    enriched = WorkflowSnapshot.model_validate_json(restored._build_snapshot().model_dump_json())
    restored_again = CoordinareDaemon(SimpleNamespace())
    restored_again._restore_from_snapshot(enriched)
    restored_again._state_store = SimpleNamespace(last_snapshot=enriched)
    round_ = dashboard(restored_again)["card_clarifications"][0]
    assert (round_["card_number"], round_["card_title"], round_["issue_url"], round_["answer"]) == (
        1, "Story 1", "https://github.com/example/sample/issues/1", "Exact answer")


@pytest.mark.parametrize("history", [None, "malformed"])
def test_known_owner_with_malformed_history_still_snapshots_safely(history):
    session = owner("A", 1, "assessing", "Unused")
    session["card_clarifications"] = history
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon.state.update(active_sessions={"A": session})
    assert daemon._build_snapshot().active_sessions["A"].card_clarifications == []
