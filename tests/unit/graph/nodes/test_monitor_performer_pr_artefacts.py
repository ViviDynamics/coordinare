"""Spec 076 T076 — _record_pr_artefacts unit tests.

Covers FR-015 (PR fields write-through) and FR-016 (synchronous mirror
to active_sessions for restart-safe persistence).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.monitor_performer import _record_pr_artefacts


def _base_state(card_id: str = "PVTI_X") -> dict:
    card = {"id": card_id, "title": "Test", "status": "IN_PROGRESS"}
    return {
        "current_card": dict(card),
        "active_sessions": {
            card_id: {
                "current_card": dict(card),
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
            },
        },
    }


def test_no_status_is_noop() -> None:
    state = _base_state()
    updates = _record_pr_artefacts(state, None)
    assert updates == {}


def test_status_without_artefacts_is_noop() -> None:
    """A status with no PR identifiers MUST NOT touch the card."""
    state = _base_state()
    updates = _record_pr_artefacts(state, {"state": "succeeded", "comment": "fine"})
    assert updates == {}


def test_pr_url_written_to_current_card() -> None:
    """FR-015: pr_url MUST land on state.active_card synchronously."""
    state = _base_state()
    updates = _record_pr_artefacts(
        state,
        {"pr_url": "https://github.com/x/y/pull/148", "pr_node_id": "PR_148"},
    )
    assert updates["current_card"]["pr_url"] == "https://github.com/x/y/pull/148"
    assert updates["current_card"]["pr_node_id"] == "PR_148"


def test_artefacts_mirrored_to_active_sessions_entry() -> None:
    """FR-016: write-through MUST also touch active_sessions[card_id] so
    the snapshot persists the new PR through a daemon restart."""
    state = _base_state()
    _record_pr_artefacts(
        state,
        {
            "pr_url": "https://github.com/x/y/pull/148",
            "pr_node_id": "PR_148",
            "pr_number": 148,
            "head_sha": "abc12345",
        },
    )
    sess_card = state["active_sessions"]["PVTI_X"]["current_card"]
    assert sess_card["pr_url"] == "https://github.com/x/y/pull/148"
    assert sess_card["pr_node_id"] == "PR_148"
    assert sess_card["pr_number"] == 148
    assert sess_card["head_after"] == "abc12345"


def test_pr_artefacts_recorded_at_timestamp_stamped() -> None:
    """The audit timestamp MUST be set when artefacts are written."""
    state = _base_state()
    before = datetime.now(UTC)
    _record_pr_artefacts(state, {"pr_url": "https://github.com/x/y/pull/148"})
    after = datetime.now(UTC)
    ts = state["active_sessions"]["PVTI_X"]["pr_artefacts_recorded_at"]
    assert isinstance(ts, datetime)
    assert before <= ts <= after


def test_pushed_branch_recorded_for_contract_verification() -> None:
    """FR-023 / contract: pushed_branch MUST be captured so downstream
    verification can compare against the canonical branch."""
    state = _base_state()
    updates = _record_pr_artefacts(state, {"pushed_branch": "coordinare/PVTI_X/feat-test"})
    assert updates["current_card"]["pushed_branch"] == "coordinare/PVTI_X/feat-test"


def test_no_active_sessions_entry_is_tolerated() -> None:
    """A missing active_sessions entry MUST NOT crash the helper — older
    flows may write through current_card only."""
    state = {"current_card": {"id": "PVTI_LEGACY"}, "active_sessions": {}}
    updates = _record_pr_artefacts(state, {"pr_url": "https://x/y/pull/1"})
    # current_card still updated
    assert updates["current_card"]["pr_url"] == "https://x/y/pull/1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("pr_url", "https://github.com/x/y/pull/148"),
        ("pr_node_id", "PR_148"),
        ("pr_number", 148),
        ("head_sha", "abc12345"),
        ("pushed_branch", "coordinare/PVTI_X/feat"),
        ("plan_path", "docs/cards/101/plan.md"),
    ],
)
def test_each_field_independently_triggers_write_through(field, value) -> None:
    """Each individual artefact field MUST trigger write-through on its
    own — operators get a partial PR update if the performer reports only
    one field."""
    state = _base_state()
    updates = _record_pr_artefacts(state, {field: value})
    assert "current_card" in updates


def test_head_after_fills_head_sha() -> None:
    """The performer wire carries ``head_after`` (070); the write-through
    MUST record it when the legacy ``head_sha`` key is absent. Production
    observed head_sha=None with a live pr_opened response otherwise."""
    state = _base_state()
    updates = _record_pr_artefacts(
        state,
        {"pr_url": "https://github.com/x/y/pull/148", "head_after": "abc12345"},
    )
    assert updates["current_card"]["head_after"] == "abc12345"


def test_head_sha_key_still_wins_when_present() -> None:
    """If a payload carries both keys, the explicit head_sha stays authoritative."""
    state = _base_state()
    updates = _record_pr_artefacts(
        state,
        {"head_sha": "explicit0", "head_after": "fallback1"},
    )
    assert updates["current_card"]["head_after"] == "explicit0"


def test_pr_number_derived_from_pr_url() -> None:
    """The performer wire never carries pr_number; the write-through MUST
    derive it from pr_url so restart recovery and divergence detection get
    the full artefact set."""
    state = _base_state()
    updates = _record_pr_artefacts(
        state,
        {"pr_url": "https://github.com/x/y/pull/148", "pr_node_id": "PR_148"},
    )
    assert updates["current_card"]["pr_number"] == 148
