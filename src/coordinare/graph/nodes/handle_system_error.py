"""handle_system_error — retry performer errors before escalating to operator."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import structlog

from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.upstream_errors import UpstreamHTTPError

logger = structlog.get_logger(__name__)

_RETRY_INTERVAL = 90.0   # seconds between retry attempts
_MAX_RETRIES = 3          # maximum attempts before notifying operator

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
    - If attempts < _MAX_RETRIES but 90s have not elapsed: set idle (wait for next cycle).
    - If attempts >= _MAX_RETRIES: notify operator via notification service, move card to
      BLOCKED on GitHub (without commenting), set system_error_notified=True, go idle.
    """
    count = state.get("system_error_count", 0)
    last_at = state.get("system_error_last_at")
    now = datetime.now(UTC)
    card = state.get("current_card") or {}
    card_id = str(card.get("id", ""))

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
            state["phase"] = "idle"
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
        logger.error(
            "handle_system_error.max_retries_exceeded",
            card_id=card_id,
            reason=reason,
            attempts=count,
        )
        notification_service = state.get("notification_service")
        if notification_service is not None:
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

        github = state.get("github_service")
        if github is not None and card_id:
            try:
                await github.move_card(card_id, "BLOCKED")
            except Exception as exc:
                logger.warning("handle_system_error.move_card_failed", error=str(exc))

        state["system_error_notified"] = True

    state["phase"] = "idle"
    return state
