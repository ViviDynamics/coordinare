"""Report assembly for the implementer workflow (spec 167).

Builds the RunRecord from workflow state, milestones, quality/CI attempts,
and phase durations. The report travels in PerformerResponse.report.
"""
from __future__ import annotations

from typing import Any

import structlog

from performer.workflows.implementer.models import RunRecord

log = structlog.get_logger(__name__)

__all__ = ["assemble_run_record"]


def assemble_run_record(
    status: str,
    reason: str,
    milestones_planned: int,
    milestones_completed: int,
    next_focus_milestone: str | None,
    per_milestone: list[Any],
    quality_attempts: list[Any],
    ci_attempts: list[Any],
    scope_reverts: list[dict[str, str]],
    phase_durations_ms: dict[str, int],
    total_duration_ms: int,
    resumed_from_milestone: int | None = None,
    turn_count: int = 0,
    model_calls: int = 0,
    github_api_calls: int = 0,
    work_salvaged: bool = False,
) -> RunRecord:
    """Assemble a complete RunRecord for the workflow (FR-018).

    Args:
        status: Terminal status ("pr_opened", "partial_progress", "env_blocked", "changes_requested").
        reason: Human-readable reason for the status.
        milestones_planned: Total milestones in the plan.
        milestones_completed: Milestones that completed successfully.
        next_focus_milestone: If partial_progress, the failing milestone goal.
        resumed_from_milestone: 171: index of the first milestone this run ran,
            when earlier ones were skipped as already done by a previous run.
        per_milestone: List of PerMilestoneRecord for each milestone.
        quality_attempts: List of QualityAttempt records.
        ci_attempts: List of CIAttempt records.
        scope_reverts: List of scope violation records.
        phase_durations_ms: Dict of phase -> duration_ms.
        total_duration_ms: Total workflow duration.
        turn_count: Total turn attempts.
        model_calls: Model invocations (if tracked).
        github_api_calls: GitHub API calls.

    Returns:
        RunRecord ready for PerformerResponse.report["implementer_run"].
    """
    record = RunRecord(
        status=status,
        reason=reason,
        milestones_planned=milestones_planned,
        milestones_completed=milestones_completed,
        resumed_from_milestone=resumed_from_milestone,
        next_focus_milestone=next_focus_milestone,
        per_milestone=per_milestone,
        quality_attempts=quality_attempts,
        ci_attempts=ci_attempts,
        scope_reverts=scope_reverts,
        phase_durations_ms=phase_durations_ms,
        total_duration_ms=total_duration_ms,
        turn_count=turn_count,
        model_calls=model_calls,
        github_api_calls=github_api_calls,
        work_salvaged=work_salvaged,
    )

    log.info(
        "report.assembled",
        status=status,
        milestones=f"{milestones_completed}/{milestones_planned}",
        resumed_from=resumed_from_milestone,
        total_ms=total_duration_ms,
        turns=turn_count,
    )

    return record
