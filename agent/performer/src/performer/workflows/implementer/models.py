"""Models for the implementer workflow (spec 167).

TurnBrief, TurnResult, Baseline, MilestonePlan, PerTurnAttempt,
PerMilestoneRecord, QualityAttempt, CIAttempt, RunRecord per data-model.md.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

__all__ = [
    "TurnBrief",
    "TurnResult",
    "Baseline",
    "MilestonePlan",
    "PerTurnAttempt",
    "PerMilestoneRecord",
    "QualityAttempt",
    "CIAttempt",
    "RunRecord",
]


class _Bounded(BaseModel):
    model_config = ConfigDict(extra="forbid")


PersonaKind = Literal["TESTS", "IMPLEMENT", "REPAIR_TESTS", "REPAIR_IMPLEMENT", "REPAIR_QUALITY", "REPAIR_CI", "INVESTIGATE", "CHANGE", "REPAIR_REVIEW"]
TurnKind = Literal["tests", "implement", "repair"]
ExitState = Literal["done", "timeout", "error"]
Lane = Literal["feature", "bug", "chore", "refactor", "tests", "repair"]
LaneSource = Literal["brief", "default", "unknown", "review"]


class TurnBrief(_Bounded):
    """Input to one harness turn (FR-002).

    What one harness turn is asked to do: the kind, the persona kind whose
    template was rendered into ``persona``, one milestone's goal, scope and
    done-when, the forbidden paths, and for repairs the failing tests and
    the exact failing output. ``persona`` is the fully rendered text the
    runner hands the harness as its instructions.
    """

    kind: TurnKind
    persona_kind: PersonaKind
    persona: Annotated[str, StringConstraints(min_length=1)]
    milestone_index: int
    milestone_goal: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    scope_paths: Annotated[list[Annotated[str, StringConstraints(max_length=256)]], Field(max_length=20)]
    done_when: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    forbidden_paths: Annotated[list[str], Field(max_length=10)]
    failing_tests: Annotated[list[str], Field(max_length=50)] = []
    failure_excerpt: Annotated[str, StringConstraints(max_length=8000)] | None = None


class TurnResult(_Bounded):
    """Output from one harness turn (FR-002), as the runner reports it."""

    exit_state: ExitState
    output_tail: str
    changed_paths: list[str]
    wall_ms: int
    harness_commits: list[str]


class Baseline(_Bounded):
    """Passing test set before any milestone work (FR-004)."""

    test_names: list[str] | None = None
    pass_count: int | None = None
    fail_count: int | None = None
    stack: str
    detected_from: str


class MilestonePlan(_Bounded):
    """One milestone in the workflow (FR-003, FR-020-023).

    Lane selection (FR-020): feature/bug/chore/refactor/tests from work_kind.
    Lane source records whether the lane came from the architect's brief,
    a role default, or was unknown/defaulted.
    """

    index: int
    goal: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    scope: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    done_when: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    lane: Lane = "feature"
    lane_source: LaneSource = "unknown"


class PerTurnAttempt(_Bounded):
    """One attempt at a turn step (FR-018)."""

    kind: TurnKind
    milestone_index: int
    attempt_number: int
    exit_state: ExitState
    wall_ms: int
    files_changed: int
    has_out_of_scope_reverts: bool
    failure_reason: str | None = None


class PerMilestoneRecord(_Bounded):
    """Workflow record for one milestone (FR-018)."""

    index: int
    goal: str
    done_when: str
    tests_attempt: PerTurnAttempt | None = None
    tests_reprompt: PerTurnAttempt | None = None
    implement_attempts: list[PerTurnAttempt] = Field(default_factory=list)
    implementation_successful: bool
    failure_reason: str | None = None


class QualityAttempt(_Bounded):
    """Record of one quality command or lint run (FR-010)."""

    command: str
    attempt_number: int
    exit_code: int
    output_tail: str
    wall_time_ms: int
    passed: bool


class CIAttempt(_Bounded):
    """Record of one CI polling and repair cycle (FR-013)."""

    attempt_number: int
    failing_checks: list[str]
    repair_needed: bool
    wall_time_ms: int
    log_excerpt: str = ""


class RunRecord(_Bounded):
    """Complete workflow run history (FR-018).

    Travels in PerformerResponse.report; not persisted by coordinare.
    """

    status: Literal["pr_opened", "changes_requested", "partial_progress", "env_blocked"]
    reason: str
    milestones_planned: int
    milestones_completed: int
    next_focus_milestone: str | None = None
    per_milestone: list[PerMilestoneRecord]
    quality_attempts: list[QualityAttempt]
    ci_attempts: list[CIAttempt]
    scope_reverts: list[dict[str, str]]
    phase_durations_ms: dict[str, int]
    total_duration_ms: int
    turn_count: int = 0
    model_calls: int = 0
    github_api_calls: int = 0
