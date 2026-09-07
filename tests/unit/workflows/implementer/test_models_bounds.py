"""Test models for implementer workflow with boundary checks (T004).

Tests per data-model.md: TurnBrief, TurnResult, Baseline, MilestonePlan,
PerTurnAttempt, PerMilestoneRecord, QualityAttempt, CIAttempt, RunRecord
with bounds, validators, and extra="forbid".
"""
from __future__ import annotations

import pytest
from performer.workflows.implementer.models import (
    Baseline,
    RunRecord,
    TurnBrief,
    TurnResult,
)
from pydantic import ValidationError


def _brief(**over):
    base = dict(
        kind="tests", persona_kind="TESTS", persona="Write only test files.", milestone_index=0,
        milestone_goal="Add login endpoint", scope_paths=["app/auth/", "spec/requests/"],
        done_when="request specs pass", forbidden_paths=["docs/"],
    )
    base.update(over)
    return TurnBrief(**base)


class TestTurnBrief:
    """TurnBrief bounds per data-model.md and contracts/turn-brief.schema.json."""

    def test_valid_turn_brief(self):
        brief = _brief()
        assert brief.kind == "tests" and brief.persona_kind == "TESTS"
        assert brief.failing_tests == [] and brief.failure_excerpt is None

    def test_milestone_goal_max_length(self):
        _brief(milestone_goal="x" * 256)
        with pytest.raises(ValidationError):
            _brief(milestone_goal="x" * 257)

    def test_scope_paths_bounded(self):
        _brief(scope_paths=["p"] * 20)
        with pytest.raises(ValidationError):
            _brief(scope_paths=["p"] * 21)
        with pytest.raises(ValidationError):
            _brief(scope_paths=["x" * 257])

    def test_done_when_max_length(self):
        _brief(done_when="x" * 256)
        with pytest.raises(ValidationError):
            _brief(done_when="x" * 257)

    def test_forbidden_paths_max_items(self):
        _brief(forbidden_paths=["p"] * 10)
        with pytest.raises(ValidationError):
            _brief(forbidden_paths=["p"] * 11)

    def test_failing_tests_and_excerpt_bounded(self):
        _brief(failing_tests=["t"] * 50, failure_excerpt="x" * 8000)
        with pytest.raises(ValidationError):
            _brief(failing_tests=["t"] * 51)
        with pytest.raises(ValidationError):
            _brief(failure_excerpt="x" * 8001)

    def test_empty_milestone_goal_and_persona_rejected(self):
        with pytest.raises(ValidationError):
            _brief(milestone_goal="")
        with pytest.raises(ValidationError):
            _brief(persona="")

    @pytest.mark.parametrize("bad", [{"kind": "refactor"}, {"persona_kind": "tests"}])
    def test_kinds_are_closed_sets(self, bad):
        with pytest.raises(ValidationError):
            _brief(**bad)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            _brief(dependencies=[])


class TestTurnResult:
    def test_exit_states_match_the_runner_contract(self):
        for state in ("done", "timeout", "error"):
            TurnResult(exit_state=state, output_tail="", changed_paths=[], wall_ms=1, harness_commits=[])
        with pytest.raises(ValidationError):
            TurnResult(exit_state="success", output_tail="", changed_paths=[], wall_ms=1, harness_commits=[])


class TestBaseline:
    """Test Baseline model validation."""

    def test_baseline_with_test_names(self):
        """Baseline with test_names (and no pass_count) is valid."""
        baseline = Baseline(
            test_names=["test_login", "test_signup"],
            pass_count=None,
            fail_count=0,
            stack="python",
            detected_from="pyproject.toml",
        )
        assert baseline.test_names == ["test_login", "test_signup"]
        assert baseline.pass_count is None

    def test_baseline_with_pass_count(self):
        """Baseline with pass_count (and no test_names) is valid."""
        baseline = Baseline(
            test_names=None,
            pass_count=5,
            fail_count=0,
            stack="python",
            detected_from="pyproject.toml",
        )
        assert baseline.pass_count == 5
        assert baseline.test_names is None


class TestRunRecord:
    """Test RunRecord model structure."""

    def test_run_record_required_fields(self):
        """RunRecord requires all top-level fields per contract."""
        record = RunRecord(
            status="pr_opened",
            reason="all checks green",
            milestones_planned=1,
            milestones_completed=1,
            per_milestone=[],
            quality_attempts=[],
            ci_attempts=[],
            scope_reverts=[],
            phase_durations_ms={},
            total_duration_ms=5000,
        )
        assert record.status == "pr_opened"
        assert record.milestones_planned == 1

    def test_run_record_schema_keys(self):
        """RunRecord.model_json_schema() has same required keys as contract."""
        schema = RunRecord.model_json_schema()
        required = set(schema.get("required", []))

        # Top-level required fields from contracts/run-record.schema.json
        expected = {
            "status",
            "reason",
            "milestones_planned",
            "milestones_completed",
            "per_milestone",
            "quality_attempts",
            "ci_attempts",
            "scope_reverts",
            "phase_durations_ms",
            "total_duration_ms",
        }

        assert expected.issubset(required), (
            f"Missing required fields: {expected - required}"
        )
