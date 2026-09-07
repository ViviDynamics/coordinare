"""Milestone planning for the implementer workflow (spec 167).

Derives milestones from the architect's implementation brief (spec 165)
or creates a single milestone from the card's acceptance criteria.

Lane selection (FR-020): work_kind from brief maps to lane per spec.
"""
from __future__ import annotations

import structlog

from performer.workflows.implementer.models import Lane, LaneSource, MilestonePlan

log = structlog.get_logger(__name__)

__all__ = ["build_plan", "select_lane"]


_LANES = ("feature", "bug", "chore", "refactor", "tests")


def select_lane(work_kind: str | None, workflow_env: dict | None = None) -> tuple[Lane, LaneSource]:
    """Select lane from work_kind (spec 167 FR-020).

    Maps:
    - feature -> feature
    - bug -> bug
    - chore -> chore
    - refactor -> refactor (run like a chore, labelled refactor)
    - tests -> tests
    - research/docs -> raise LaneNotForImplementer

    Args:
        work_kind: From brief's work_kind, or None.
        workflow_env: Role config with optional default kind.

    Returns:
        Tuple of (lane, source) where source is "brief", "default", or "unknown".

    Raises:
        LaneNotForImplementer: If work_kind is research or docs.
    """
    if work_kind in ("research", "docs"):
        raise LaneNotForImplementer(f"work kind '{work_kind}' ends at the architect (spec 168)")

    if work_kind in _LANES:
        return work_kind, "brief"  # refactor keeps its own label; the driver runs it as a chore

    env = workflow_env or {}
    default = env.get("IMPL_DEFAULT_KIND") or env.get("impl_default_kind")
    if default in _LANES:
        return default, "default"

    return "feature", "unknown"


class LaneNotForImplementer(Exception):
    """Raised when a work_kind should not reach the implementer stage."""
    pass


def _criteria_text(score) -> str:
    """The card's acceptance criteria as one bounded done-when string."""
    raw = getattr(score, "acceptance_criteria", None)
    if isinstance(raw, (list, tuple)):
        text = "; ".join(str(c).strip() for c in raw if str(c).strip())
    else:
        text = str(raw or "").strip()
    if not text:
        text = str(getattr(score, "description", "") or "").strip()
    return text[:256]


def build_plan(score, brief: dict | None) -> list[MilestonePlan]:
    """Build milestone plan from brief or card (FR-003, FR-020).

    Args:
        score: The dispatch payload carrying the card details.
        brief: The implementation brief from the architect, or None.

    Returns:
        List of MilestonePlan with lane selection per FR-020.

        - If brief exists and has milestones, return those in order.
        - If brief exists but has implementer_single_turn=true,
          collapse multiple milestones into one.
        - If no brief, return one milestone with card acceptance criteria.
        - Lane is selected from brief's work_kind (feature, bug, chore, refactor, tests).
    """
    workflow_env = score.workflow_env if hasattr(score, "workflow_env") else None

    if not brief:
        log.info("plan.no_brief", card=score.issue_number if hasattr(score, "issue_number") else "unknown")
        lane, lane_source = select_lane(None, workflow_env)
        acceptance_criteria = _criteria_text(score)

        return [
            MilestonePlan(
                index=0,
                goal="Implement acceptance criteria",
                scope=".",
                done_when=acceptance_criteria or "Acceptance criteria met",
                lane=lane,
                lane_source=lane_source,
            )
        ]

    work_kind = brief.get("work_kind")
    try:
        lane, lane_source = select_lane(work_kind, workflow_env)
    except LaneNotForImplementer as e:
        log.error("plan.lane_not_for_implementer", reason=str(e))
        raise

    milestones_raw = brief.get("milestones", [])
    if not milestones_raw:
        log.info("plan.empty_brief")
        acceptance_criteria = _criteria_text(score)
        return [
            MilestonePlan(
                index=0,
                goal="Implement acceptance criteria",
                scope=".",
                done_when=acceptance_criteria or "Acceptance criteria met",
                lane=lane,
                lane_source=lane_source,
            )
        ]

    collapse = brief.get("implementer_single_turn", False)

    milestones = []
    for i, m in enumerate(milestones_raw):
        milestones.append(
            MilestonePlan(
                index=i,
                goal=str(m.get("goal", ""))[:256],
                scope=str(m.get("scope", "."))[:256],
                done_when=str(m.get("done_when", ""))[:256],
                lane=lane,
                lane_source=lane_source,
            )
        )

    if collapse and len(milestones) > 1:
        log.info("plan.collapsing", count=len(milestones))
        combined_scope = " ".join([m.scope for m in milestones])
        combined_done = "\n".join([m.done_when for m in milestones])
        return [
            MilestonePlan(
                index=0,
                goal="Implement all milestones",
                scope=combined_scope[:256],
                done_when=combined_done[:256],
                lane=lane,
                lane_source=lane_source,
            )
        ]

    log.info("plan.built", count=len(milestones), lane=lane, lane_source=lane_source)
    return milestones
