"""Dispatcher in-flight guard, per-card mutex, canonical branch name, and
relay drain/reap helpers (spec 076).

Contracts:
- ``specs/076-qa-cycle/contracts/in-flight-guard.md``
- ``specs/076-qa-cycle/contracts/canonical-branch.md``

This module is consumed by ``coordinare.graph.nodes.dispatch_performer`` at
the top of every dispatch call and by ``coordinare.graph.nodes.monitor_performer``
on relay handoffs.  No graph state mutation lives here — pure helpers and
a module-level mutex registry.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from coordinare.services.dispatcher_dedup_models import CanonicalBranchName

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

# Pinned algorithm version — bump in lock-step with any change to
# ``compute_title_slug`` so the contract test catches accidental drift.
SLUG_ALGORITHM_VERSION: int = 1

# Per-(card_id, performer_stage) mutex registry.  Module-level so two
# concurrent graph invocations on the same card share the same lock.
# Entries are never reaped — each entry is ~200 bytes and the registry
# grows monotonically per session; even 10k cards stays under 2 MB.
_dispatch_locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


class InFlightGuardResult(BaseModel):
    """Returned by :func:`check_inflight`."""

    model_config = ConfigDict(extra="forbid")

    is_in_flight: bool
    session_id: str | None
    advice: Literal["proceed", "refuse"]


async def check_inflight(
    state: CoordinareState,
    card_id: str,
    performer_stage: str,
) -> InFlightGuardResult:
    """076 FR-001: refuse dispatch when a session is already in flight for
    this ``(card_id, performer_stage)`` AND the resolved service still
    considers it live.

    Returns advice=\"refuse\" when both conditions hold.  Returns
    advice=\"proceed\" otherwise (no session id set, session id set but
    service does not consider it live, or service unavailable).

    See ``specs/076-qa-cycle/contracts/in-flight-guard.md`` for the
    precondition and post-condition contract.
    """
    agent_dispatch = state.get("agent_dispatch") or {}
    if not isinstance(agent_dispatch, dict):
        return InFlightGuardResult(is_in_flight=False, session_id=None, advice="proceed")
    session_id = agent_dispatch.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return InFlightGuardResult(is_in_flight=False, session_id=None, advice="proceed")

    performer_services = state.get("performer_services") or {}
    service = performer_services.get(performer_stage) if isinstance(performer_services, dict) else None
    has_live = getattr(service, "has_live_session", None) if service is not None else None
    if not callable(has_live):
        # No service / no liveness probe → cannot prove the session is alive,
        # so do not refuse.  This preserves the existing behaviour when a
        # snapshot is restored before performer_services is repopulated.
        return InFlightGuardResult(
            is_in_flight=False, session_id=session_id, advice="proceed",
        )

    try:
        is_alive = bool(has_live(session_id))
    except Exception as exc:
        # Liveness probe failed — log and proceed.  Better to dispatch
        # than to wedge on a probe failure (FR-012 fail-open style).
        logger.warning(
            "dispatch_performer.in_flight_guard_probe_failed",
            card_id=card_id,
            performer_stage=performer_stage,
            session_id=session_id,
            error=str(exc),
        )
        return InFlightGuardResult(
            is_in_flight=False, session_id=session_id, advice="proceed",
        )

    if is_alive:
        logger.warning(
            "dispatch_performer.in_flight_guard_tripped",
            card_id=card_id,
            performer_stage=performer_stage,
            session_id=session_id,
            service_has_live_session=True,
        )
        return InFlightGuardResult(
            is_in_flight=True, session_id=session_id, advice="refuse",
        )
    return InFlightGuardResult(
        is_in_flight=False, session_id=session_id, advice="proceed",
    )


def acquire_dispatch_lock(card_id: str, performer_stage: str) -> asyncio.Lock:
    """Return the per-(card_id, performer_stage) mutex.

    Caller is expected to use the lock with ``async with``.  See FR-006
    + ``contracts/in-flight-guard.md`` §"Mutex semantics".
    """
    return _dispatch_locks[(card_id, performer_stage)]


class _MutexHeldContext:
    """Async context manager for the per-(card, stage) dispatch lock.

    Acquires the lock, measures wait time, emits ``dispatch_performer
    .mutex_waited`` if the wait was non-trivial (>0 ms), and releases on
    exit (even on exception).
    """

    def __init__(self, card_id: str, performer_stage: str, lock: asyncio.Lock) -> None:
        self._card_id = card_id
        self._performer_stage = performer_stage
        self._lock = lock
        self._wait_started_at: float = 0.0

    async def __aenter__(self) -> _MutexHeldContext:
        from time import perf_counter
        self._wait_started_at = perf_counter()
        was_contested = self._lock.locked()
        await self._lock.acquire()
        if was_contested:
            wait_ms = (perf_counter() - self._wait_started_at) * 1000.0
            logger.info(
                "dispatch_performer.mutex_waited",
                card_id=self._card_id,
                performer_stage=self._performer_stage,
                wait_ms=round(wait_ms, 2),
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._lock.release()


def dispatch_mutex(card_id: str, performer_stage: str) -> _MutexHeldContext:
    """076 FR-006: per-(card_id, performer_stage) mutex context manager.

    Use with ``async with``.  Concurrent invocations on the same
    ``(card_id, performer_stage)`` MUST serialise.  Other tuples remain
    fully parallel.
    """
    return _MutexHeldContext(card_id, performer_stage, acquire_dispatch_lock(card_id, performer_stage))


def compute_title_slug(title: str) -> str:
    """Deterministic, idempotent, 60-char-bounded title slug (FR-022 / Q2).

    See ``specs/076-qa-cycle/contracts/canonical-branch.md`` for the full
    algorithm specification and mandatory test vectors.
    """
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) <= 60:
        return s
    cut = s[:60].rsplit("-", 1)
    return cut[0] or s[:60]


def canonical_branch_name(card: dict) -> CanonicalBranchName:
    """Build the canonical branch name for a card.

    The card dict MUST contain ``id`` and ``title``.  Raises ``ValueError``
    if either is missing or if the resulting slug is empty (degenerate
    titles — caller MUST refuse to dispatch per the contract).
    """
    card_node_id = card.get("id")
    title = card.get("title")
    if not isinstance(card_node_id, str) or not card_node_id:
        msg = "card.id is required to compute the canonical branch name"
        raise ValueError(msg)
    if not isinstance(title, str):
        msg = "card.title is required to compute the canonical branch name"
        raise ValueError(msg)
    slug = compute_title_slug(title)
    if not slug:
        msg = f"card.title {title!r} produced an empty slug; cannot dispatch"
        raise ValueError(msg)
    return CanonicalBranchName(card_node_id=card_node_id, title_slug=slug)


async def detect_multi_pr_divergence(
    state: CoordinareState,
    card_id: str,
    *,
    github_service: Any = None,
    owner: str | None = None,
    repo: str | None = None,
    trigger: Literal["dispatch", "restart", "webhook"] = "dispatch",
) -> dict[str, Any] | None:
    """076 (T111, FR-024): detect when > 1 open PR exists for a card
    matching the canonical branch prefix.

    Returns a ``MultiPRDivergence``-shaped dict on detection, or None.
    On detection, ALSO stores the dict on
    ``state.active_sessions[card_id].multi_pr_divergence`` for audit
    + dashboard surfacing.

    The check uses ``github_service.list_prs_by_branch_prefix`` (T110)
    so test doubles can return canned answers.  If the github service
    is None or raises, returns None — never propagates a GitHub error
    into the dispatch path.
    """
    if github_service is None or owner is None or repo is None:
        return None

    sessions = state.get("active_sessions") or {}
    sess = sessions.get(card_id) if isinstance(sessions, dict) else None
    card = sess.get("current_card") if isinstance(sess, dict) else None
    if not isinstance(card, dict):
        card = state.get("current_card") if isinstance(state.get("current_card"), dict) else None
    if not isinstance(card, dict):
        return None

    try:
        prefix = f"coordinare/{card.get('id', '')}/"
        prs = await github_service.list_prs_by_branch_prefix(
            owner, repo, prefix, state="OPEN", limit=20,
        )
    except Exception as exc:
        logger.warning(
            "dispatch_performer.multi_pr_divergence_check_failed",
            card_id=card_id,
            error=str(exc),
        )
        return None

    if not isinstance(prs, list) or len(prs) <= 1:
        return None  # 0 or 1 PR → no divergence

    divergence: dict[str, Any] = {
        "card_id": card_id,
        "canonical_branch_prefix": prefix,
        "pr_numbers": [p.get("number") for p in prs if isinstance(p, dict) and p.get("number")],
        "detected_at": datetime.now(UTC).isoformat(),
        "detected_at_trigger": trigger,
        "response": "dispatch_refused" if trigger == "dispatch" else "card_blocked",
    }
    if isinstance(sess, dict):
        sess["multi_pr_divergence"] = divergence
    logger.warning(
        "daemon.multi_pr_divergence_detected",
        card_id=card_id,
        trigger=trigger,
        pr_numbers=divergence["pr_numbers"],
        canonical_branch_prefix=prefix,
    )
    return divergence


async def drain_or_reap(
    session_id: str,
    *,
    service: Any = None,
    docker_executor: Any = None,
    drain_budget: float = 5.0,
    reap_budget: float = 5.0,
) -> tuple[Literal["drained", "reaped"], float]:
    """076 (T084, FR-007 / clarification Q5): drain a performer container
    on relay handoff, force-stop if it doesn't drain within budget.

    Total wall-clock budget hard-capped at ``drain_budget + reap_budget``
    (default 10 s).  Returns ``("drained", elapsed_ms)`` if the
    job-runner finished its current call cleanly within ``drain_budget``;
    otherwise issues ``docker stop --time=<reap_budget>`` and returns
    ``("reaped", elapsed_ms)``.

    Best-effort: if the docker_stop also fails, escalates to docker_kill
    and STILL returns ``("reaped", elapsed_ms)`` — the relay handoff
    MUST NOT wedge on a sticky container.  Failures are visible via
    ``daemon.reap_failed`` from the docker_executor layer.
    """
    from time import perf_counter

    started = perf_counter()
    container_id = _container_id_for_session(service, session_id)

    # --- Step 1: drain ---
    if service is not None and container_id is not None:
        drained = await _request_drain(service, session_id, timeout=drain_budget)
        elapsed_ms = (perf_counter() - started) * 1000.0
        if drained:
            logger.info(
                "dispatch_performer.drain_succeeded",
                session_id=session_id,
                container_id=container_id,
                elapsed_ms=round(elapsed_ms, 2),
            )
            return "drained", elapsed_ms

    # --- Step 2: reap ---
    if docker_executor is not None and container_id is not None:
        try:
            await docker_executor.stop_container(container_id, timeout=reap_budget)
        except Exception as exc:
            logger.warning(
                "daemon.reap_failed",
                container_id=container_id,
                session_id=session_id,
                reason="drain_or_reap_stop_failed",
                error=str(exc),
            )
    elapsed_ms = (perf_counter() - started) * 1000.0
    logger.info(
        "dispatch_performer.drain_reaped",
        session_id=session_id,
        container_id=container_id,
        elapsed_ms=round(elapsed_ms, 2),
    )
    return "reaped", elapsed_ms


def _container_id_for_session(service: Any, session_id: str) -> str | None:
    """Look up the container_id for a session via the service's
    ``_active_jobs`` registry.  Returns None if the service is None,
    has no registry, or the session isn't tracked."""
    if service is None:
        return None
    jobs = getattr(service, "_active_jobs", None)
    if not isinstance(jobs, dict):
        return None
    job = jobs.get(session_id)
    return getattr(job, "container_id", None) if job is not None else None


async def _request_drain(service: Any, session_id: str, *, timeout: float) -> bool:
    """Ask the service to drain a session's current LLM call cleanly.

    Best-effort: if the service has no drain method, or the call times
    out / errors, returns False so the caller falls through to the
    docker-stop path.
    """
    import asyncio

    drain = getattr(service, "drain_session", None)
    if not callable(drain):
        return False
    try:
        await asyncio.wait_for(drain(session_id), timeout=timeout)
        return True
    except (TimeoutError, Exception):
        return False


__all__ = [
    "SLUG_ALGORITHM_VERSION",
    "InFlightGuardResult",
    "acquire_dispatch_lock",
    "canonical_branch_name",
    "check_inflight",
    "compute_title_slug",
    "detect_multi_pr_divergence",
    "dispatch_mutex",
    "drain_or_reap",
]
