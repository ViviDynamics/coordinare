"""Spec 125 — verdict recording rules (contract R1-R4) + override forcing flag.

Verdicts are recorded in monitor_performer's terminal-success handling: one
StageVerdict-shaped slot per verdict stage, keyed to the performer-reported
settled head. Implementing/assessing never record; marker/stage mismatches
never record; skips (persona-scope, override) never record.

Contract: specs/125-stage-verdict-memory/contracts/skip-decision.md
"""
from __future__ import annotations

from coordinare.graph.nodes.monitor_performer import (
    EXPECTED_STAGE_MARKER,
    VERDICT_STAGES,
    _apply_pending_override,
    _record_stage_verdict,
)
from coordinare.graph.state import initial_state


def _state(stage: str) -> dict:
    state = initial_state()
    state["performer_stage"] = stage
    state["current_card"] = {"id": "CARD_1"}
    state["lifecycle_sequence"] = [
        "assessing", "implementing", "reviewing", "security", "qa",
        "documenting", "closing_review",
    ]
    return state


# ---------------------------------------------------------------------------
# R1 — matching passing marker + resolvable head records the slot
# ---------------------------------------------------------------------------


def test_reviewing_approved_records_head_after() -> None:
    state = _state("reviewing")
    _record_stage_verdict(state, "approved", {"head_after": "abc123"})

    slot = state["stage_verdicts"]["reviewing"]
    assert slot["head_sha"] == "abc123"
    assert slot["verdict"] == "approved"
    assert slot["recorded_at"]  # observability only — never asserted further


def test_all_verdict_stages_record_their_matching_marker() -> None:
    for stage in VERDICT_STAGES:
        for marker in EXPECTED_STAGE_MARKER[stage]:
            state = _state(stage)
            _record_stage_verdict(state, marker, {"head_after": "abc123"})
            assert state["stage_verdicts"][stage]["verdict"] == marker


def test_head_sha_fallback_when_head_after_missing() -> None:
    state = _state("qa")
    _record_stage_verdict(state, "qa_passed", {"head_sha": "def456"})
    assert state["stage_verdicts"]["qa"]["head_sha"] == "def456"


def test_new_verdict_overwrites_prior_slot() -> None:
    state = _state("reviewing")
    _record_stage_verdict(state, "approved", {"head_after": "old000"})
    _record_stage_verdict(state, "approved", {"head_after": "new111"})
    assert state["stage_verdicts"]["reviewing"]["head_sha"] == "new111"


# ---------------------------------------------------------------------------
# R2 — no resolvable head: no record
# ---------------------------------------------------------------------------


def test_no_resolvable_head_records_nothing() -> None:
    state = _state("reviewing")
    _record_stage_verdict(state, "approved", {})
    _record_stage_verdict(state, "approved", {"head_after": "", "head_sha": None})
    assert state["stage_verdicts"] == {}


# ---------------------------------------------------------------------------
# R3 — non-verdict stages and marker mismatches: no record
# ---------------------------------------------------------------------------


def test_implementing_and_assessing_never_record() -> None:
    for stage, marker in (
        ("implementing", "pr_opened"),
        ("assessing", "assessment_complete"),
    ):
        state = _state(stage)
        _record_stage_verdict(state, marker, {"head_after": "abc123"})
        assert state["stage_verdicts"] == {}


def test_marker_stage_mismatch_never_records() -> None:
    # An "approved" marker arriving while the stage is qa must not mint a
    # qa slot (cross-wired response protection).
    state = _state("qa")
    _record_stage_verdict(state, "approved", {"head_after": "abc123"})
    assert state["stage_verdicts"] == {}


def test_failure_markers_never_record() -> None:
    state = _state("qa")
    _record_stage_verdict(state, "qa_failed", {"head_after": "abc123"})
    assert state["stage_verdicts"] == {}


# ---------------------------------------------------------------------------
# Override forcing flag (US1 scenario 4 / decision row V1)
# ---------------------------------------------------------------------------


def test_override_restart_sets_forced_dispatch_flag() -> None:
    state = _state("reviewing")
    state["pending_override"] = {"action": "restart", "target_stage": "reviewing"}

    result = _apply_pending_override(state)

    assert result is not None
    assert result["override_forced_dispatch"] == "reviewing"
    assert result["performer_stage"] == "reviewing"


def test_override_skip_and_veto_do_not_set_forced_dispatch() -> None:
    for action in ("skip", "veto"):
        state = _state("reviewing")
        state["pending_override"] = {"action": action, "target_stage": "reviewing"}
        result = _apply_pending_override(state)
        assert result is not None
        assert not result.get("override_forced_dispatch")


def test_override_restart_invalid_stage_does_not_set_flag() -> None:
    state = _state("reviewing")
    state["pending_override"] = {"action": "restart", "target_stage": "nonsense"}
    result = _apply_pending_override(state)
    assert result is not None
    assert not result.get("override_forced_dispatch")
