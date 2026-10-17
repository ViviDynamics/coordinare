"""Bootstrap progress tracking and operator reset budgets (270)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.environment_repair import restart_environment_attempt

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.activity_log import ActivityLog

BOOTSTRAP_OPEN_TOOL_LIMIT_SECONDS = 2700
BOOTSTRAP_ANONYMOUS_TOOL_MAX_AGE_SECONDS = 1200
MANUAL_BOOTSTRAP_RESET_COOLDOWN = timedelta(minutes=15)
_TOOL_START_TYPES = frozenset({"tool_use", "tool_call", "local_shell_call"})
_TOOL_RESULT_TYPES = frozenset({"tool_result", "tool_call_result"})
_log = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _ResetRequest:
    actor: str
    activity_log: ActivityLog
    cleanup_confirmed: bool = False


def reset_env_bootstrap_breaker(
    state: CoordinareState, symphony: str, cache_state: EnvCacheState,
    actor: str, activity_log: ActivityLog,
) -> datetime | None:
    return _reset_breaker(state, symphony, cache_state, _ResetRequest(actor, activity_log))


async def request_env_bootstrap_reset(
    state: CoordinareState, symphony: str, cache_state: EnvCacheState,
    actor: str, activity_log: ActivityLog,
) -> datetime | None:
    """Confirm owned runtime cleanup before the shared atomic operator reset."""
    cleanup_confirmed = False
    journal = cache_state.environment_attempt
    if isinstance(journal, dict) and (journal.get("status") == "executing"
            or journal.get("last_error") == "cleanup_unconfirmed"
            or (journal.get("target") is not None and journal.get("status") not in {"ready", "repair_pending"})):
        runtime = (state.get("bootstrap_runtimes") or {}).get(symphony)
        stop = getattr(getattr(runtime, "service", None), "stop_session_confirmed", None)
        job_id = getattr(runtime, "job_id", None)
        if stop is not None and isinstance(job_id, str) and job_id:
            try:
                async with asyncio.timeout(15):
                    cleanup_confirmed = await stop(job_id) is True
            except (OSError, RuntimeError, TimeoutError):
                cleanup_confirmed = False
    return _reset_breaker(state, symphony, cache_state, _ResetRequest(actor, activity_log, cleanup_confirmed))


def _reset_breaker(
    state: CoordinareState, symphony: str, cache_state: EnvCacheState, request: _ResetRequest,
) -> datetime | None:
    """Reset once per cooldown. Return the next allowed time on refusal, without writes.

    Both dashboard and MCP bootstrap requests reach this service through the API.
    There is no await between the cooldown check and its reservation.
    """
    now = _utcnow()
    previous_reset = state.get("env_bootstrap_manual_resets", {}).get(symphony)
    if previous_reset is not None:
        next_reset = previous_reset + MANUAL_BOOTSTRAP_RESET_COOLDOWN
        if now < next_reset:
            return next_reset
    restart_environment_attempt(cache_state, cleanup_confirmed=request.cleanup_confirmed)
    state.setdefault("env_bootstrap_manual_resets", {})[symphony] = now
    actor, activity_log = request.actor, request.activity_log
    previous, was_exhausted = cache_state.bootstrap_attempts, cache_state.bootstrap_exhausted
    cache_state.bootstrap_attempts = 0
    cache_state.bootstrap_exhausted = False
    cache_state.bootstrap_exhaustion_reason = None
    cache_state.infra_failures = 0
    breaker = "tripped" if was_exhausted else "armed"
    text = (
        # The actor can be the word "token", and "token:" reads as a credential to the redactor.
        f"Env bootstrap for '{symphony}' forced by {actor}. Retry budget reset "
        f"(was {previous} attempts, breaker {breaker})."
    )
    _log.info("symphony.env_bootstrap_forced", symphony=symphony, actor=actor, previous_attempts=previous)
    try:
        activity_log.record(activity_type="operator_action", text=text)
    except Exception:
        _log.warning("symphony.env_bootstrap_activity_not_recorded", symphony=symphony)
    return None


@dataclass
class BootstrapTools:
    """Track unmatched starts across silent polls and overlapping event windows.

    Normalised backends also encode a result as tool_use with status ok or error.
    Older events lack call ids, so results for those close the oldest anonymous
    start. Explicit ids never close a different explicitly identified call.
    """

    open_since: dict[str, float] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    def observe(self, events: Any, now: float) -> bool:
        """Consume events, expire anonymous starts, and report results or new deltas."""
        self.open_since = {
            key: since for key, since in self.open_since.items()
            if not key.startswith("event:") or now - since < BOOTSTRAP_ANONYMOUS_TOOL_MAX_AGE_SECONDS
        }
        progressed = False
        for event in events or []:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind not in _TOOL_START_TYPES | _TOOL_RESULT_TYPES:
                continue
            identity = json.dumps(event, sort_keys=True, default=str)
            if identity in self.seen:
                continue
            self.seen.add(identity)
            if event.get("is_delta"):
                progressed = True
                continue
            call_id = event.get("tool_use_id") or event.get("call_id") or event.get("tool_call_id") or event.get("id")
            is_result = kind in _TOOL_RESULT_TYPES or event.get("status") in {"ok", "error"}
            if not is_result:
                key = f"call:{call_id}" if call_id else f"event:{identity}"
                self.open_since.setdefault(key, now)
                continue
            call_id = call_id or event.get("detail")
            key = f"call:{call_id}"
            if key not in self.open_since:
                key = next((k for k in self.open_since if not call_id or k.startswith("event:")), "")
            if key in self.open_since:
                del self.open_since[key]
            progressed = True
        return progressed
