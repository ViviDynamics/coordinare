"""Startup reconciliation pass + per-cycle wedge invariant (spec 076).

Contract: ``specs/076-qa-cycle/contracts/reconciliation-pass.md``

Implements FR-002 (restart-time reconciliation pass), FR-003 (re-adopt),
FR-004 (reap before replace), FR-005 (orphan sweep), FR-008 (stale-
session re-dispatch hardening), FR-011 (persistent-mode opt-out),
FR-012 (Docker-down fail-closed), FR-013 (observability).

The wedge invariant (FR-020 / detect_wedged_state) lands in Phase 5.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.state import _retire_active_session
from coordinare.services.dispatcher_dedup_models import (
    ReconciliationDecision,
    ReconciliationReport,
    WedgeResolution,
)
from coordinare.services.docker_executor import (
    ContainerInfo,
    DockerExecutor,
    DockerUnreachableError,
)
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.documenting_side import record_result
from coordinare.services.http_performer_service import _EphemeralJob
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.owned_writers import has_owned_writers
from coordinare.transport.base import TransportTimeoutError
from coordinare.transport.http_transport import PerformerAuthError, PerformerHTTPClient

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


_UNKNOWN_RUNNER_JOB = "runner job identity is unknown or inconsistent"

_COORDINARE_LABEL_FILTER = {"coordinare.spec_version": "076"}


async def run_startup_reconciliation(
    state: CoordinareState,
    docker_executor: DockerExecutor,
    *,
    budget_seconds: float = 30.0,
) -> ReconciliationReport:
    """076 FR-002: enumerate Docker containers, decide adopt / reap+replace
    / fresh-dispatch / orphan-sweep per card from the persisted snapshot.

    Returns a :class:`ReconciliationReport` summarising the decisions taken.
    Docker-down failures are surfaced via ``docker_unreachable=True`` on
    the report (per FR-012, the daemon refuses to dispatch any ephemeral
    performer in that case — the caller in ``daemon.py`` enforces this).

    Budget: ``budget_seconds`` (default 30 s) hard-caps total wall-clock.
    Cards not processed within the budget receive no decision; they will
    fall through to the normal graph cycle which fresh-dispatches them.
    """
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()

    in_flight_sessions = _collect_in_flight_sessions(state)
    side_sessions = {card_id: sess for card_id, sess in (state.get("active_sessions") or {}).items()
                     if isinstance(sess, dict) and isinstance(sess.get("documenting_side"), dict)
                     and (sess["documenting_side"].get("status") == "running" or sess["documenting_side"].get("writer_active"))
                     and sess["documenting_side"].get("session_id")}
    logger.info(
        "daemon.reconciliation_pass_started",
        cards_to_process=len(in_flight_sessions),
        budget_seconds=budget_seconds,
    )

    # SC-002 fast path: when the snapshot has zero in-flight sessions
    # (cold start / fresh boot), skip the entire Docker enumeration.
    # Orphan sweep is meaningful only when we have an authoritative
    # in-flight set to compare against; a snapshot with no in-flight
    # cards means we cannot distinguish a true orphan from a container
    # spawned by another tool on the host.
    if not in_flight_sessions and not side_sessions:
        completed_at = datetime.now(UTC)
        wall_clock = time.monotonic() - started_monotonic
        logger.info(
            "daemon.reconciliation_pass_complete",
            wall_clock_seconds=round(wall_clock, 3),
            decisions={},
            orphans_swept_count=0,
            fast_path=True,
        )
        return ReconciliationReport(
            started_at=started_at,
            completed_at=completed_at,
            wall_clock_seconds=wall_clock,
            cards_considered=0,
            decisions={},
            orphans_swept=[],
            docker_unreachable=False,
        )

    decisions: dict[str, ReconciliationDecision] = {}
    orphans_swept: list[str] = []
    docker_unreachable = False

    # FR-002: enumerate every coordinare-labelled container ONCE up front.
    # If Docker is down, fail closed (per FR-012) by recording the flag
    # and skipping per-card decisions; the daemon will refuse to dispatch.
    containers: list[ContainerInfo] = []
    needs_docker = any(_requires_docker(state, sess) for sess in in_flight_sessions.values())
    needs_docker = needs_docker or any(
        _requires_docker(state, {"performer_stage": "documenting"}) for _ in side_sessions
    )
    try:
        containers = await docker_executor.list_containers_by_label(
            _COORDINARE_LABEL_FILTER, timeout=min(budget_seconds, 10.0),
        ) if needs_docker else []
    except DockerUnreachableError as exc:
        logger.error(
            "daemon.reconciliation_pass_aborted_docker_unreachable",
            error=str(exc),
        )
        docker_unreachable = True
        completed_at = datetime.now(UTC)
        return ReconciliationReport(
            started_at=started_at,
            completed_at=completed_at,
            wall_clock_seconds=time.monotonic() - started_monotonic,
            cards_considered=0,
            decisions={},
            orphans_swept=[],
            docker_unreachable=True,
        )

    # Index containers by session_id for O(1) per-card lookup.  A
    # collision (two containers labelled with the same session_id) is
    # pathological — the prior run must have lost track of one.  Log it
    # and prefer the most-recently-started container; the loser will be
    # caught by the orphan sweep further down.
    by_session: dict[str, ContainerInfo] = {}
    for c in containers:
        sid = c.labels.get("coordinare.session_id")
        if not sid:
            continue
        existing = by_session.get(sid)
        if existing is not None:
            logger.warning(
                "daemon.reconciliation_session_id_collision",
                session_id=sid,
                container_a=existing.container_id,
                container_b=c.container_id,
                kept=c.container_id if c.started_at >= existing.started_at else existing.container_id,
            )
            # Keep whichever started more recently
            if c.started_at < existing.started_at:
                continue
        by_session[sid] = c

    # Walk every in-flight card.  Spend at most `budget_seconds` total.
    deadline = started_monotonic + budget_seconds
    for card_id, sess in in_flight_sessions.items():
        if time.monotonic() >= deadline:  # pragma: no cover — operational backstop
            logger.warning(
                "daemon.reconciliation_pass_budget_exceeded",
                cards_processed=len(decisions),
                cards_remaining=len(in_flight_sessions) - len(decisions),
            )
            break
        decision = await _classify_and_act(
            state, card_id, sess, by_session, docker_executor,
        )
        decisions[card_id] = decision

    # Early documenters own independent jobs and must survive restart alongside
    # the foreground performer. Never clear foreground dispatch during recovery.
    for card_id, sess in side_sessions.items():
        if time.monotonic() >= deadline:
            break
        await _restore_documenting_side(state, card_id, sess, by_session, docker_executor)

    # FR-005: orphan sweep — any container with a session_id NOT in the
    # snapshot is a leftover from a previous daemon run that exited
    # uncleanly.  Stop them all.
    persisted_session_ids: set[str] = {
        str((sess.get("agent_dispatch") or {}).get("session_id") or "")
        for sess in in_flight_sessions.values()
    }
    persisted_session_ids.update(str(sess["documenting_side"]["session_id"]) for sess in side_sessions.values())
    persisted_session_ids.discard("")
    for container in containers:
        if time.monotonic() >= deadline:  # pragma: no cover — operational backstop
            break
        sid = container.labels.get("coordinare.session_id")
        if not sid or sid in persisted_session_ids:
            continue
        # This container belongs to no in-flight session — orphan.
        if await _sweep_orphan(container, docker_executor):
            orphans_swept.append(container.container_id)

    completed_at = datetime.now(UTC)
    wall_clock = time.monotonic() - started_monotonic

    # Stash decisions on state for the notification layer to consult
    # (FR-010 / contracts/notification-dedup.md).  Cleared at the end
    # of the first poll cycle.
    state["reconciliation_decisions_last_startup"] = {
        cid: str(d) for cid, d in decisions.items()
    }

    logger.info(
        "daemon.reconciliation_pass_complete",
        wall_clock_seconds=round(wall_clock, 3),
        decisions={cid: str(d) for cid, d in decisions.items()},
        orphans_swept_count=len(orphans_swept),
    )

    return ReconciliationReport(
        started_at=started_at,
        completed_at=completed_at,
        wall_clock_seconds=wall_clock,
        cards_considered=len(decisions),
        decisions=decisions,
        orphans_swept=orphans_swept,
        docker_unreachable=docker_unreachable,
    )


#: 401: consecutive ``docker ps`` timeouts on one session before an unknown is
#: read as "Docker is genuinely gone" and the old fresh-dispatch fallback runs.
#: One slow answer under load restarted website#111 twice in 27 minutes while
#: its performer was fine; the startup pass treats the same condition as a
#: reason to dispatch NOTHING, and the per-cycle path must at least not
#: dispatch more.
DOCKER_UNREACHABLE_ESCALATION_STREAK = 3
#: Per-SESSION streaks, keyed by the performer session id. Review caught two
#: ways a single counter went wrong: when a card has no session dict the
#: target is the state root, so every such card shared one counter; and a
#: session dict outlives its performer, so a replacement performer inherited
#: the old count. A session id is unique per performer instance and per card,
#: on either storage target. Transient: not a session field, never persisted.
_STREAKS_KEY = "_docker_unreachable_streaks"


async def handle_potentially_stale_session(
    state: CoordinareState,
    card_id: str,
    *,
    docker_executor: DockerExecutor | None = None,
) -> ReconciliationDecision:
    """076 FR-008: per-card variant invoked from ``check_board._is_stale``.

    Replaces the unconditional ``agent_dispatch={}`` clearing that fired
    today's duplicate-dispatch sequence.  Attempts re-adoption first,
    falls back to reap-and-replace, then fresh-dispatch as a last resort.
    """
    sessions = state.get("active_sessions") or {}
    sess = sessions.get(card_id) if isinstance(sessions, dict) else None
    # 076 backwards-compat: pre-076 single-symphony tests (and the legacy
    # daemon path) set agent_dispatch at the state root rather than on a
    # per-card session in active_sessions.  Honour that by falling back
    # to the state dict itself.  The session-clearing helpers update
    # whichever dict was the source of truth.
    target: dict[str, Any] | None = sess if isinstance(sess, dict) else None
    if target is None:
        target = state  # type: ignore[assignment]

    dispatch = target.get("agent_dispatch") or {}
    selected_service = _resolve_service(state, str(target.get("performer_stage") or ""), dispatch.get("performer_id"))
    if not _is_ephemeral_container_service(selected_service):
        return (ReconciliationDecision.SKIPPED_PERSISTENT
                if _is_persistent_mode(selected_service) else ReconciliationDecision.DEFERRED)
    service = _kubernetes_service(state, target)
    if service is not None:
        return await _reconcile_kubernetes_session(target, card_id, service)
    if docker_executor is None:
        docker_executor = DockerExecutor()

    agent_dispatch = target.get("agent_dispatch") or {}
    session_id = agent_dispatch.get("session_id") if isinstance(agent_dispatch, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return _clear_agent_dispatch_and_return(
            target, ReconciliationDecision.FRESH_DISPATCHED, card_id,
        )

    try:
        containers = await docker_executor.list_containers_by_label(
            {"coordinare.session_id": session_id}, timeout=5.0,
        )
    except DockerUnreachableError as exc:
        # 401: a timeout is an unknown, not "the container is gone". Leave the
        # session untouched and count the streak; escalate to the old
        # fresh-dispatch fallback only once the unknown has persisted.
        streaks = target.setdefault(_STREAKS_KEY, {})
        if not isinstance(streaks, dict):
            streaks = target[_STREAKS_KEY] = {}
        streak = int(streaks.get(session_id) or 0) + 1
        if streak < DOCKER_UNREACHABLE_ESCALATION_STREAK:
            streaks[session_id] = streak
            logger.warning(
                "check_board.stale_session_deferred",
                card_id=card_id,
                session_id=session_id,
                decision=str(ReconciliationDecision.DEFERRED),
                reason="docker_unreachable",
                streak=streak,
                escalate_at=DOCKER_UNREACHABLE_ESCALATION_STREAK,
                error=str(exc),
            )
            return ReconciliationDecision.DEFERRED
        streaks.pop(session_id, None)
        logger.error(
            "check_board.stale_session_reconciled",
            card_id=card_id,
            session_id=session_id,
            decision="fresh_dispatched",
            reason="docker_unreachable",
            streak=streak,
            error=str(exc),
        )
        return _clear_agent_dispatch_and_return(
            target, ReconciliationDecision.FRESH_DISPATCHED, card_id,
        )
    _streaks = target.get(_STREAKS_KEY)
    if isinstance(_streaks, dict):
        _streaks.pop(session_id, None)  # 401: Docker answered; this session's streak is over
    decision = await _classify_and_act(
        state, card_id, target, {session_id: containers[0]} if containers else {}, docker_executor,
    )
    logger.info(
        "check_board.stale_session_reconciled",
        card_id=card_id,
        session_id=session_id,
        decision=str(decision),
    )
    return decision


def detect_wedged_state(
    state: CoordinareState,
    *,
    wedge_block_threshold: int = 3,
    wedge_block_window_hours: int = 24,
) -> WedgeResolution | None:
    """076 (T090, FR-020 / clarification Q1): per-cycle wedge invariant.

    The forbidden combination is::

        state.active_card != None
        AND state.active_sessions[active_card.id] is missing
        AND state.performer_stage in (None, "")
        AND state.phase in (None, "idle")

    When detected, the **default** action is to **release the pin**
    (set ``active_card=None``).  After ``wedge_block_threshold`` wedges
    in the trailing ``wedge_block_window_hours``, promote the next
    wedge to BLOCKED (emit ``card_blocked`` notification, set
    ``phase=blocked``).

    Returns the ``WedgeResolution`` taken, or ``None`` if no wedge was
    detected.  Emits ``daemon.wedged_state_detected`` then
    ``daemon.wedge_resolution`` for every wedge handled.
    """
    active_card = state.get("active_card")
    if not active_card and isinstance(state.get("current_card"), dict):
        active_card = state["current_card"]
    if not isinstance(active_card, dict):
        return None
    card_id = str(active_card.get("id", ""))
    if not card_id:
        return None

    sessions = state.get("active_sessions")
    has_session = (
        isinstance(sessions, dict)
        and card_id in sessions
        and isinstance(sessions.get(card_id), dict)
    )
    if has_session:
        return None  # session exists → not wedged

    performer_stage = state.get("performer_stage")
    if performer_stage:  # non-empty stage → not idle/wedged
        return None

    phase = state.get("phase")
    if phase not in (None, "idle"):
        return None

    # Wedge confirmed.  Update the per-card rolling window of wedge
    # timestamps (data-model §8: ``dict[str, list[datetime]]`` keyed by
    # card_id, NOT a flat list — different cards must have isolated
    # wedge counts so card A wedging once doesn't trip card B's BLOCKED
    # promotion).
    now = datetime.now(UTC)
    window_cutoff = now - timedelta(hours=wedge_block_window_hours)

    raw_windows = state.get("wedge_count_window")
    if not isinstance(raw_windows, dict):
        # Backward compatibility: a snapshot might have the legacy flat
        # list shape from an earlier 076 dev build.  Reset to the
        # spec-compliant dict shape.
        windows: dict[str, list[datetime]] = {}
    else:
        windows = dict(raw_windows)

    card_window: list[datetime] = []
    for entry in windows.get(card_id, []) or []:
        if isinstance(entry, datetime):
            ts = entry if entry.tzinfo is not None else entry.replace(tzinfo=UTC)
        elif isinstance(entry, str):
            try:
                ts = datetime.fromisoformat(entry)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                continue
        else:
            continue
        if ts > window_cutoff:
            card_window.append(ts)
    card_window.append(now)
    windows[card_id] = card_window
    state["wedge_count_window"] = windows
    window = card_window  # local alias for the rest of the function

    logger.warning(
        "daemon.wedged_state_detected",
        card_id=card_id,
        phase=str(phase) if phase is not None else None,
        performer_stage=str(performer_stage) if performer_stage is not None else None,
        active_sessions_keys=list(sessions.keys()) if isinstance(sessions, dict) else [],
        wedge_count_in_window=len(window),
    )

    # FR-020 / clarification Q1: default release-the-pin; promote to
    # BLOCKED only when this wedge exceeds the threshold — i.e. the
    # spec's "after N wedges, the NEXT wedge promotes."  With
    # threshold=3, wedges 1/2/3 release the pin; wedge 4 promotes.
    if len(window) > wedge_block_threshold:
        state["phase"] = "blocked"
        state["open_questions"] = [
            *(state.get("open_questions") or []),
            f"Card {card_id} has wedged {len(window)} times in the trailing "
            f"{wedge_block_window_hours}h. Operator intervention required.",
        ]
        # Notify via the standard card_blocked event channel — the notify
        # node will pick it up on its next pass.
        logger.warning(
            "card_blocked",
            card_id=card_id,
            reason="wedge_block_threshold_exceeded",
            wedge_count=len(window),
            window_hours=wedge_block_window_hours,
        )
        logger.info(
            "daemon.wedge_resolution",
            card_id=card_id,
            resolution="blocked",
            wedge_count_in_window=len(window),
        )
        return WedgeResolution.BLOCKED

    # Default: release the pin
    state["active_card"] = None
    state["current_card"] = None
    state["active_card_id"] = None
    logger.info(
        "daemon.wedge_resolution",
        card_id=card_id,
        resolution="released",
        wedge_count_in_window=len(window),
    )
    return WedgeResolution.RELEASED


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_in_flight_sessions(state: CoordinareState) -> dict[str, dict[str, Any]]:
    """Return the subset of ``state.active_sessions`` that the reconciliation
    pass needs to consider — sessions in dispatching / monitoring phases
    with a non-empty agent_dispatch.session_id."""
    sessions = state.get("active_sessions") or {}
    if not isinstance(sessions, dict):
        return {}
    in_flight: dict[str, dict[str, Any]] = {}
    for card_id, sess in sessions.items():
        if not isinstance(sess, dict):
            continue
        phase = sess.get("phase")
        if phase not in {"dispatching", "monitoring_performer", "monitoring_agent"}:
            continue
        agent_dispatch = sess.get("agent_dispatch") or {}
        if not isinstance(agent_dispatch, dict):
            continue
        if not agent_dispatch.get("session_id"):
            continue
        in_flight[card_id] = sess
    return in_flight


def collect_active_session_ids(state: CoordinareState) -> set[str]:
    """Issue #489: the keep-set for the kubernetes orphan sweep — every
    session id the daemon still owns. Mirrors the FR-005 Docker sweep's
    keep-set exactly: in-flight sessions (dispatching / monitoring phases
    with a non-empty ``agent_dispatch.session_id``) plus live documenting
    sides."""
    active: set[str] = set()
    for sess in _collect_in_flight_sessions(state).values():
        dispatch = sess.get("agent_dispatch") or {}
        if isinstance(dispatch, dict) and dispatch.get("session_id"):
            active.add(str(dispatch["session_id"]))
    sessions = state.get("active_sessions") or {}
    if isinstance(sessions, dict):
        for sess in sessions.values():
            if not isinstance(sess, dict):
                continue
            side = sess.get("documenting_side")
            if (
                isinstance(side, dict)
                and side.get("session_id")
                and (side.get("status") == "running" or side.get("writer_active"))
            ):
                active.add(str(side["session_id"]))
    return active


def find_kubernetes_sweep_runtime(state: CoordinareState) -> Any:
    """Return the performer-pool runtime that can sweep kubernetes pods, or
    None when no service in the pool carries one (docker / subprocess
    deployments) — the duck-typed transport gate. Scans the ID-keyed HTTP
    registry first: in mixed pools the stage default may be a legacy
    subprocess service while the kubernetes services are keyed by
    performer id."""
    by_id = state.get("performer_services_by_id") or {}
    if isinstance(by_id, dict):
        for service in by_id.values():
            runtime = getattr(service, "_runtime", None)
            if runtime is None:
                continue
            if callable(getattr(runtime, "sweep_orphaned_sessions", None)):
                return runtime
    services = state.get("performer_services") or {}
    if not isinstance(services, dict):
        return None
    for service in services.values():
        runtime = getattr(service, "_runtime", None)
        if runtime is None:
            continue
        if callable(getattr(runtime, "sweep_orphaned_sessions", None)):
            return runtime
    return None


async def run_kubernetes_orphan_sweep(state: CoordinareState) -> list[str]:
    """Issue #489: delete managed pods whose ``coordinare.session_id`` is not
    among the daemon's active sessions. Kubernetes counterpart of the FR-005
    Docker orphan sweep, driven through the same startup-reconciliation
    seam. Returns the swept pod names; raises propagate to the caller's
    crash-blocker."""
    runtime = find_kubernetes_sweep_runtime(state)
    if runtime is None:
        return []
    active_ids = collect_active_session_ids(state)
    # SC-002 fast path, same rationale as the Docker sweep: with no
    # authoritative in-flight set we cannot tell a true orphan from
    # legitimate work, so sweep nothing.
    if not active_ids:
        return []
    swept = await runtime.sweep_orphaned_sessions(active_ids)
    if swept:
        logger.info(
            "daemon.kubernetes_orphan_sweep_complete",
            swept_count=len(swept),
            swept_pods=list(swept),
        )
    return list(swept)


async def _restore_documenting_side(state, card_id, sess, by_session, docker_executor) -> None:
    """Adopt an early writer, or confirm it stopped before releasing its lock."""
    if sess.get("board_paused"):
        # The confirmed owning-service stop path controls paused identities.
        return
    service = _resolve_service(state, "documenting")
    if service is None or _is_persistent_mode(service):
        return
    if not isinstance(getattr(service, "_runtime", None), DockerRuntime):
        # Docker enumeration cannot establish absence of a Kubernetes writer.
        return
    session_id = str(sess["documenting_side"]["session_id"])
    container = by_session.get(session_id)
    if container is None:
        record_result(sess, status="failed", head_sha=None, reason="side writer absent at restart")
        return
    job_id = sess["documenting_side"].get("job_id")
    if await _probe_job_runner_health(container, docker_executor):
        adopted = await _adopt(
            state, card_id, container, session_id, service, docker_executor, dispatch={"job_id": job_id},
        )
        if adopted:
            sess["documenting_side"]["job_id"] = service._active_jobs[session_id].job_id
        if adopted is None or adopted:
            return
    stopped = await docker_executor.stop_container(container.container_id, timeout=5.0)
    record_result(sess, status="failed", head_sha=None,
                  reason="side writer stopped at restart" if stopped else "side writer stop unconfirmed at restart",
                  writer_active=not stopped)


async def recover_documenting_side_session(card_id: str, sess: dict[str, Any], service: Any) -> bool:
    """Recover an existing writer before polling; uncertainty keeps its lock."""
    side = sess.get("documenting_side") or {}
    session_id = str(side.get("session_id") or "")
    live = getattr(service, "has_live_session", None)
    if not _is_ephemeral_container_service(service) or (callable(live) and live(session_id)):
        return True
    runtime = getattr(service, "_runtime", None)
    try:
        if isinstance(runtime, KubernetesRuntime):
            temporary = {"agent_dispatch": {"session_id": session_id, "job_id": side.get("job_id")}}
            decision = await _reconcile_kubernetes_session(temporary, card_id, service)
            if decision == ReconciliationDecision.ADOPTED:
                side["job_id"] = temporary["agent_dispatch"]["job_id"]
            elif decision != ReconciliationDecision.DEFERRED:
                record_result(sess, status="failed", head_sha=None, reason="side writer absent after recovery")
        else:
            if not isinstance(runtime, DockerRuntime):
                return False
            executor = DockerExecutor()
            containers = await executor.list_containers_by_label({"coordinare.session_id": session_id}, timeout=5.0)
            if len(containers) > 1:
                return False
            await _restore_documenting_side(
                {"performer_services": {"documenting": service}}, card_id, sess,
                {session_id: containers[0]} if containers else {}, executor,
            )
    except Exception as exc:
        logger.warning("daemon.side_reconciliation_deferred", card_id=card_id, error_type=type(exc).__name__)
    return bool(callable(live) and live(session_id))


async def _classify_and_act(
    state: CoordinareState,
    card_id: str,
    sess: dict[str, Any],
    by_session: dict[str, ContainerInfo],
    docker_executor: DockerExecutor,
) -> ReconciliationDecision:
    """Classify a single in-flight card and apply the matching decision."""
    if sess.get("board_paused"):
        # Legacy reap-and-replace clears IDs even if the runtime stop fails.
        # A paused owner must remain visible until stop_owned_writers confirms
        # absence; neither adoption nor replacement is requested here.
        return ReconciliationDecision.DEFERRED
    return await _classify_active_session(state, card_id, sess, by_session, docker_executor)


async def _classify_active_session(
    state: CoordinareState, card_id: str, sess: dict[str, Any],
    by_session: dict[str, ContainerInfo], docker_executor: DockerExecutor,
) -> ReconciliationDecision:
    """Apply the existing adoption/replacement policy only to active owners."""
    agent_dispatch = sess.get("agent_dispatch") or {}
    session_id = agent_dispatch.get("session_id") if isinstance(agent_dispatch, dict) else None
    performer_stage = sess.get("performer_stage")

    # FR-011: persistent-mode services have no per-job container.
    service = _resolve_service(state, performer_stage, agent_dispatch.get("performer_id")) if performer_stage else None
    if not _is_ephemeral_container_service(service):
        return (ReconciliationDecision.SKIPPED_PERSISTENT
                if _is_persistent_mode(service) else ReconciliationDecision.DEFERRED)

    kubernetes_service = _kubernetes_service(state, sess)
    if kubernetes_service is not None:
        return await _reconcile_kubernetes_session(sess, card_id, kubernetes_service)

    container = by_session.get(session_id) if isinstance(session_id, str) else None
    if container is None:
        # No matching container → just fresh-dispatch by clearing
        # agent_dispatch.  Don't emit reap_failed — there's nothing to
        # reap.
        return _clear_agent_dispatch_and_return(
            sess, ReconciliationDecision.FRESH_DISPATCHED, card_id,
        )

    # We have a match.  Probe the container's job-runner for health
    # before deciding adopt vs reap-and-replace.
    is_healthy = await _probe_job_runner_health(container, docker_executor)
    if is_healthy:
        adopted = await _adopt(
            state, card_id, container, session_id, service, docker_executor, dispatch=agent_dispatch,
        )
        if adopted is None:
            return ReconciliationDecision.DEFERRED
        if adopted:
            return ReconciliationDecision.ADOPTED
        # Adoption failed (port unresolvable etc.) → fall through to
        # reap-and-replace rather than leaving a dead client in
        # _active_jobs.

    # Container exists but unreachable → reap+replace
    await _reap_and_replace(container, sess, card_id, docker_executor)
    return ReconciliationDecision.REAPED_AND_REPLACED


def _requires_docker(state: CoordinareState, sess: dict[str, Any]) -> bool:
    dispatch = sess.get("agent_dispatch") or {}
    service = _resolve_service(state, str(sess.get("performer_stage") or ""), dispatch.get("performer_id"))
    return _is_ephemeral_container_service(service) and _kubernetes_service(state, sess) is None


def _is_ephemeral_container_service(service: Any) -> bool:
    """Only configured ephemeral HTTP performers have recoverable containers."""
    return getattr(getattr(service, "_config", None), "mode", None) == "ephemeral"


def _kubernetes_service(state: CoordinareState, sess: dict[str, Any]) -> Any:
    dispatch = sess.get("agent_dispatch") or {}
    service = _resolve_service(state, str(sess.get("performer_stage") or ""), dispatch.get("performer_id"))
    return service if isinstance(getattr(service, "_runtime", None), KubernetesRuntime) else None


async def _reconcile_kubernetes_session(
    sess: dict[str, Any], card_id: str, service: Any,
) -> ReconciliationDecision:
    """Adopt or reap a known owned pod; unreadable state never authorizes replacement."""
    dispatch = sess.get("agent_dispatch") or {}
    session_id = str(dispatch.get("session_id") or "")
    if not session_id:
        return _clear_agent_dispatch_and_return(sess, ReconciliationDecision.FRESH_DISPATCHED, card_id)
    runtime = service._runtime
    client = None
    try:
        started = await runtime.find_session(session_id)
        if started is None:
            return _clear_agent_dispatch_and_return(sess, ReconciliationDecision.FRESH_DISPATCHED, card_id)
        healthy = False
        runner_status: Any = None
        if started.endpoint:
            token_fn = getattr(service, "_auth_token", None)
            client = PerformerHTTPClient(
                started.endpoint, auth_token=token_fn() if callable(token_fn) else None, timeout_seconds=5.0,
            )
            try:
                runner_status = await client.get_status()
                healthy = True
            except (PerformerAuthError, TransportTimeoutError):
                raise  # authentication or timing uncertainty must not destroy live work
            except Exception:
                healthy = False
        if healthy:
            job_id = _require_runner_job_id(dispatch, runner_status)
            service._active_jobs[session_id] = _EphemeralJob(
                container_id=started.handle, endpoint=started.endpoint, client=client, job_id=job_id,
            )
            dispatch["job_id"] = job_id
            client = None  # adopted service owns the client now
            sess["phase"] = "monitoring_performer"
            return ReconciliationDecision.ADOPTED
        await runtime.stop(started.handle)
        if await runtime.find_session(session_id) is not None:
            return ReconciliationDecision.DEFERRED
        return _clear_agent_dispatch_and_return(sess, ReconciliationDecision.REAPED_AND_REPLACED, card_id)
    except Exception as exc:
        logger.warning("daemon.kubernetes_reconciliation_deferred", card_id=card_id, error_type=type(exc).__name__)
        return ReconciliationDecision.DEFERRED
    finally:
        if client is not None:
            await client.aclose()


def _require_runner_job_id(dispatch: dict[str, Any], status: Any) -> str:
    job_id = _runner_job_id(dispatch, status)
    if not job_id:
        raise RuntimeError(_UNKNOWN_RUNNER_JOB)
    return job_id


def _runner_job_id(dispatch: dict[str, Any], status: Any) -> str | None:
    saved = dispatch.get("job_id")
    current = getattr(status, "current_job_id", None)
    saved = saved.strip() if isinstance(saved, str) else None
    current = current.strip() if isinstance(current, str) else None
    if saved and current and saved != current:
        return None
    return saved or current or None


def _resolve_service(state: CoordinareState, performer_stage: str, performer_id: str | None = None) -> Any:
    by_id = state.get("performer_services_by_id") or {}
    if performer_id and isinstance(by_id, dict) and performer_id in by_id:
        return by_id[performer_id]
    perf_services = state.get("performer_services") or {}
    if isinstance(perf_services, dict):
        return perf_services.get(performer_stage)
    return None


def _is_persistent_mode(service: Any) -> bool:
    """FR-011: check whether a service is configured for persistent mode."""
    config = getattr(service, "_config", None)
    mode = getattr(config, "mode", None) if config is not None else None
    return mode == "persistent"


def _clear_agent_dispatch_and_return(
    sess: dict[str, Any],
    decision: ReconciliationDecision,
    card_id: str,
) -> ReconciliationDecision:
    sess["agent_dispatch"] = {}
    sess["agent_dispatch_at"] = None
    sess["phase"] = "dispatching"
    sess["reconciled_dispatch_pending"] = True
    return decision


async def _adopt(
    state: CoordinareState,
    card_id: str,
    container: ContainerInfo,
    session_id: str,
    service: Any,
    docker_executor: DockerExecutor,
    *,
    dispatch: dict[str, Any] | None = None,
) -> bool | None:
    """FR-003: register an existing container into the new process's
    ``_active_jobs`` so the orchestrator resumes monitoring instead of
    spawning a duplicate.

    The performer's job-runner is reachable (we already probed); we
    resolve the container's REAL host port via ``docker port`` and
    construct a fresh ``_EphemeralJob`` and ``PerformerHTTPClient``
    pointed at the actual endpoint.

    Returns None when runner identity is unreadable; preserve the session.
    Returns True on successful adoption.  Returns False if the host
    port cannot be resolved (in which case the caller falls through
    to reap-and-replace — better than leaving a dead client in
    ``_active_jobs`` that subsequent status polls would silently fail).
    """
    # Lazy-import to avoid module-load cycles; these are heavy modules.
    from coordinare.services.http_performer_service import _EphemeralJob
    from coordinare.transport.http_transport import PerformerHTTPClient

    # CRITICAL: resolve the container's real host port BEFORE building
    # the client.  Without this, the client would point at the
    # placeholder ``http://127.0.0.1:0`` and every subsequent monitor
    # call would silently fail.
    port = await docker_executor.port_of(container.container_id, internal_port=8088)
    if port is None or port == 0:
        logger.warning(
            "daemon.adopt_failed_no_port_resolved",
            card_id=card_id,
            container_id=container.container_id,
        )
        return False
    endpoint = f"http://127.0.0.1:{port}"

    auth_token = getattr(service, "_auth_token", None)
    token = auth_token() if callable(auth_token) else None
    client = PerformerHTTPClient(endpoint, auth_token=token)
    dispatch = dispatch if dispatch is not None else {}
    adopted = False
    try:
        job_id = _runner_job_id(dispatch, await client.get_status())
        if not job_id:
            return None
        service._active_jobs[session_id] = _EphemeralJob(
            container_id=container.container_id, endpoint=endpoint, client=client, job_id=job_id,
        )
        dispatch["job_id"] = job_id
        adopted = True
    except Exception as exc:
        logger.warning("daemon.adopt_job_identity_deferred", card_id=card_id, error_type=type(exc).__name__)
        return None
    finally:
        if not adopted:
            await client.aclose()
    logger.info(
        "daemon.session_adopted",
        card_id=card_id,
        session_id=session_id,
        container_id=container.container_id,
        endpoint=endpoint,
    )
    return True


async def _reap_and_replace(
    container: ContainerInfo,
    sess: dict[str, Any],
    card_id: str,
    docker_executor: DockerExecutor,
) -> None:
    """FR-004: stop a container that matched by session but whose
    job-runner is unreachable; clear ``agent_dispatch`` so the next graph
    cycle fresh-dispatches.

    Stop is best-effort: if it fails, emit ``daemon.reap_failed`` and
    proceed (the daemon must not wedge on a sticky orphan).
    """
    stopped = await docker_executor.stop_container(
        container.container_id, timeout=5.0,
    )
    if not stopped:
        logger.warning(
            "daemon.reap_failed",
            container_id=container.container_id,
            card_id=card_id,
            reason="reap_and_replace_stop_failed",
        )
    sess["agent_dispatch"] = {}
    sess["agent_dispatch_at"] = None
    sess["phase"] = "dispatching"
    sess["reconciled_dispatch_pending"] = True


async def _sweep_orphan(
    container: ContainerInfo,
    docker_executor: DockerExecutor,
) -> bool:
    """FR-005: stop a true orphan container (one with no matching
    session in the snapshot).  Returns True on success."""
    stopped = await docker_executor.stop_container(container.container_id, timeout=5.0)
    logger.info(
        "daemon.orphan_swept",
        container_id=container.container_id,
        name=container.name,
        image=container.image,
        started_at=container.started_at.isoformat(),
        labels=dict(container.labels),
        stopped=stopped,
    )
    if not stopped:
        logger.warning(
            "daemon.reap_failed",
            container_id=container.container_id,
            reason="orphan_sweep_failed",
        )
    return stopped


async def _probe_job_runner_health(
    container: ContainerInfo,
    docker_executor: DockerExecutor,
    *,
    timeout: float = 15.0,
) -> bool:
    """076 (T054): probe the container's job-runner HTTP endpoint.

    Delegates to ``docker_executor.probe_healthz`` so test doubles can
    return canned answers without spinning up a real HTTP server.
    Returns True on HTTP 200 within ``timeout``; False on any failure.
    """
    return await docker_executor.probe_healthz(container.container_id, timeout=timeout)


def _retire_terminal_board_session(state: CoordinareState, card_id: str, board_status: str) -> dict[str, Any] | None:
    """Clear only the terminal card's mirrors after its ownership guard passed."""
    retired = (state.get("active_sessions") or {}).get(card_id)
    if not state.get("active_card_id"):
        state["active_card_id"] = card_id
    state["phase"] = "idle"
    _retire_active_session(state, trigger="board_card_done_reconciled" if board_status else "board_card_missing_reconciled")
    state["active_card"] = None
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return retired


