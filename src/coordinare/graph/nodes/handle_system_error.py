"""handle_system_error — retry performer errors before escalating to operator."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import structlog

from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity
from coordinare.services.assessor_failure import classify_assessor_failure
from coordinare.services.board_provider import board_of, move_card_or_warn

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.upstream_errors import UpstreamHTTPError

logger = structlog.get_logger(__name__)

_RETRY_INTERVAL = 90.0   # seconds between retry attempts
_MAX_RETRIES = 3          # maximum attempts before notifying operator
# 123 US3 (FR-008): per-card transient/infra failure budget — how many times a
# card may definitively fail a dispatch on infra/transient grounds (each
# exhausting the per-dispatch _MAX_RETRIES above) before it is surfaced as
# ENV_BLOCKED. Distinct from _MAX_RETRIES (which counts retries WITHIN one
# dispatch); this counter accumulates across the card's lifetime.
_TRANSIENT_ERROR_CYCLE_LIMIT = 3

# Single source of truth for upstream-HTTP transient/permanent classification
# (spec 067 FR-005). No other substring-matching of upstream content is
# permitted anywhere else in the codebase.
TRANSIENT_STATUSES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
# Body-marker fallback is only consulted on 5xx responses — a 4xx that
# happens to mention "rate limit" as part of a policy explanation must not be
# auto-retried. Markers are anchored tighter than bare substrings to reduce
# false positives (e.g. a 500 whose body merely *describes* rate limiting).
TRANSIENT_BODY_MARKERS: tuple[str, ...] = (
    "upstream temporarily unavailable",
    "rate limit exceeded",
    "rate_limit_exceeded",
)


def classify_upstream(error: UpstreamHTTPError) -> Literal["transient", "permanent"]:
    """Classify an UpstreamHTTPError as transient (retry) or permanent (surface).

    Status takes precedence; the body-marker tuple is a documented fallback
    consulted **only on 5xx** responses, for proxies that return a generic
    500 wrapping a transient upstream condition (e.g. some LiteLLM proxies
    return 500 with 'upstream temporarily unavailable'). 4xx responses are
    always permanent — a client error mentioning rate-limit policy in prose
    must not trigger automatic retry. 2xx responses never reach this function
    — the envelope is only constructed on non-2xx upstream responses.
    """
    if error.status in TRANSIENT_STATUSES:
        return "transient"
    if 500 <= error.status < 600:
        body_lower = error.body.lower()
        if any(marker in body_lower for marker in TRANSIENT_BODY_MARKERS):
            return "transient"
    return "permanent"


async def handle_system_error(state: CoordinareState) -> CoordinareState:
    """Retry failed performer dispatches up to _MAX_RETRIES times.

    On each invocation:
    - If attempts < _MAX_RETRIES and 90s have elapsed: clear dispatch, re-queue for dispatch.
    - If attempts < _MAX_RETRIES but 90s have not elapsed: keep phase="system_error" so
      check_board's session-error early-return re-routes back here on the next cycle.
      (Setting phase="idle" here used to wedge the card permanently — nothing else routes
      back to handle_system_error, so the retry timer never re-fired. 069 follow-up.)
    - If attempts >= _MAX_RETRIES: notify operator via notification service, move card to
      BLOCKED on GitHub (without commenting), set system_error_notified=True, go idle.
    """
    count = state.get("system_error_count", 0)
    last_at = state.get("system_error_last_at")
    now = datetime.now(UTC)
    card = state.get("current_card") or {}
    card_id = str(card.get("id", ""))

    # 163: a persisted pre-upgrade truncation must not re-enter the unchanged
    # system-error retry loop either. New failures take monitor's token-limit path.
    if classify_assessor_failure(state.get("system_error_reason")) == "truncated":
        state["phase"] = "blocked"
        state["open_questions"] = [
            "The model exhausted its output budget. Raise the performer output token cap "
            "or shorten the prompt before retrying.",
        ]
        return state

    if count < _MAX_RETRIES:
        elapsed = (now - last_at).total_seconds() if last_at else _RETRY_INTERVAL
        if elapsed < _RETRY_INTERVAL:
            logger.info(
                "handle_system_error.waiting",
                card_id=card_id,
                attempt=count,
                elapsed=f"{elapsed:.0f}s",
                retry_in=f"{_RETRY_INTERVAL - elapsed:.0f}s",
            )
            state["phase"] = "system_error"
            return state

        logger.info(
            "handle_system_error.retrying",
            card_id=card_id,
            attempt=count,
            max_retries=_MAX_RETRIES,
        )
        # 065 Fix 21: tear down the previous ephemeral container before clearing
        # agent_dispatch. dispatch_performer's success path will launch a fresh
        # container — without this teardown the previous one is orphaned (no
        # other code path holds a reference to its session_id) and leaks for
        # every retry cycle.  Mirrors monitor_performer.transport_error cleanup.
        prev_dispatch = state.get("agent_dispatch") or {}
        prev_session_id = prev_dispatch.get("session_id") if isinstance(prev_dispatch, dict) else None
        if prev_session_id:
            stage = str(state.get("performer_stage") or "")
            performer_services = state.get("performer_services") or {}
            service = performer_services.get(stage) if isinstance(performer_services, dict) else None
            cleanup = getattr(service, "_cleanup_ephemeral_job_by_id", None) if service is not None else None
            if cleanup is not None:
                try:
                    await cleanup(str(prev_session_id))
                except Exception as exc:
                    logger.warning(
                        "handle_system_error.cleanup_failed",
                        card_id=card_id,
                        session_id=str(prev_session_id),
                        exc_type=type(exc).__name__,
                        error=str(exc),
                    )
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["phase"] = "dispatching"
        return state

    # Max retries exhausted — notify operator once, then move card to BLOCKED
    if not state.get("system_error_notified"):
        reason = state.get("system_error_reason") or "Unknown performer error"
        # 098 US3 (FR-003): when the assessor stage exhausted retries on a
        # persistently EMPTY upstream (the heavily-shared model is overloaded/down),
        # this is an infrastructure condition — surface it as ENV_BLOCKED (operator
        # must act on capacity), not a generic card-fault terminal error. A
        # malformed-body exhaustion is a content failure and blocks normally.
        stage = str(state.get("performer_stage") or "")
        assessor_shape = classify_assessor_failure(reason)
        is_env_blocked = stage == "assessing" and assessor_shape == "empty_body"
        cause = (
            "Assessor model unavailable/overloaded — the assessor upstream "
            "returned empty responses across all retries."
        )
        action = (
            "Operator: check assessor model capacity/availability (the shared "
            "model is likely under load); the card auto-resumes when it recovers."
        )

        # 123 US3 (FR-006/FR-008): this dispatch has definitively failed on a
        # transient/infra condition (the per-dispatch system_error_count retry
        # budget above is exhausted). Bump the SEPARATE per-card transient budget
        # — it accumulates across the card's lifetime and is never reset between
        # dispatches, and it never touches content_feedback_cycles (content
        # feedback keeps its own budget, FR-006). At the limit (3), surface the
        # card as ENV_BLOCKED (spec-095 shape) rather than a generic
        # performer_error, so a card repeatedly hitting infra failures becomes
        # operator-actionable instead of silently churning.
        transient_cycles = int(state.get("transient_error_cycles") or 0) + 1
        state["transient_error_cycles"] = transient_cycles
        if transient_cycles >= _TRANSIENT_ERROR_CYCLE_LIMIT and not is_env_blocked:
            is_env_blocked = True
            cause = (
                f"Repeated infrastructure/transient performer failures "
                f"({transient_cycles} across this card's lifetime)."
            )
            action = (
                "Operator: investigate the performer environment/backend; the "
                "card auto-resumes when the infrastructure recovers."
            )

        logger.error(
            "handle_system_error.max_retries_exceeded",
            card_id=card_id,
            reason=reason,
            attempts=count,
            env_blocked=is_env_blocked,
        )
        # 123 US3: distinguish the assessor-empty-body env-block from the
        # transient-budget-exhaustion env-block so dedup + operator surfacing
        # carry the right pattern.
        env_pattern_id = (
            "assessor_model_unavailable"
            if stage == "assessing" and assessor_shape == "empty_body"
            else "transient_error_budget_exhausted"
        )
        notification_service = state.get("notification_service")
        if notification_service is not None:
            if is_env_blocked:
                event = NotificationEvent(
                    event_type=EventType.env_blocked,
                    severity=NotificationSeverity.warning,
                    payload={
                        "event_type": EventType.env_blocked.value,
                        "card_title": str(card.get("title", "")),
                        "card_id": card_id,
                        "pattern_id": env_pattern_id,
                        "cause": cause,
                        "action": action,
                        "attempts": str(count),
                    },
                    source="handle_system_error",
                    dedup_key=f"env_blocked:{env_pattern_id}:{card_id}",
                )
            else:
                event = NotificationEvent(
                    event_type=EventType.performer_error,
                    severity=NotificationSeverity.critical,
                    payload={
                        "card_title": str(card.get("title", "")),
                        "card_id": card_id,
                        "error": reason[:500],
                        "attempts": str(count),
                    },
                    source="handle_system_error",
                    dedup_key=f"system_error:{card_id}",
                )
            try:
                await notification_service.dispatch(event)
            except Exception as exc:
                logger.warning("handle_system_error.notification_failed", error=str(exc))

        if is_env_blocked:
            # Per-card ENV_BLOCKED marker (spec-095 shape) so the surfacing is
            # deduped and operator-visible. No head_sha at the assessing stage.
            state["env_blocked"] = {
                "head_sha": "",
                "pattern_id": env_pattern_id,
                "cause": cause,
                "action": action,
            }

        board_provider = board_of(state)
        if board_provider is not None and card_id:
            try:
                await move_card_or_warn(board_provider, card_id, "BLOCKED")
            except Exception as exc:
                logger.warning("handle_system_error.move_card_failed", error=str(exc))

        state["system_error_notified"] = True

        from coordinare.services.attempt_telemetry import close_attempt
        close_attempt(state, "error", "system", None)

    state["phase"] = "idle"
    return state
