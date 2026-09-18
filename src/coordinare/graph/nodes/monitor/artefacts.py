"""PR artefact write-through and findings lift for the monitor (435, 076, 169, 170)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def _record_pr_artefacts(
    state: CoordinareState,
    status: dict[str, Any] | None,
) -> dict[str, Any]:
    """076 (T073) FR-015 / FR-016: write through any new PR identifiers
    reported by the performer.

    Two distinct write paths:
    1. RETURNS ``{"current_card": <merged card dict>}`` for the caller
       (``_advance_stage`` or the DONE branch) to merge into the state
       update dict it returns.  That merge updates the flat
       ``state["current_card"]`` (which mirrors ``state["active_card"]``
       in the same-cycle view).  Empty dict returned if no artefact
       fields were present.
    2. SIDE-EFFECT: directly mutates
       ``state["active_sessions"][card_id]["current_card"]`` AND
       ``state["active_sessions"][card_id]["pr_artefacts_recorded_at"]``
       so the persisted-snapshot view is updated immediately and a
       daemon restart loads the new PR identifiers.

    No-ops on unparseable / missing status.  Logs
    ``monitor_performer.pr_artefacts_recorded`` when any field is
    actually written so operators can grep for the write-through.
    """
    if status is None:
        return {}
    pr_url = status.get("pr_url")
    pr_node_id = status.get("pr_node_id")
    pr_number = status.get("pr_number")
    head_sha = status.get("head_sha")
    pushed_branch = status.get("pushed_branch")
    plan_path = status.get("plan_path")

    if not any((pr_url, pr_node_id, pr_number, head_sha, pushed_branch, plan_path)):
        return {}

    updates: dict[str, Any] = {}
    card = dict(state.get("current_card") or {})
    if pr_url:
        card["pr_url"] = pr_url
    if pr_node_id:
        card["pr_node_id"] = pr_node_id
    if pr_number is not None:
        card["pr_number"] = pr_number
    if head_sha:
        card["head_after"] = head_sha
    if pushed_branch:
        card["pushed_branch"] = pushed_branch
    if plan_path:
        card["plan_path"] = plan_path
    updates["current_card"] = card

    # Mirror to active_sessions[card_id] so a daemon restart loads the
    # new PR identifiers from snapshot, not the stale ones.
    card_id = str(card.get("id", ""))
    if card_id:
        sessions = state.get("active_sessions")
        if isinstance(sessions, dict) and card_id in sessions and isinstance(sessions[card_id], dict):
            sessions[card_id]["current_card"] = card
            sessions[card_id]["pr_artefacts_recorded_at"] = datetime.now(UTC)

    logger.info(
        "monitor_performer.pr_artefacts_recorded",
        card_id=card_id,
        pr_url=pr_url,
        pr_node_id=pr_node_id,
        pr_number=pr_number,
        head_sha=head_sha,
        pushed_branch=pushed_branch,
    )
    return updates


def _lift_review_findings(state: dict[str, Any], report: dict[str, Any], stage: str) -> None:
    """169 (T038): lift reviewer findings into state when reviewer reports changes_requested.

    Findings are cleared on reviewer re-dispatch and injected into implementing stage only.
    Report structure: {"review": {...}, "workflow_metrics": {...}}. The "review" key
    carries the complete ReviewRecord (changed_files, findings, dispositions, coverage,
    verdict, post result).

    Mutates state in place.
    """
    if stage != "reviewing":
        return

    _review_report = report if isinstance(report, dict) else {}
    _review = _review_report.get("review")

    if isinstance(_review, dict) and isinstance(_review.get("changed_files"), list) and isinstance(_review.get("verdict"), str):
        # Deep copy to avoid references to mutable structures
        import copy
        state["review_findings"] = copy.deepcopy(_review)
        categories = set()
        for finding in _review.get("findings", []):
            if isinstance(finding, dict):
                categories.add(finding.get("category", "unknown"))
        logger.info(
            "review_findings.lifted",
            card_id=state.get("current_card", {}).get("id", "unknown"),
            count=len(_review.get("findings", [])),
            categories=list(categories),
            verdict=_review.get("verdict"),
        )
    elif isinstance(_review, dict):
        logger.info(
            "review_findings.not_lifted",
            card_id=state.get("current_card", {}).get("id", "unknown"),
            has_review=isinstance(_review, dict),
            missing_fields=not (isinstance(_review.get("changed_files"), list) and isinstance(_review.get("verdict"), str)),
        )


def _lift_security_findings(state: dict[str, Any], report: dict[str, Any], target_stage: str) -> None:
    """170: lift security workflow findings into state when routed to implementer.

    Report structure: {"security": {...}, "workflow_metrics": {...}}. The "security" key
    carries the complete SecurityRecord (changed_files, findings, verdict, etc).
    Only lifts when target_stage is implementing and findings route to implementer.

    Mutates state in place.
    """
    if target_stage != "implementing":
        return

    _security_report = report if isinstance(report, dict) else {}
    sec = _security_report.get("security")

    if not isinstance(sec, dict):
        return

    # Build the findings record with implementer-routed findings only
    blocking = sec.get("blocking") or []
    implementer_findings = [
        {
            "path": f.get("path", ""),
            "line": f.get("line", 0),
            "category": f.get("category", ""),
            "problem": f.get("problem", ""),
            "why_blocking": f.get("why_blocking", ""),
            "evidence": f.get("evidence", ""),
            "origin": f.get("origin", "model"),
        }
        for f in blocking
        if isinstance(f, dict) and f.get("routing", "implementer") == "implementer"
    ]

    if implementer_findings:
        import copy
        record = {
            "changed_files": sec.get("changed_files") or [],
            "diff_truncated": bool(sec.get("diff_truncated")),
            "verdict": "changes_requested",
            "covered_files": sec.get("covered_files") or [],
            "findings": implementer_findings,
        }
        state["review_findings"] = copy.deepcopy(record)
        categories = {f.get("category", "unknown") for f in implementer_findings}
        logger.info(
            "review_findings.lifted",
            card_id=state.get("current_card", {}).get("id", "unknown"),
            source="security",
            count=len(implementer_findings),
            categories=list(categories),
        )


def _parse_job_id_from_details_url(url: str) -> int | None:
    """Extract the Actions job_id from a check ``details_url``.

    GitHub Actions check details URLs look like
    ``https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id}``.
    The logs endpoint is per-job, so we need ``job_id``. Returns ``None`` for
    non-Actions URLs (third-party CI), empty input, or malformed paths.
    """
    if not url:
        return None
    try:
        parts = urlparse(url).path.strip("/").split("/")
        if "actions" in parts and "job" in parts:
            idx = parts.index("job")
            if idx + 1 < len(parts):
                return int(parts[idx + 1])
    except ValueError:
        return None
    return None


def _pr_url_parts(pr_url: str | None) -> tuple[str, str, int] | None:
    """Parse a PR URL into (owner, repo, pr_number). Returns None on failure."""
    if not pr_url:
        return None
    try:
        cleaned = pr_url.rstrip("/")
        parts = cleaned.split("/")
        if parts[-2] != "pull":
            return None
        pr_num = int(parts[-1])
        owner = parts[-4]
        repo = parts[-3]
    except (ValueError, IndexError):
        return None
    return owner, repo, pr_num

