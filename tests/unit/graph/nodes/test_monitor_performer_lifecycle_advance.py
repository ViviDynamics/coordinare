"""Spec 076 T081 — DONE-handling stage advancement (FR-018).

Verifies that ``_advance_stage`` (already invoked on TERMINAL_SUCCESS_STATES)
moves the lifecycle forward AND records PR artefacts via T073's helper.
The wiring is exercised — full _advance_stage behaviour is covered by
pre-076 tests.
"""
from __future__ import annotations

from coordinare.graph.nodes.monitor_performer import _advance_stage


def test_advance_stage_promotes_to_next_role() -> None:
    """FR-018: DONE with more roles remaining → next stage queued."""
    state = {
        "lifecycle_sequence": ["implementing", "reviewing", "closing_review"],
        "performer_stage": "implementing",
        "current_card": {"id": "PVTI_X", "title": "card"},
        "active_sessions": {
            "PVTI_X": {
                "current_card": {"id": "PVTI_X", "title": "card"},
                "performer_stage": "implementing",
            },
        },
    }
    updates = _advance_stage(state, status={"pr_url": "https://x/y/pull/1"})
    assert updates["performer_stage"] == "reviewing"
    assert updates["phase"] == "dispatching"
    assert updates["agent_dispatch"] == {}
    # PR artefact captured via T073 _record_pr_artefacts
    assert updates["current_card"]["pr_url"] == "https://x/y/pull/1"


def test_advance_stage_pr_url_mirrored_to_active_session() -> None:
    """FR-016: DONE write-through hits active_sessions[card_id] too."""
    state = {
        "lifecycle_sequence": ["implementing", "reviewing"],
        "performer_stage": "implementing",
        "current_card": {"id": "PVTI_X"},
        "active_sessions": {
            "PVTI_X": {
                "current_card": {"id": "PVTI_X"},
                "performer_stage": "implementing",
            },
        },
    }
    _advance_stage(state, status={"pr_url": "https://x/y/pull/148"})
    sess_card = state["active_sessions"]["PVTI_X"]["current_card"]
    assert sess_card["pr_url"] == "https://x/y/pull/148"
    assert state["active_sessions"]["PVTI_X"]["pr_artefacts_recorded_at"] is not None


def test_advance_stage_without_status_is_clean() -> None:
    """DONE with no status payload (legacy single-stage exit) MUST NOT crash."""
    state = {
        "lifecycle_sequence": ["implementing", "reviewing"],
        "performer_stage": "implementing",
        "current_card": {"id": "PVTI_X"},
        "active_sessions": {"PVTI_X": {"current_card": {"id": "PVTI_X"}}},
    }
    updates = _advance_stage(state, status=None)
    assert updates["performer_stage"] == "reviewing"
    # No PR fields written when no status
    assert "current_card" not in updates or "pr_url" not in updates.get("current_card", {})


def test_advance_stage_terminal_transitions_to_monitoring_pr() -> None:
    """FR-018: final stage exhausted → move to monitoring_pr."""
    state = {
        "lifecycle_sequence": ["implementing"],
        "performer_stage": "implementing",
        "current_card": {"id": "PVTI_X", "pr_url": "https://x/y/pull/1", "pr_node_id": "PR_1"},
        "active_sessions": {
            "PVTI_X": {
                "current_card": {"id": "PVTI_X", "pr_url": "https://x/y/pull/1", "pr_node_id": "PR_1"},
            },
        },
        "github_service": None,
        "workspace_path": None,
    }
    updates = _advance_stage(state, status={"pr_url": "https://x/y/pull/1"})
    assert updates["phase"] == "monitoring_pr"
