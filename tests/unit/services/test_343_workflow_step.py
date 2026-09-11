"""Latching which workflow step a performer is in (issue #343).

Every spec-164 workflow already emits a named, non-delta ``progress`` event at
each step boundary. Coordinare latches those transitions into per-card session
state at the moment it observes them, which is what makes the answer durable:
captured before the activity feed can evict the marker, persisted across a
daemon restart, and still readable after the performer job is reaped.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from coordinare.services.workflow_step import (
    MAX_STEP_TRAIL,
    is_step_event,
    latch_workflow_step,
)

OBSERVED = datetime(2026, 9, 11, 1, 30, 0, tzinfo=UTC)


def _step(text: str, ts: str | None = "2026-09-11T01:16:39Z") -> dict:
    return {"type": "progress", "text": text, "is_delta": False, "timestamp": ts}


class TestRecognisingAStepMarker:
    def test_named_step_of_a_known_role(self) -> None:
        assert is_step_event(_step("implementer.baseline"))
        assert is_step_event(_step("architect.blueprint"))
        assert is_step_event(_step("env_bootstrap.verify"))

    def test_streamed_model_output_is_never_a_step(self) -> None:
        """Deltas are the overwhelming majority of traffic and never boundaries."""
        ev = _step("implementer.baseline")
        ev["is_delta"] = True
        assert not is_step_event(ev)

    def test_non_progress_events_are_not_steps(self) -> None:
        ev = _step("implementer.baseline")
        ev["type"] = "tool_use"
        assert not is_step_event(ev)

    def test_prose_containing_a_dot_is_not_a_step(self) -> None:
        """Shape alone is not selective enough; the role prefix must match."""
        assert not is_step_event(_step("main.py"))
        assert not is_step_event(_step("spec.md"))
        assert not is_step_event(_step("Reading config.yaml now"))
        assert not is_step_event(_step("unknown_role.something"))

    def test_malformed_events_do_not_raise(self) -> None:
        assert not is_step_event(None)
        assert not is_step_event("implementer.baseline")
        assert not is_step_event({"type": "progress"})
        assert not is_step_event({"type": "progress", "text": 42})


class TestLatching:
    def test_first_step_sets_name_and_entry_time(self) -> None:
        state: dict = {}
        assert latch_workflow_step(state, [_step("implementer.intake")], observed_at=OBSERVED)
        assert state["workflow_step"] == "implementer.intake"
        assert state["workflow_step_entered_at"] == datetime(2026, 9, 11, 1, 16, 39, tzinfo=UTC)

    def test_the_latest_step_in_a_batch_wins(self) -> None:
        state: dict = {}
        latch_workflow_step(
            state,
            [_step("implementer.intake"), _step("implementer.plan"), _step("implementer.baseline")],
            observed_at=OBSERVED,
        )
        assert state["workflow_step"] == "implementer.baseline"

    def test_re_reporting_the_same_step_does_not_reset_the_timer(self) -> None:
        """The decisive behaviour.

        Backends re-report their whole event list every poll. If re-seeing the
        current step re-stamped its entry time, a wedged step would look
        permanently fresh -- which would defeat the entire point of showing how
        long it has been there.
        """
        state: dict = {}
        latch_workflow_step(state, [_step("implementer.baseline")], observed_at=OBSERVED)
        first = state["workflow_step_entered_at"]

        later = OBSERVED + timedelta(minutes=30)
        moved = latch_workflow_step(
            state, [_step("implementer.baseline", ts=None)], observed_at=later
        )

        assert moved is False
        assert state["workflow_step_entered_at"] == first

    def test_a_real_transition_moves_the_timer(self) -> None:
        state: dict = {}
        latch_workflow_step(state, [_step("implementer.baseline")], observed_at=OBSERVED)
        later = OBSERVED + timedelta(minutes=30)
        assert latch_workflow_step(state, [_step("implementer.quality", ts=None)], observed_at=later)
        assert state["workflow_step"] == "implementer.quality"
        assert state["workflow_step_entered_at"] == later

    def test_a_batch_with_no_steps_leaves_state_untouched(self) -> None:
        state = {"workflow_step": "implementer.baseline"}
        deltas = [{"type": "progress", "text": "thinking...", "is_delta": True}]
        assert latch_workflow_step(state, deltas, observed_at=OBSERVED) is False
        assert state["workflow_step"] == "implementer.baseline"


class TestEntryTime:
    def test_performer_timestamp_is_preferred(self) -> None:
        state: dict = {}
        latch_workflow_step(
            state, [_step("architect.survey", ts="2026-09-11T00:00:00Z")], observed_at=OBSERVED
        )
        assert state["workflow_step_entered_at"] == datetime(2026, 9, 11, 0, 0, 0, tzinfo=UTC)

    def test_missing_timestamp_falls_back_to_observation(self) -> None:
        state: dict = {}
        latch_workflow_step(state, [_step("architect.survey", ts=None)], observed_at=OBSERVED)
        assert state["workflow_step_entered_at"] == OBSERVED

    def test_unparseable_timestamp_falls_back_rather_than_raising(self) -> None:
        state: dict = {}
        latch_workflow_step(state, [_step("architect.survey", ts="not-a-date")], observed_at=OBSERVED)
        assert state["workflow_step_entered_at"] == OBSERVED

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        """A naive stamp compared against an aware now() raises at render time."""
        state: dict = {}
        latch_workflow_step(
            state, [_step("architect.survey", ts="2026-09-11T00:00:00")], observed_at=OBSERVED
        )
        entered = state["workflow_step_entered_at"]
        assert entered.tzinfo is not None
        assert (OBSERVED - entered).total_seconds() > 0


class TestTrail:
    def test_trail_records_transitions_in_order(self) -> None:
        state: dict = {}
        for name in ("implementer.intake", "implementer.plan", "implementer.baseline"):
            latch_workflow_step(state, [_step(name)], observed_at=OBSERVED)
        assert [e["step"] for e in state["workflow_step_trail"]] == [
            "implementer.intake",
            "implementer.plan",
            "implementer.baseline",
        ]

    def test_a_step_that_legitimately_recurs_is_recorded_again(self) -> None:
        """Implementer milestone steps repeat once per milestone."""
        state: dict = {}
        for name in ("implementer.milestone", "implementer.quality", "implementer.milestone"):
            latch_workflow_step(state, [_step(name)], observed_at=OBSERVED)
        assert [e["step"] for e in state["workflow_step_trail"]] == [
            "implementer.milestone",
            "implementer.quality",
            "implementer.milestone",
        ]

    def test_trail_is_bounded_and_keeps_the_most_recent(self) -> None:
        state: dict = {}
        for i in range(MAX_STEP_TRAIL + 10):
            latch_workflow_step(state, [_step(f"implementer.s{i}")], observed_at=OBSERVED)
        trail = state["workflow_step_trail"]
        assert len(trail) == MAX_STEP_TRAIL
        assert trail[-1]["step"] == f"implementer.s{MAX_STEP_TRAIL + 9}"

    def test_repolling_does_not_grow_the_trail(self) -> None:
        state: dict = {}
        for _ in range(10):
            latch_workflow_step(state, [_step("implementer.baseline")], observed_at=OBSERVED)
        assert len(state["workflow_step_trail"]) == 1
