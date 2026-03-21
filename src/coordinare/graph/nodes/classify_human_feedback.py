"""Classify human PR feedback and route to the appropriate performer role (019).

When a human posts comments on a PR, this node classifies each comment by
concern type and re-enters the lifecycle at the earliest affected performer
role.

FR-008: classify_human_feedback reads PR comments and resets performer_stage.
FR-009: supports implementation, architecture, security, documentation, qa, review.
FR-010: includes PR comments as relay_feedback in state.

Note: FR-011 (human approval → merging) is handled by ``monitor_pr``, which
detects APPROVED reviews and sets ``phase="merging"`` before this node runs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Concern → performer stage mapping
# ---------------------------------------------------------------------------

CONCERN_TO_STAGE: dict[str, str] = {
    "implementation": "implementing",
    "architecture": "architecting",
    "security": "security",
    "documentation": "documenting",
    "qa": "qa",
    "review": "reviewing",
}

def _earliest_stage(stages: list[str], lifecycle: list[str]) -> str:
    """Return the earliest stage from *stages* that exists in *lifecycle*.

    Uses the lifecycle sequence itself as the canonical order (avoids
    duplicating the ordering defined in ``__main__._CANONICAL_ORDER``).
    Falls back to ``"implementing"`` if none of the stages are in the lifecycle.
    """
    order = {s: i for i, s in enumerate(lifecycle)}
    candidates = [s for s in stages if s in lifecycle]
    if not candidates:
        return "implementing" if "implementing" in lifecycle else lifecycle[0]
    return min(candidates, key=lambda s: order.get(s, 999))


_CONCERN_KEYWORDS: dict[str, list[str]] = {
    "implementation": [
        "bug", "fix", "broken", "crash", "error", "logic", "implement",
        "code", "function", "method", "variable", "refactor", "typo",
    ],
    "architecture": [
        "architect", "design", "pattern", "structure", "module",
        "coupling", "dependency", "abstraction", "schema",
    ],
    "security": [
        "security", "vulnerab", "authentication", "authorization",
        "token", "secret", "inject", "xss", "csrf", "permission",
        "access control", "owasp",
    ],
    "documentation": [
        "doc", "readme", "comment", "changelog", "documentation",
        "docstring", "typo in doc",
    ],
    "qa": [
        "test", "coverage", "assertion", "fixture", "mock", "flaky",
        "regression",
    ],
    "review": [
        "style", "naming", "convention", "format", "lint", "readability",
    ],
}


def classify_feedback_concerns(reviews: list[dict[str, Any]]) -> list[str]:
    """Extract concern categories from pending reviews using keyword heuristics.

    This is the V1 implementation.  A future version may use the assessment
    backend (Claude) for AI-powered classification.  For now, keyword matching
    provides fast, deterministic routing.
    """
    concerns: set[str] = set()

    for review in reviews:
        body = str(review.get("body", "")).lower()
        comments = review.get("comments", [])
        all_text = body
        if isinstance(comments, list):
            for c in comments:
                all_text += " " + str(c.get("body", "")).lower()

        for concern, keywords in _CONCERN_KEYWORDS.items():
            if any(kw in all_text for kw in keywords):
                concerns.add(concern)

    return sorted(concerns)


async def classify_human_feedback(state: CoordinareState) -> CoordinareState:
    """Classify pending human PR reviews and route to the appropriate role.

    If the human approved the PR (detected by monitor_pr setting phase to
    "merging"), this node does nothing — the merge path handles it.

    If actionable reviews exist, classifies each by concern type and resets
    ``performer_stage`` to the earliest affected role in the lifecycle.
    """
    pending_reviews: list[dict[str, Any]] = list(state.get("pending_reviews") or [])
    lifecycle: list[str] = list(state.get("lifecycle_sequence") or ["implementing"])

    if not pending_reviews:
        # No reviews to classify — stay in monitoring_pr.
        state["phase"] = "monitoring_pr"
        return state

    # Classify concerns from review content.
    concern_names = classify_feedback_concerns(pending_reviews)
    target_stages = [
        CONCERN_TO_STAGE[c] for c in concern_names if c in CONCERN_TO_STAGE
    ]

    if not target_stages:
        # Could not classify — default to implementer.
        target_stages = ["implementing"]

    target_stage = _earliest_stage(target_stages, lifecycle)

    # If the target stage isn't in the lifecycle, fall back to implementer.
    if target_stage not in lifecycle:
        target_stage = "implementing" if "implementing" in lifecycle else lifecycle[0]

    logger.info(
        "classify_human_feedback.routing",
        concerns=concern_names,
        target_stage=target_stage,
        review_count=len(pending_reviews),
    )

    # Move card to IN_PROGRESS on the board so the next check_board cycle
    # routes to dispatching (not back to monitoring_pr).
    github = state.get("github_service")
    card = state.get("current_card")
    if github is not None and isinstance(card, dict):
        card_id = str(card.get("id", ""))
        try:
            await github.move_card(card_id, "IN_PROGRESS")
        except Exception:
            logger.warning("classify_human_feedback.move_card_failed", card_id=card_id)
        card["previous_status"] = card.get("status", "IN_REVIEW")
        card["status"] = "IN_PROGRESS"
        state["current_card"] = card

    # Store feedback for the dispatch node to relay (FR-010).
    state["relay_feedback"] = pending_reviews  # type: ignore[typeddict-unknown-key]
    state["pending_reviews"] = []
    state["performer_stage"] = target_stage
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None

    return state
