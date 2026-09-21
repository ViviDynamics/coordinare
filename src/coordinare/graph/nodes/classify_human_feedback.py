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

import re
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.state import _set_current_card
from coordinare.services.board_provider import board_of, move_card_or_warn

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 031 — /coordinare command parsing for human overrides
# ---------------------------------------------------------------------------

_COMMAND_RE = re.compile(
    r"/coordinare\s+(skip-\w+|restart-from\s+\w+|veto)",
    re.IGNORECASE,
)

def _resolve_stage(name: str, lifecycle: list[str]) -> str:
    """Resolve a role noun or stage name to a lifecycle stage.

    Accepts both role nouns (e.g. ``architect``) and stage names
    (e.g. ``architecting``).  Returns the original name if it's already
    a valid stage, or the mapped stage derived from
    ``dispatch_performer._STAGE_TO_ROLE`` (the canonical mapping).
    """
    if name in lifecycle:
        return name
    # Build role→stage from the canonical stage→role mapping (single source of truth).
    from coordinare.graph.nodes.dispatch_performer import _STAGE_TO_ROLE
    role_to_stage = {v: k for k, v in _STAGE_TO_ROLE.items()}
    return role_to_stage.get(name, name)


def _parse_coordinare_commands(
    reviews: list[dict[str, Any]],
    lifecycle: list[str] | None = None,
) -> dict[str, Any] | None:
    """Extract the first ``/coordinare`` command from review bodies.

    Recognized commands: ``skip-<role>``, ``restart-from <role>``, ``veto``.
    Returns a pending-override dict or None if no command was found.
    """
    _lifecycle = lifecycle or []
    for review in reviews:
        body = str(review.get("body", ""))
        match = _COMMAND_RE.search(body)
        if match:
            cmd = match.group(1).lower().strip()
            if cmd == "veto":
                return {"action": "veto"}
            if cmd.startswith("skip-"):
                return {"action": "skip"}
            if cmd.startswith("restart-from"):
                # Regex guarantees \w+ after restart-from, so split always has 2+ parts.
                parts = cmd.split()
                target = parts[1]
                resolved = _resolve_stage(target, _lifecycle)
                return {"action": "restart", "target_stage": resolved}
    return None

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


# ---------------------------------------------------------------------------
# 029 — AI Classification prompt and confidence threshold
# ---------------------------------------------------------------------------

CONFIDENCE_THRESHOLD = 0.6

CLASSIFICATION_PROMPT = """Classify the following PR review comments into concern categories.

Valid categories: implementation, architecture, security, documentation, qa, review

For each comment, return a JSON array of objects with "concern" (string) and "confidence" (float 0.0-1.0).
Only include concerns with confidence >= {threshold}.

Example response:
[{{"concern": "implementation", "confidence": 0.9}}, {{"concern": "security", "confidence": 0.7}}]

PR comments to classify:
{comment_text}

Respond with ONLY a JSON array. No explanation."""


async def _classify_with_ai(
    reviews: list[dict[str, Any]],
    conducting_backend: Any,
) -> list[str] | None:
    """Classify PR comments using the AI assessment backend.

    Uses ``conducting_backend.prompt()`` to send a classification prompt
    and receive a structured JSON response (039).

    Returns a sorted list of concern names above the confidence threshold,
    or None if the AI backend is unavailable or returns an unusable response.
    """
    # Build comment text from all reviews
    parts: list[str] = []
    for review in reviews:
        body = str(review.get("body", ""))
        if body.strip():
            parts.append(body)
        raw_comments = review.get("comments")
        if isinstance(raw_comments, list):
            for c in raw_comments:
                if isinstance(c, dict):
                    parts.append(str(c.get("body", "")))
    comment_text = "\n\n".join(parts)

    if not comment_text.strip():
        return None

    prompt_text = CLASSIFICATION_PROMPT.format(comment_text=comment_text, threshold=CONFIDENCE_THRESHOLD)

    try:
        result = await conducting_backend.prompt(prompt_text, response_format="json")
    except Exception as exc:
        logger.warning("classify_human_feedback.ai_backend_error", error=str(exc))
        return None

    if not isinstance(result, dict):
        logger.warning("classify_human_feedback.ai_response_not_dict")
        return None

    # prompt() returns {"text": str, "data": parsed_json | None}
    parsed = result.get("data")
    if parsed is None:
        # Fall back to parsing the raw text if data wasn't auto-parsed
        import json as _json

        raw = result.get("text", "")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = _json.loads(raw)
            except (ValueError, TypeError):
                logger.warning("classify_human_feedback.ai_response_parse_failed")
                return None
        else:
            return None

    if isinstance(parsed, dict):
        # Some models wrap the array in a dict — unwrap the first list-valued key.
        for v in parsed.values():
            if isinstance(v, list):
                parsed = v
                break
        else:
            return None
    if not isinstance(parsed, list):
        return None

    concerns = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        concern = item.get("concern", "")
        confidence = item.get("confidence", 0.0)
        if isinstance(confidence, (int, float)) and confidence >= CONFIDENCE_THRESHOLD and concern in CONCERN_TO_STAGE:
            concerns.add(concern)

    return sorted(concerns) if concerns else None


