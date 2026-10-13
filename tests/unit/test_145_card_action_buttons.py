"""#145: buttons for the existing cancel / skip / restart controls.

The endpoints existed and nothing in the dashboard called them. This covers
the server half (each action is recorded in the activity feed, Cancel can
never hit a card other than the one clicked, a queued control is exposed as
pending) and the client helpers under node.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.dashboard import _DASHBOARD_JS_FILES, _DASHBOARD_JS_SOURCES, DashboardStore
from coordinare.dashboard.routers.controls import record_operator_action
from coordinare.dashboard.store import _pending_control
from tests.unit.dashboard_js_slices import label_helpers_js, shipped_function
from tests.unit.test_dashboard import (
    _make_app,
    _make_mock_daemon,
    _make_mock_health,
    _make_mock_metrics,
)

_CARD = {"id": "PVTI_145", "issue_number": 57, "title": "Add login"}


def _running_daemon():
    daemon = _make_mock_daemon(phase="monitoring_performer")
    daemon.state.update(
        performer_stage="implementing", current_card=dict(_CARD), active_card_id="PVTI_145",
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    )
    return daemon


def _feed(store: DashboardStore) -> list[dict]:
    return [entry for entry in store.activity_log.snapshot() if entry["activity_type"] == "operator_action"]


# --- recorded in the activity feed --------------------------------------------- #


def test_skip_is_queued_and_recorded() -> None:
    store, daemon = DashboardStore(), _running_daemon()
    response = _make_app(store=store, daemon=daemon).post("/api/skip-role", params={"card_id": "PVTI_145"})
    assert response.status_code == 200 and response.json()["action"] == "skip"
    (entry,) = _feed(store)
    assert entry["card_id"] == "PVTI_145" and entry["card_number"] == 57
    assert entry["stage"] == "implementing"
    assert entry["text"] == "Operator asked to skip the current stage."


def test_restart_is_queued_and_recorded_with_the_stage_name() -> None:
    store, daemon = DashboardStore(), _running_daemon()
    response = _make_app(store=store, daemon=daemon).post("/api/restart-from/assessor", params={"card_id": "PVTI_145"})
    assert response.status_code == 200 and response.json()["target_stage"] == "assessing"
    (entry,) = _feed(store)
    assert entry["text"] == "Operator asked to restart from Assess."


def test_a_refused_control_is_not_recorded() -> None:
    store, daemon = DashboardStore(), _running_daemon()
    client = _make_app(store=store, daemon=daemon)
    assert client.post("/api/restart-from/nonsense").status_code == 400
    assert client.post("/api/skip-role", params={"card_id": "PVTI_OTHER"}).status_code == 400
    client.post("/api/skip-role")
    assert client.post("/api/skip-role").status_code == 409  # one already pending
    assert len(_feed(store)) == 1


def test_cancel_is_recorded_when_it_cancels(monkeypatch) -> None:
    store, daemon = DashboardStore(), _running_daemon()
    cancel = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    response = _make_app(store=store, daemon=daemon).post("/api/cancel", params={"card_id": "PVTI_145"})
    assert response.json() == {"status": "cancelled"}
    cancel.assert_awaited_once()
    (entry,) = _feed(store)
    assert entry["text"] == "Operator cancelled the card." and entry["card_number"] == 57


def test_cancel_with_nothing_to_cancel_is_not_recorded(monkeypatch) -> None:
    store, daemon = DashboardStore(), _running_daemon()
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", AsyncMock(return_value={"status": "no_active_card"}))
    _make_app(store=store, daemon=daemon).post("/api/cancel")
    assert _feed(store) == []


# --- cancel never hits a different card ---------------------------------------- #


def test_cancel_for_a_card_that_is_not_active_cancels_nothing(monkeypatch) -> None:
    store, daemon = DashboardStore(), _running_daemon()
    cancel = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    response = _make_app(store=store, daemon=daemon).post("/api/cancel", params={"card_id": "PVTI_SOMETHING_ELSE"})
    assert response.status_code == 409
    assert response.json()["status"] == "not_active_card"
    cancel.assert_not_awaited()
    assert _feed(store) == []


def test_cancel_without_a_card_id_still_works_as_before(monkeypatch) -> None:
    daemon = _running_daemon()
    cancel = AsyncMock(return_value={"status": "cancelled"})
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    assert _make_app(daemon=daemon).post("/api/cancel").json() == {"status": "cancelled"}
    cancel.assert_awaited_once()


def test_cancel_during_a_check_is_still_refused(monkeypatch) -> None:
    daemon = _running_daemon()
    daemon._cycle_active = True
    cancel = AsyncMock()
    monkeypatch.setattr("coordinare.cancel.cancel_active_card", cancel)
    response = _make_app(daemon=daemon).post("/api/cancel", params={"card_id": "PVTI_145"})
    assert response.status_code == 409 and response.json()["status"] == "cycle_in_progress"
    cancel.assert_not_awaited()


# --- recording is best effort -------------------------------------------------- #


def test_a_broken_feed_never_fails_the_action() -> None:
    def explode(**_kwargs):
        raise RuntimeError("feed down")

    broken = SimpleNamespace(activity_log=SimpleNamespace(record=explode))
    record_operator_action(broken, {"current_card": dict(_CARD)}, "text")
    record_operator_action(broken, None, "text")


# --- pending chip -------------------------------------------------------------- #


@pytest.mark.parametrize(("raw", "expected"), [
    ({"action": "skip", "control_id": "x"}, {"action": "skip", "target_stage": ""}),
    ({"action": "restart", "target_stage": "reviewing"}, {"action": "restart", "target_stage": "reviewing"}),
    ({"action": "skip", "applied": True}, None),
    ({}, None), (None, None), ("skip", None), ({"action": ""}, None),
])
def test_pending_control(raw, expected) -> None:
    assert _pending_control(raw) == expected


def test_session_summary_exposes_a_queued_control() -> None:
    store, daemon = DashboardStore(), _running_daemon()
    daemon.state["active_sessions"] = {"PVTI_145": {
        "phase": "monitoring_performer", "performer_stage": "implementing",
        "current_card": dict(_CARD), "pending_override": {"action": "skip", "control_id": "c1"},
    }}
    snapshot = store.build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())
    (session,) = snapshot["active_sessions"]
    assert session["pending_control"] == {"action": "skip", "target_stage": ""}

    daemon.state["active_sessions"]["PVTI_145"]["pending_override"] = {"action": "skip", "applied": True}
    (session,) = store.build_snapshot(daemon, _make_mock_metrics(), _make_mock_health())["active_sessions"]
    assert session["pending_control"] is None


# --- client ------------------------------------------------------------------- #


def test_actions_script_is_shipped_and_wired() -> None:
    assert "actions" in _DASHBOARD_JS_FILES
    performers = _DASHBOARD_JS_SOURCES["performers"]
    assert performers.count("cardActionsHtml(sess, s)") == 2  # the tile and card detail
    assert performers.count("pendingControlHtml(sess)") == 2
    actions = _DASHBOARD_JS_SOURCES["actions"]
    assert "window.confirm(cardActionEffect(kind, stage))" in actions
    assert "performers" in _DASHBOARD_JS_FILES and _DASHBOARD_JS_FILES.index("actions") > _DASHBOARD_JS_FILES.index("helpers")


def test_card_action_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "actions.js"
    js_path.write_text(
        "\n\n".join([shipped_function("helpers", "esc"), label_helpers_js(), _DASHBOARD_JS_SOURCES["actions"]]),
        encoding="utf-8",
    )
    checks = Path(__file__).resolve().parents[1] / "js" / "card_action_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 48, f"only {summary.group(1)} checks ran:\n{report}"
