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


_LANES = ("feature", "bug", "chore", "refactor", "tests", "docs", "config", "dependency")


def select_lane(work_kind: str | None, workflow_env: dict | None = None) -> tuple[Lane, LaneSource]:
    """Select lane from work_kind (spec 167 FR-020, 410).

    Maps:
    - feature -> feature
    - bug -> bug
    - chore -> chore
    - refactor -> refactor (run like a chore, labelled refactor)
    - tests -> tests
    - docs -> docs (writes the documentation itself)
    - config/dependency -> chore-shaped: one change turn, no red/green
    - research -> raise LaneNotForImplementer

    Args:
        work_kind: From brief's work_kind, or None.
        workflow_env: Role config with optional default kind.

    Returns:
        Tuple of (lane, source) where source is "brief", "default", or "unknown".

    Raises:
        LaneNotForImplementer: If work_kind is research.
    """
    if work_kind == "research":
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


_LABEL_LANES: tuple[tuple[str, Lane], ...] = (
    ("docs", "docs"),
    ("documentation", "docs"),
    ("dependency", "dependency"),
    ("dependencies", "dependency"),
    ("config", "config"),
    ("ci", "config"),
    ("infrastructure", "config"),
    ("chore", "chore"),
)


def _lane_from_labels(labels: list[str]) -> tuple[Lane, LaneSource] | None:
    """Infer the lane from card labels when no brief names a work_kind (410).

    First matching label wins; unknown labels say nothing.
    """
    for label in labels:
        for prefix, lane in _LABEL_LANES:
            if label == prefix:
                return lane, "labels"
    return None


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


def review_findings(score) -> list[dict]:
    """The reviewer's surviving findings on the dispatch payload (spec 169), or []."""
    record = getattr(score, "review_findings", None)
    if not isinstance(record, dict):
        return []
    return [f for f in (record.get("findings") or []) if isinstance(f, dict)]


def _safe_repo_path(path: str) -> bool:
    """A repair group path stays inside the repository: relative, no parent segments."""
    return bool(path) and not path.startswith("/") and ".." not in path.split("/") and "\\" not in path


def review_findings_for(score, path: str) -> list[dict]:
    return [f for f in review_findings(score) if str(f.get("path")) == path]


def repair_plan(score) -> list[MilestonePlan]:
    """Spec 169 FR-013: one repair milestone per file group of review findings, in diff order.

    Findings without a file anchor (prior comments on the PR body) ride with
    the first group so the turn sees them; they cannot form a group of their own.
    """
    findings = review_findings(score)
    if not findings:
        return []
    groups: dict[str, list[dict]] = {}
    for f in findings:
        path = str(f.get("path") or "")
        if not _safe_repo_path(path):
            continue  # body-anchored findings ride with the first group; traversal paths never form one
        groups.setdefault(path, []).append(f)
    plans = []
    for index, (path, group) in enumerate(groups.items()):
        cats = ", ".join(sorted({str(f.get("category")) for f in group}))
        plans.append(MilestonePlan(
            index=index, goal=f"Address {len(group)} review finding(s) in {path}"[:256], scope=path[:256],
            done_when=f"the findings ({cats}) are fixed and the tests pass"[:256], lane="repair", lane_source="review",
        ))
    return plans


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
          When the brief names no work_kind -- the Blueprint schema has no
          work_kind field, so a structured architect brief cannot carry one --
          the card's labels are consulted next, then the role default.
    """
    workflow_env = score.workflow_env if hasattr(score, "workflow_env") else None

    repair = repair_plan(score)
    if repair:
        log.info("plan.repair_lane", groups=len(repair), findings=len(review_findings(score)))
        return repair

    if not brief:
        log.info("plan.no_brief", card=score.issue_number if hasattr(score, "issue_number") else "unknown")
        labels = [str(label).strip().lower() for label in (getattr(score, "labels", None) or [])]
        inferred = _lane_from_labels(labels)
        if inferred:
            lane, lane_source = inferred
        else:
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
            ),
        ]

    work_kind = brief.get("work_kind")
    try:
        lane, lane_source = select_lane(work_kind, workflow_env)
    except LaneNotForImplementer as e:
        log.error("plan.lane_not_for_implementer", reason=str(e))
        raise
    if lane_source != "brief":
        # The brief is silent on the kind of work (the usual case: the
        # Blueprint schema carries no work_kind). Card labels are the next
        # witness, and only then the role default.
        inferred = _lane_from_labels([str(label).strip().lower() for label in (getattr(score, "labels", None) or [])])
        if inferred:
            lane, lane_source = inferred

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
            ),
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
            ),
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
            ),
        ]

    log.info("plan.built", count=len(milestones), lane=lane, lane_source=lane_source)
    return milestones