def reconcile_board_state(
    state: CoordinareState,
    board_snapshot: dict[str, Any],
    *, allow_paused_missing: bool = False,
) -> dict[str, Any]:
    """076 (T120, FR-025): per-cycle board ↔ local-state reconciliation.

    Compares ``state.active_card.status`` to the board's Status for the
    same card.  When they disagree, releases the pin (consistent with
    the FR-020 default per clarification Q1) so the next eligibility
    pass can re-pick the card from the correct queue.

    Issue #516: a card the board places in DONE is terminal regardless
    of divergence — the matching session is retired (not just the pin)
    so a zombie session cannot hold the pickup slot across restarts.

    Paused missing sessions retire only when ``allow_paused_missing`` confirms
    a fresh, successful board read and all owned writers have stopped.

    Returns ``{"action": "agreed" | "pin_released" | "done_session_retired" | "missing_session_retired" |
    "no_active_card" | "deferred", "card_id": str | None, "local": str,
    "board": str}``; for ``done_session_retired`` the result additionally
    carries ``retired_session`` (the removed session record, or None when
    no record existed) so the caller can release its performer resources.
    """
    active_card = state.get("active_card") or state.get("current_card")
    card_id = str(active_card.get("id", "")) if isinstance(active_card, dict) else ""
    if not isinstance(active_card, dict) or not card_id:
        return {"action": "no_active_card", "card_id": None, "local": "", "board": ""}

    local_status = str(active_card.get("status") or "")

    # Find which column the board snapshot places the card in
    board_status = ""
    if isinstance(board_snapshot, dict):
        for column in ("IN_PROGRESS", "IN_REVIEW", "TODO", "BLOCKED", "DONE", "BACKLOG"):
            ids = board_snapshot.get(column) or []
            if isinstance(ids, list) and card_id in ids:
                board_status = column
                break

    # Paused owners must reach confirmed absence before DONE or missing can retire
    # them. Normal, nonpaused DONE retains its existing best-effort cleanup.
    sessions = state.get("active_sessions") or {}
    paused = bool((sessions.get(card_id) or {}).get("board_paused"))
    if paused and (
        board_status not in {"DONE", ""} or has_owned_writers(sessions.get(card_id) or {})
        or (not board_status and not allow_paused_missing)
    ):
        return {"action": "deferred", "card_id": card_id, "local": "", "board": "paused"}

    # Normalise both sides before comparing — the board column names are
    # UPPER_SNAKE ("IN_PROGRESS") but ``card.status`` from various code
    # paths may be human-readable ("In Progress") or display-form.
    def _norm(s: str) -> str:
        return s.strip().upper().replace(" ", "_").replace("-", "_")

    # Issue #516: a card in DONE is terminal — its work is finished on the
    # board no matter what the local pin or phase thinks.  The monitor's own
    # Done path only runs for phases in PHASE_TO_EXPECTED_COLUMN (no "idle"),
    # and this check must also precede the agreed-status short-circuit so a
    # restart between "status write" and "session teardown" (local=DONE +
    # board=DONE + live session) cannot re-wedge.  Retire whatever session
    # still pins the card and free the pickup slot within this cycle.

    action = ""
    board_label = board_status
    retired_session: dict[str, Any] | None = None
    if board_status == "DONE" or (paused and not board_status):
        action = "done_session_retired" if board_status else "missing_session_retired"
        board_label = board_status or "missing"
        logger.warning(
            "daemon.board_state_reconciled", card_id=card_id,
            local_status=local_status, board_status=board_label, action=action,
        )
        retired_session = _retire_terminal_board_session(state, card_id, board_status)
    elif not board_status:
        # Card is no longer on any tracked column — operator may have
        # deleted or archived it.  Release the pin.
        board_label = "missing"
        action = "pin_released"
        logger.warning(
            "daemon.board_state_reconciled",
            card_id=card_id,
            local_status=local_status,
            board_status=board_label,
            action=action,
        )
        state["active_card"] = None
        state["current_card"] = None
        state["active_card_id"] = None
    elif _norm(board_status) == _norm(local_status):
        action = "agreed"
    elif _norm(board_status) in ("TODO", "BACKLOG") and _norm(local_status) in (
        "IN_PROGRESS",
        "IN_REVIEW",
        "BLOCKED",
    ):
        # Today's incident pattern (FR-025 narrow scope): the board has the
        # card BACK in TODO / BACKLOG but local state still pins it as
        # IN_PROGRESS / IN_REVIEW / BLOCKED.  Release the pin so eligibility
        # can re-pick.
        #
        # Other forms of divergence (e.g., local=IN_PROGRESS, board=IN_REVIEW
        # meaning the card legitimately advanced during downtime) are handled
        # by the EXISTING ``daemon._reconcile_with_board`` at startup.  We
        # do NOT interfere with that path — only the wedge-shape divergence.
        action = "pin_released"
        logger.warning(
            "daemon.board_state_reconciled",
            card_id=card_id,
            local_status=local_status,
            board_status=board_status,
            action=action,
        )
        state["active_card"] = None
        state["current_card"] = None
        state["active_card_id"] = None
    else:
        # Other divergences are deferred to the existing startup-time
        # reconciliation (or to a future spec); log for audit and proceed.
        action = "deferred"
        logger.info(
            "daemon.board_state_diverged_deferred",
            card_id=card_id,
            local_status=local_status,
            board_status=board_status,
        )
    result = {
        "action": action,
        "card_id": card_id,
        "local": local_status,
        "board": board_label,
    }
    if action in {"done_session_retired", "missing_session_retired"}:
        # Hand the retiring session back so the caller can best-effort
        # release its performer resources before the record is dropped.
        result["retired_session"] = retired_session
    return result


__all__ = [
    "ReconciliationDecision",
    "ReconciliationReport",
    "WedgeResolution",
    "detect_wedged_state",
    "handle_potentially_stale_session",
    "reconcile_board_state",
    "run_startup_reconciliation",
]