def classify_feedback_concerns(reviews: list[dict[str, Any]]) -> list[str]:
    """Extract concern categories from pending reviews using keyword heuristics.

    This is the fallback classifier used when the AI backend is unavailable
    or returns no actionable results.
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

    # 031: Parse /coordinare commands before concern classification.
    # Only parse if no override is already queued (dashboard takes precedence per FR-010).
    if state.get("pending_override") is None:
        command = _parse_coordinare_commands(pending_reviews, lifecycle)
        if command is not None:
            # Validate restart-from target against lifecycle
            if command.get("action") == "restart" and command.get("target_stage") not in lifecycle:
                logger.warning(
                    "classify_human_feedback.invalid_restart_target",
                    target=command.get("target_stage"),
                    lifecycle=lifecycle,
                )
                # Don't set override — fall through to normal classification
            else:
                logger.info("classify_human_feedback.override_command", command=command)
                state["pending_override"] = command
                state["pending_reviews"] = []
                state["phase"] = "dispatching"
                return state

    # 029: Try AI classification first, fall back to keywords
    classification_method = "keyword"
    conducting_backend = state.get("conducting_backend")
    concern_names: list[str] | None = None

    if conducting_backend is not None:
        concern_names = await _classify_with_ai(pending_reviews, conducting_backend)
        if concern_names is not None:
            classification_method = "ai"

    if concern_names is None:
        concern_names = classify_feedback_concerns(pending_reviews)

    target_stages = [
        CONCERN_TO_STAGE[c] for c in concern_names if c in CONCERN_TO_STAGE
    ]

    if not target_stages:
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
        classification_method=classification_method,
    )

    # 066 FR-010 contract for this node: ``current_card`` writes go through
    # ``_set_current_card`` so the session entry stays the source of truth, but
    # the per-cycle scratch fields below (``agent_dispatch``, ``performer_stage``,
    # ``phase``, ``relay_feedback``, ``pending_reviews``) are intentionally
    # flat-only — they have no session-entry counterpart and are not part of
    # the I3 invariant.  Do not "session-ify" these without first extending
    # CardSession + the rederive contract.

    # Move card to IN_PROGRESS on the board so the next check_board cycle
    # routes to dispatching (not back to monitoring_pr).
    github = state.get("github_service")
    board_provider = board_of(state)
    card = state.get("current_card")
    if github is not None and isinstance(card, dict):
        card_id = str(card.get("id", ""))
        try:
            await move_card_or_warn(board_provider, card_id, "IN_PROGRESS")
        except Exception:
            logger.warning("classify_human_feedback.move_card_failed", card_id=card_id)
        card["previous_status"] = card.get("status", "IN_REVIEW")
        card["status"] = "IN_PROGRESS"
        _set_current_card(state, card)

    # Store feedback for the dispatch node to relay (FR-010).
    # Mark review IDs as processed NOW (after relay is committed to state),
    # not in monitor_pr (where a crash before dispatch would skip them).
    processed_ids: set[str] = state.get("processed_review_ids") or set()
    new_ids = {str(r.get("id", "")) for r in pending_reviews if isinstance(r, dict) and r.get("id")}
    state["processed_review_ids"] = processed_ids | new_ids
    if state.get("last_attempt_id"):
        state["last_attempt_failure_source"] = "human"
    state["relay_feedback"] = pending_reviews
    state["pending_reviews"] = []
    state["performer_stage"] = target_stage
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    # Clear lifecycle_completed_at so the next lifecycle completion sets a
    # fresh cutoff for monitor_pr review filtering.
    state["lifecycle_completed_at"] = None

    return state
