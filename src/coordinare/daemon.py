from __future__ import annotations

import asyncio
import contextlib
import copy
import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)
from coordinare.graph.state import CoordinareState, initial_state
from coordinare.lib.runtime_events import build_runtime_event
from coordinare.metrics import METRICS
from coordinare.models.dependency import DependencyStatus
from coordinare.observability import HEALTH, HealthStatus, bind_cycle_id, clear_cycle_id
from coordinare.resilience import CircuitOpenError
from coordinare.services.dependency import build_graph as _build_dep_graph
from coordinare.services.rebase import fetch_main_sha, repo_url_from_config, run_rebase_round
from coordinare.session import _SESSION_FIELDS, session_to_state, state_to_session
from coordinare.state_store import StateLoadError, WorkflowPhase, WorkflowSnapshot

if TYPE_CHECKING:
    from coordinare.dashboard import DashboardStore
    from coordinare.models.dependency import DependencyGraph
    from coordinare.state_store import StateStore

logger = structlog.get_logger(__name__)


@dataclass
class SessionEligibility:
    """Per-cycle eligibility verdict for one active session."""

    card_id: str
    eligible: bool
    reason: str  # "eligible" | "blocked_column" | "dependency_blocked" | "missing_card"
    blockers: list[int] = field(default_factory=list)


@dataclass
class AsyncSessionTickResult:
    """Result of one concurrent session invocation."""

    card_id: str
    ok: bool
    session_state: dict
    skipped: bool = False
    error: str | None = None
    duration_ms: int = 0
    global_updates: dict[str, Any] | None = None


_PHASE_PRIORITY: dict[str, int] = {
    "system_error": 9,
    "blocked": 8,
    "recovery": 7,
    "merging": 6,
    "relay_feedback": 5,
    "dispatching": 4,
    "monitoring_performer": 3,
    "monitoring_pr": 2,
    "monitoring_agent": 1,
    "idle": 0,
}


def _derive_global_phase(active_sessions: dict) -> str:
    """Derive the global daemon phase from the highest-priority session phase."""
    if not active_sessions:
        return "idle"
    best = "idle"
    best_pri = 0
    for sess in active_sessions.values():
        p = str(sess.get("phase") or "idle")
        pri = _PHASE_PRIORITY.get(p, 0)
        if pri > best_pri:
            best = p
            best_pri = pri
    return best


# Global state keys mutated by graph nodes (e.g. check_board) that are shared
# across all sessions in a cycle.  After the concurrent fanout, these are merged
# back into self._state from the first successful result so they're not lost.
_GLOBAL_STATE_KEYS: tuple[str, ...] = (
    "last_known_main_sha",
    "last_rebase_round",
    "last_poll_at",
    "github_retry_queue",
    "github_retry_after",
    # advocate_history is union-merged across all session results below so
    # no issue processed by any concurrent tick is re-scanned next cycle.
    "advocate_history",
    # phase is NOT included here; it is derived explicitly from active_sessions
    # after the merge loop to avoid misreporting the daemon as idle when only
    # the first completed session had phase="idle" while others are still active.
)


def _compute_eligibility(
    card_id: str,
    session: dict,
    board_snapshot: dict[str, list[str]],
    dep_graph: DependencyGraph | None,
) -> SessionEligibility:
    """Derive per-cycle eligibility for a session from board state."""
    card = session.get("current_card") or {}
    if not card:
        return SessionEligibility(card_id=card_id, eligible=False, reason="missing_card")
    card_item_id = str(card.get("content_id") or card.get("id") or "")
    if not card_item_id:
        return SessionEligibility(card_id=card_id, eligible=False, reason="missing_card")

    blocked_cards = board_snapshot.get("BLOCKED", [])
    if card_item_id in blocked_cards:
        return SessionEligibility(card_id=card_id, eligible=False, reason="blocked_column")

    if dep_graph is not None:
        deps = dep_graph.by_dependent.get(card_item_id, [])
        unresolved = [d for d in deps if d.status != DependencyStatus.SATISFIED]
        if unresolved:
            return SessionEligibility(
                card_id=card_id,
                eligible=False,
                reason="dependency_blocked",
                blockers=[d.blocker_issue_number for d in unresolved],
            )

    return SessionEligibility(card_id=card_id, eligible=True, reason="eligible")


# Maps (previous_phase, current_phase) tuples to canonical metric transition labels.
# Transitions not listed here are not recorded (e.g. recovery→idle, relay_feedback→*).
_PHASE_TRANSITION_METRIC: dict[tuple[str, str], str] = {
    ("idle", "dispatching"): "idle_to_dispatch",
    ("dispatching", "monitoring_agent"): "dispatch_to_monitor",
    ("dispatching", "monitoring_pr"): "dispatch_to_monitor",
    ("monitoring_agent", "merging"): "monitor_to_merge",
    ("monitoring_pr", "merging"): "monitor_to_merge",
    ("monitoring_agent", "blocked"): "monitor_to_blocked",
    ("monitoring_pr", "blocked"): "monitor_to_blocked",
    ("blocked", "idle"): "blocked_to_idle",
}

# Maps circuit-breaker service names to the HEALTH subsystem they represent.
# Services not listed here are not registered as health probes and are skipped.
# When the circuit is open and poll=0 (webhook-only mode), the daemon cannot
# rely on _wait_for_next_cycle() for recovery — no webhook will arrive if
# GitHub is down. Use a fixed backoff so the circuit can probe-recover.
_CIRCUIT_OPEN_BACKOFF_SECONDS: int = 60

_CIRCUIT_TO_HEALTH_SUBSYSTEM: dict[str, str] = {
    "github": "github",
    "agent": "agent",
    "agent_ssh": "agent",
}


class RuntimeExecutionError(RuntimeError):
    def __init__(self, *, phase: str, step: str, cause: Exception) -> None:
        super().__init__(f"{phase} failure in {step}: {cause}")
        self.phase = phase
        self.step = step
        self.cause = cause


class CoordinareDaemon:
    def __init__(
        self,
        graph: Any,
        *,
        run_mode: str = "shell",
        poll_interval_seconds: int = 30,
        heartbeat_interval_seconds: int = 30,
        max_cycles: int | None = None,
        sleep_func: Any = asyncio.sleep,
        state_store: StateStore | None = None,
        idle_threshold_seconds: int = 1800,
        dashboard_store: DashboardStore | None = None,
        webhook_trigger: asyncio.Event | None = None,
    ) -> None:
        self._graph = graph
        self._run_mode = run_mode
        self._poll_interval_seconds = poll_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._max_cycles = max_cycles
        self._sleep = sleep_func
        self._running = False
        self._stop_event = asyncio.Event()
        self._webhook_trigger: asyncio.Event = webhook_trigger or asyncio.Event()
        self._state: CoordinareState = initial_state()
        self._cycle_active = False
        self._stop_during_cycle = False
        self._state_store = state_store
        self._idle_threshold_seconds = idle_threshold_seconds
        self._dashboard_store = dashboard_store
        self._main_task: asyncio.Task[None] | None = None
        self._config_reload_trigger: asyncio.Event = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def state(self) -> CoordinareState:
        return self._state

    @property
    def state_store(self) -> StateStore | None:
        return self._state_store

    def _build_snapshot(self) -> WorkflowSnapshot:
        card = self._state.get("current_card")
        card_dict = card if isinstance(card, dict) else {}
        dispatch = self._state.get("agent_dispatch")
        dispatch_dict = dispatch if isinstance(dispatch, dict) else {}
        raw_questions = self._state.get("open_questions")
        questions = [str(q) for q in raw_questions] if isinstance(raw_questions, list) else []
        raw_clarifications = self._state.get("card_clarifications")
        clarifications = list(raw_clarifications) if isinstance(raw_clarifications, list) else []
        last_notified = self._state.get("last_blocked_notified_at")
        performer_stage = self._state.get("performer_stage")
        lifecycle_sequence = self._state.get("lifecycle_sequence")

        # Coerce a value to a non-empty string or None.  Critical: ``str(None)``
        # returns the literal string ``"None"`` which then survives the
        # ``or None`` guard because it's truthy — that bug previously wrote
        # ``"pr_url": "None"`` into the snapshot, breaking PR lookups on
        # restart.  Convert None/empty to None FIRST, then stringify.
        def _str_or_none(value: Any) -> str | None:
            if value is None or value == "":
                return None
            return str(value)

        # 045: Persist issue_number/url/description/acceptance_criteria so
        # restore after a restart doesn't dispatch with issue_number=0 (which
        # caused PRs to open without ``Closes #N`` linkage).
        raw_issue_number = card_dict.get("issue_number") if card_dict else None
        issue_number = raw_issue_number if isinstance(raw_issue_number, int) and raw_issue_number > 0 else None
        raw_ac = card_dict.get("acceptance_criteria") if card_dict else None
        acceptance_criteria = [str(c) for c in raw_ac] if isinstance(raw_ac, list) else []

        return WorkflowSnapshot(
            snapshot_at=datetime.now(UTC),
            phase=self._state.get("phase", "idle"),
            active_card_id=_str_or_none(card_dict.get("id")) if card_dict else None,
            active_card_title=_str_or_none(card_dict.get("title")) if card_dict else None,
            active_card_column=_str_or_none(card_dict.get("status")) if card_dict else None,
            active_card_issue_id=_str_or_none(card_dict.get("issue_id")) if card_dict else None,
            active_card_issue_number=issue_number,
            active_card_issue_url=_str_or_none(card_dict.get("issue_url")) if card_dict else None,
            active_card_description=_str_or_none(card_dict.get("description")) if card_dict else None,
            active_card_acceptance_criteria=acceptance_criteria,
            pr_url=_str_or_none(card_dict.get("pr_url")) if card_dict else None,
            pr_node_id=_str_or_none(card_dict.get("pr_node_id")) if card_dict else None,
            agent_session_id=_str_or_none(dispatch_dict.get("session_id")) if dispatch_dict else None,
            open_questions=questions,
            card_clarifications=clarifications,
            performer_stage=str(performer_stage) if isinstance(performer_stage, str) and performer_stage else None,
            lifecycle_sequence=[
                str(stage) for stage in lifecycle_sequence
            ] if isinstance(lifecycle_sequence, list) else [],
            last_blocked_notified_at=last_notified if isinstance(last_notified, datetime) else None,
            lifecycle_completed_at=self._state.get("lifecycle_completed_at") if isinstance(self._state.get("lifecycle_completed_at"), datetime) else None,
            processed_review_ids=sorted(self._state.get("processed_review_ids") or set()),
        )

    def _restore_from_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self._state["phase"] = snapshot.phase
        self._state["open_questions"] = list(snapshot.open_questions)
        self._state["card_clarifications"] = list(snapshot.card_clarifications)
        if snapshot.lifecycle_sequence:
            self._state["lifecycle_sequence"] = list(snapshot.lifecycle_sequence)
        if snapshot.performer_stage:
            self._state["performer_stage"] = snapshot.performer_stage
        self._state["last_blocked_notified_at"] = snapshot.last_blocked_notified_at
        self._state["lifecycle_completed_at"] = snapshot.lifecycle_completed_at
        self._state["processed_review_ids"] = set(snapshot.processed_review_ids)
        if snapshot.active_card_id:
            self._state["current_card"] = {
                "id": snapshot.active_card_id,
                "issue_id": snapshot.active_card_issue_id or "",
                "issue_number": snapshot.active_card_issue_number or 0,
                "issue_url": snapshot.active_card_issue_url or "",
                "title": snapshot.active_card_title or "",
                "description": snapshot.active_card_description or "",
                "acceptance_criteria": list(snapshot.active_card_acceptance_criteria),
                "status": snapshot.active_card_column or "",
                "pr_url": snapshot.pr_url,
                "pr_node_id": snapshot.pr_node_id,
            }
        if snapshot.agent_session_id:
            self._state["agent_dispatch"] = {"session_id": snapshot.agent_session_id}

    @staticmethod
    def _infer_phase_from_board_column(column: str) -> WorkflowPhase:
        normalized = column.strip().lower()
        if normalized in {"in progress", "in_progress"}:
            return "monitoring_agent"
        if normalized in {"in review", "in_review"}:
            return "monitoring_pr"
        if normalized == "blocked":
            return "blocked"
        return "idle"

    async def _reconcile_with_board(self, snapshot: WorkflowSnapshot) -> None:
        """Query the live board and reconcile restored state against it."""
        github = self._state.get("github_service")
        if github is None:
            return
        try:
            board = await github.poll_board()
            board_snapshot = board.get("snapshot", {})
            found_column: str | None = None
            for column, card_ids in board_snapshot.items():
                if isinstance(card_ids, list) and snapshot.active_card_id in card_ids:
                    found_column = column
                    break

            if found_column is None or found_column.upper() == "DONE":
                logger.warning(
                    "board_contradicts_snapshot",
                    active_card_id=snapshot.active_card_id,
                    found_column=found_column,
                )
                self._state["phase"] = "idle"
                self._state["current_card"] = None
            else:
                inferred = self._infer_phase_from_board_column(found_column)
                if inferred != snapshot.phase:
                    logger.info(
                        "board_reconciliation_advanced",
                        active_card_id=snapshot.active_card_id,
                        snapshot_phase=snapshot.phase,
                        board_column=found_column,
                        inferred_phase=inferred,
                    )
                    self._state["phase"] = inferred
                else:
                    logger.info(
                        "board_reconciliation_confirmed",
                        active_card_id=snapshot.active_card_id,
                        phase=snapshot.phase,
                    )
        except Exception as exc:
            logger.warning(
                "board_reconciliation_skipped",
                error=str(exc),
            )

    async def _wait_for_next_cycle(self) -> None:
        """Wait for the next polling cycle, honouring webhook triggers and poll=0 mode."""
        poll = self._poll_interval_seconds
        if poll > 0:
            # Race the poll sleep against a webhook trigger so either can wake the loop.
            # Using self._sleep makes this injectable/mockable in tests.
            sleep_task = asyncio.ensure_future(self._sleep(poll))
            trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
            done, pending = await asyncio.wait(
                {sleep_task, trigger_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Only consume the trigger if it actually fired; a webhook arriving
            # just as the sleep expires should not be silently discarded.
            if trigger_task in done:
                self._webhook_trigger.clear()
        else:
            # Polling disabled — block until a webhook trigger or stop event fires
            trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
            stop_task = asyncio.ensure_future(self._stop_event.wait())
            done, pending = await asyncio.wait(
                {trigger_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if trigger_task in done:
                self._webhook_trigger.clear()

    def stop(self) -> None:
        if self._cycle_active:
            self._stop_during_cycle = True
        self._running = False
        self._stop_event.set()
        # Cancel the in-progress cycle only when stop() is called from outside
        # start() — i.e. signal handlers or external code.  When called from
        # within start() itself (max_cycles, mid-cycle graph callbacks) the
        # existing break/stop-event logic handles the exit and we must not
        # self-cancel, which would propagate CancelledError to the caller.
        if (
            self._main_task is not None
            and not self._main_task.done()
            and asyncio.current_task() != self._main_task
        ):
            self._main_task.cancel()

    def _max_concurrent_cards(self) -> int:
        """Return the configured concurrency limit (defaults to 1)."""
        config = self._state.get("config")
        if config is not None and hasattr(config, "max_concurrent_cards"):
            return max(1, int(config.max_concurrent_cards))
        return 1

    async def _invoke_multi_session(self) -> None:
        """Process each active session through the graph concurrently.

        Called only when max_concurrent_cards > 1 and there are active
        sessions.  Before dispatching, eligible sessions are filtered from
        ineligible ones (BLOCKED column, unsatisfied dependencies).  Eligible
        sessions are fanned out with asyncio.gather so they run concurrently
        within a single cycle.  Failures are isolated per session.

        The board is polled once in a pre-flight step and cached so concurrent
        sessions don't each trigger a GitHub API call.  The main-SHA cache is
        also pre-seeded so rebase detection fires at most once per cycle.
        """
        active_sessions: dict = self._state.get("active_sessions") or {}

        # 048: Sync SlotManager with current sessions to free stale slots
        # from crashed/expired performers before dispatching new ones.
        slot_mgr = self._state.get("slot_manager")
        if slot_mgr is not None and hasattr(slot_mgr, "sync_from_sessions"):
            slot_mgr.sync_from_sessions(active_sessions)

        self._state["_board_cache"] = None  # type: ignore[typeddict-unknown-key]
        self._state["_main_sha_cache"] = None  # type: ignore[typeddict-unknown-key]

        if not active_sessions:
            # No sessions yet — run one graph cycle to let check_board populate them
            self._state = await self._graph.ainvoke(self._state)
            return

        # Pre-fanout: run advocate_scan exactly once so N concurrent sessions don't
        # each call scan_and_respond independently (duplicate comments/labels/load).
        # The flag signals per-session advocate_scan invocations to be no-ops.
        if self._state.get("advocate_service") is not None:
            from coordinare.graph.nodes.advocate import advocate_scan
            self._state = await advocate_scan(self._state)  # type: ignore[assignment]
        self._state["_advocate_scan_done"] = True  # type: ignore[typeddict-unknown-key]

        # Pre-flight: poll board once so all concurrent sessions share the cache
        # and eligibility can be computed before the fanout.  Respect the same
        # github_operation_ready/backoff state used by check_board so multi-session
        # mode doesn't bypass transient-outage handling.
        github = self._state.get("github_service")
        if github is not None:
            ready, retry_in = github_operation_ready(self._state, "poll_board")
            if not ready:
                logger.info(
                    "multi_session.pre_poll_deferred",
                    retry_in_seconds=round(retry_in, 1),
                )
            else:
                try:
                    board = await github.poll_board()
                    self._state["_board_cache"] = board  # type: ignore[typeddict-unknown-key]
                    clear_deferred_github_operation(self._state, "poll_board")
                    self._state["last_poll_at"] = datetime.now(UTC)
                    snapshot = board.get("snapshot")
                    if isinstance(snapshot, dict):
                        self._state["board_snapshot"] = snapshot
                    # Pre-seed _main_sha_cache so concurrent sessions share one
                    # ls-remote result instead of each making an independent call.
                    # suppress is scoped only to _current_token() — fetch_main_sha
                    # and rebase failures are logged explicitly so they're visible.
                    _config = self._state.get("config")
                    if _config is not None and hasattr(github, "_current_token"):
                        _token = ""
                        with contextlib.suppress(Exception):
                            _token = await github._current_token()
                        if _token:
                            _repo_url = repo_url_from_config(_config)
                            if _repo_url:
                                try:
                                    _sha = await fetch_main_sha(_repo_url, _token)
                                except Exception:
                                    logger.warning("multi_session.preflight.sha_fetch_failed", exc_info=True)
                                    _sha = None
                                if _sha:
                                    self._state["_main_sha_cache"] = _sha  # type: ignore[typeddict-unknown-key]
                                    # Run main-advance detection once in preflight so
                                    # every per-session check_board copy inherits the
                                    # updated last_known_main_sha and skips its own
                                    # run_rebase_round — preventing N parallel rebase
                                    # rounds when main advances with N active sessions.
                                    _prev_sha = self._state.get("last_known_main_sha")
                                    if _prev_sha is None:
                                        self._state["last_known_main_sha"] = _sha
                                    elif _sha != _prev_sha:
                                        logger.info(
                                            "multi_session.preflight.main_head_changed",
                                            old_sha=_prev_sha[:8],
                                            new_sha=_sha[:8],
                                        )
                                        self._state["last_known_main_sha"] = _sha
                                        try:
                                            _rr = await run_rebase_round(
                                                active_sessions,
                                                _sha,
                                                _repo_url,
                                                _token,
                                                notification_service=self._state.get("notification_service"),
                                                github=github,
                                                human_reviewers=self._state.get("human_reviewers"),
                                            )
                                            self._state["last_rebase_round"] = _rr.to_dict()
                                            # Mirror check_board's conflict-resolution
                                            # handoff: route the first BLOCKED job back
                                            # to implementing so relay_feedback fires.
                                            from coordinare.models.rebase import RebaseOutcome
                                            from coordinare.services.rebase import (
                                                prepare_conflict_resolution,
                                            )
                                            for _job in _rr.jobs:
                                                if _job.outcome == RebaseOutcome.BLOCKED:
                                                    _sess = active_sessions.get(_job.card_id)
                                                    if isinstance(_sess, dict):
                                                        prepare_conflict_resolution(
                                                            _job, _sess,
                                                            human_reviewers=self._state.get("human_reviewers"),
                                                        )
                                                    break
                                        except Exception:
                                            logger.warning("multi_session.preflight.rebase_round_failed", exc_info=True)
                except Exception as _poll_exc:
                    logger.warning("multi_session.pre_poll_failed", exc_info=True)
                    if is_transient_github_outage_error(_poll_exc):
                        defer_github_operation(self._state, operation="poll_board", error=_poll_exc)

        # Build dependency graph from the pre-fetched board if available.
        # NOTE: This uses build_graph only — it does not run resolve_off_board_dependencies,
        # so sessions blocked by a now-closed off-board issue may be conservatively skipped
        # this cycle.  The full resolution runs inside each session's check_board tick and
        # will correct the dep state by the following cycle.
        dep_graph: DependencyGraph | None = None
        cached_board = self._state.get("_board_cache")  # type: ignore[misc]
        if cached_board is not None:
            try:
                dep_graph = _build_dep_graph(cached_board)
            except Exception:
                logger.warning("multi_session.dep_graph_failed", exc_info=True)

        board_snapshot: dict[str, list[str]] = self._state.get("board_snapshot") or {}  # type: ignore[assignment]

        # Compute eligibility for all sessions.
        eligibilities: dict[str, SessionEligibility] = {
            card_id: _compute_eligibility(card_id, session, board_snapshot, dep_graph)
            for card_id, session in active_sessions.items()
        }

        # Record skip reasons for ineligible sessions.
        skip_reasons: dict[str, dict] = {}
        for card_id, elig in eligibilities.items():
            if not elig.eligible:
                skip_reasons[card_id] = {
                    "reason": elig.reason,
                    "detail": None,
                    "blockers": elig.blockers,
                }
                logger.info(
                    "session_skipped",
                    card_id=card_id,
                    reason=elig.reason,
                    blockers=elig.blockers,
                )
        self._state["session_skip_reasons"] = skip_reasons

        # Fallback: if every session is ineligible this cycle (e.g. all BLOCKED /
        # dependency_blocked), run a single full graph invocation so check_board
        # can still pick up new sessions from open slots or do other per-cycle
        # maintenance.  Without this, check_board never fires and available slots
        # go unfilled until at least one existing session becomes eligible.
        if not any(e.eligible for e in eligibilities.values()):
            self._state = await self._graph.ainvoke(self._state)  # type: ignore[assignment]
            self._state.pop("_advocate_scan_done", None)  # type: ignore[misc]
            return

        graph = self._graph
        semaphore = asyncio.Semaphore(self._max_concurrent_cards())

        async def _invoke_one(card_id: str, session: dict) -> AsyncSessionTickResult:
            elig = eligibilities[card_id]
            if not elig.eligible:
                return AsyncSessionTickResult(
                    card_id=card_id, ok=True, session_state=session, skipped=True
                )
            pre_session = dict(session)
            async with semaphore:
                # State prep runs inside the semaphore so the concurrency bound
                # also limits peak memory from simultaneous deep-copies.
                # self._state is stable throughout the fanout (mutated only after
                # all results are merged), so sessions that acquire the semaphore
                # at different times still snapshot the same pre-fanout state.
                t0 = perf_counter()
                try:
                    session_state: dict[str, Any] = dict(self._state)
                    if session_state.get("github_retry_queue") is not None:
                        session_state["github_retry_queue"] = list(session_state["github_retry_queue"])
                    # Deep-copy only the session being invoked (inside the semaphore
                    # so the concurrency bound also limits peak copy memory).
                    # Siblings are shallow-copied from the stable pre-fanout
                    # active_sessions; nodes only mutate top-level sibling keys so
                    # shallow isolation is sufficient.
                    session_state["active_sessions"] = {
                        cid: (copy.deepcopy(sess) if cid == card_id else dict(sess))
                        for cid, sess in active_sessions.items()
                    }
                    session_to_state(session_state["active_sessions"][card_id], session_state)
                    # Snapshot sibling sessions before ainvoke.  Graph nodes such
                    # as prepare_conflict_resolution can mutate session dicts
                    # in-place; capturing shallow copies here lets us detect
                    # real mutations post-ainvoke by value comparison.
                    pre_fanout_siblings: dict[str, dict] = {
                        k: dict(v) for k, v in session_state["active_sessions"].items()
                        if k != card_id
                    }
                    updated = await graph.ainvoke(session_state)
                    updated_session = state_to_session(updated)
                    # Merge in any mutations that nodes made directly to
                    # updated["active_sessions"][card_id] without mirroring them
                    # back onto the flat state fields (e.g. prepare_conflict_resolution
                    # routing a BLOCKED card by writing phase/performer_stage directly
                    # into the session dict).  Only apply an in-place value when the
                    # flat field was NOT independently updated — flat mutations take
                    # priority so that nodes using the canonical flat-field path are
                    # not overwritten by a stale pre-fanout deep copy.
                    _in_place_session = (updated.get("active_sessions") or {}).get(card_id)
                    if _in_place_session:
                        for _f in _SESSION_FIELDS:
                            if _f not in _in_place_session:
                                continue
                            _pre_val = session.get(_f)
                            if updated_session.get(_f) == _pre_val:
                                # flat field unchanged — apply in-place mutation if any
                                _ip_val = _in_place_session[_f]
                                if _ip_val != _pre_val:
                                    updated_session[_f] = _ip_val  # type: ignore[literal-required]
                    # Capture new sessions added by check_board so they survive
                    # the fanout merge.  Only keys not present before dispatch
                    # are considered new to avoid overwriting concurrent updates.
                    updated_sessions = updated.get("active_sessions") or {}
                    new_sessions = {k: v for k, v in updated_sessions.items() if k not in active_sessions}
                    # Capture mutations to other existing sessions (e.g. prepare_conflict_resolution
                    # routing a BLOCKED session back to dispatching).  Only include sessions
                    # that actually changed vs the pre-fanout snapshot so that an unmodified
                    # deep-copy of a sibling can't clobber a real mutation applied by another
                    # concurrent task via last-writer-wins in cross_mutations.update(cm).
                    cross_session = {
                        k: v for k, v in updated_sessions.items()
                        if k != card_id and k in active_sessions
                        and v != pre_fanout_siblings.get(k)
                    }
                    g_updates: dict[str, Any] = {k: updated[k] for k in _GLOBAL_STATE_KEYS if k in updated}
                    if new_sessions:
                        g_updates["_new_sessions"] = new_sessions
                    if cross_session:
                        g_updates["_cross_session_mutations"] = cross_session
                    return AsyncSessionTickResult(
                        card_id=card_id,
                        ok=True,
                        session_state=updated_session,
                        duration_ms=int((perf_counter() - t0) * 1000),
                        global_updates=g_updates,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("session_graph_error", card_id=card_id, exc_info=True)
                    return AsyncSessionTickResult(
                        card_id=card_id,
                        ok=False,
                        error=str(exc),
                        session_state=pre_session,
                        duration_ms=int((perf_counter() - t0) * 1000),
                    )

        results: list[AsyncSessionTickResult] = list(
            await asyncio.gather(
                *[_invoke_one(cid, sess) for cid, sess in list(active_sessions.items())]
            )
        )

        # Merge global state updates from results.  Non-session, non-queue keys
        # come from the first successful result.  github_retry_queue is merged
        # across ALL results (dedupe by operation, keep highest attempt) so that
        # deferred entries from any session are not silently dropped.  New sessions
        # added by check_board are also merged from ALL results so no slot is lost.
        # Ops that were present at fanout start but are absent in any successful
        # result are treated as cleared and removed from the merged queue.
        pre_fanout_ops: set[str] = {
            e["operation"]
            for e in (self._state.get("github_retry_queue") or [])  # type: ignore[misc]
            if isinstance(e, dict) and e.get("operation")
        }
        cleared_ops: set[str] = set()
        first_global_merged = False
        merged_retry_queue: list[dict] | None = None
        merged_advocate_history: set[str] | None = None
        cross_mutations: dict[str, dict] = {}
        for result in results:
            if not result.ok or result.skipped or not result.global_updates:
                continue
            if not first_global_merged:
                for k, v in result.global_updates.items():
                    if k not in ("_new_sessions", "_cross_session_mutations", "github_retry_queue", "github_retry_after", "advocate_history"):
                        self._state[k] = v  # type: ignore[literal-required]
                first_global_merged = True
            ah = result.global_updates.get("advocate_history")
            if isinstance(ah, set):
                if merged_advocate_history is None:
                    merged_advocate_history = set(ah)
                else:
                    merged_advocate_history |= ah
            rq = result.global_updates.get("github_retry_queue")
            if isinstance(rq, list):
                session_ops = {e["operation"] for e in rq if isinstance(e, dict) and e.get("operation")}
                cleared_ops.update(pre_fanout_ops - session_ops)
                if merged_retry_queue is None:
                    merged_retry_queue = list(rq)
                else:
                    for entry in rq:
                        if not isinstance(entry, dict):
                            continue
                        op = entry.get("operation")
                        existing = next(
                            (e for e in merged_retry_queue if isinstance(e, dict) and e.get("operation") == op),
                            None,
                        )
                        if existing is None:
                            merged_retry_queue.append(entry)
                        else:
                            entry_attempt = int(entry.get("attempt", 0))
                            existing_attempt = int(existing.get("attempt", 0))
                            # Keep the most conservative entry: higher attempt wins;
                            # on a tie, keep the later retry_at so two sessions that
                            # deferred the same op at the same attempt (but slightly
                            # different wall-clock times) don't shorten the backoff.
                            # Compare as datetime objects — retry_at is always a
                            # datetime in-memory; str() comparison is fragile across
                            # tz representations.
                            entry_ra = entry.get("retry_at")
                            existing_ra = existing.get("retry_at")
                            later_retry_at = (
                                isinstance(entry_ra, datetime)
                                and isinstance(existing_ra, datetime)
                                and entry_ra > existing_ra
                            )
                            if entry_attempt > existing_attempt or (
                                entry_attempt == existing_attempt and later_retry_at
                            ):
                                merged_retry_queue[merged_retry_queue.index(existing)] = entry
            cm = result.global_updates.get("_cross_session_mutations") or {}
            cross_mutations.update(cm)  # last-writer-wins across concurrent results
            new_sessions = result.global_updates.get("_new_sessions") or {}
            for cid, sess in new_sessions.items():
                if cid not in active_sessions:
                    active_sessions[cid] = sess
                    logger.info("new_session_registered", card_id=cid)
        if merged_retry_queue is not None:
            if cleared_ops:
                merged_retry_queue = [
                    e for e in merged_retry_queue
                    if not (isinstance(e, dict) and e.get("operation") in cleared_ops)
                ]
            self._state["github_retry_queue"] = merged_retry_queue  # type: ignore[literal-required]
            retry_ats = [e["retry_at"] for e in merged_retry_queue if isinstance(e, dict) and isinstance(e.get("retry_at"), datetime)]
            self._state["github_retry_after"] = min(retry_ats) if retry_ats else None  # type: ignore[literal-required]
        if merged_advocate_history is not None:
            self._state["advocate_history"] = merged_advocate_history  # type: ignore[literal-required]

        # Apply cross-session mutations as a baseline before direct results so
        # each session's own tick result takes precedence over mutations from a
        # sibling session's graph run (e.g. prepare_conflict_resolution targeting
        # a BLOCKED session that was skipped this cycle).
        for cid, mutated_sess in cross_mutations.items():
            if cid in active_sessions:
                active_sessions[cid] = mutated_sess

        # Merge session results back into active_sessions; collect completions.
        completed_ids: list[str] = []
        for result in results:
            if result.skipped:
                # missing_card sessions have no card to resume — remove them so
                # the slot doesn't linger forever.
                if eligibilities[result.card_id].reason == "missing_card":
                    logger.warning("session_missing_card_evicted", card_id=result.card_id)
                    completed_ids.append(result.card_id)
                continue
            if result.ok:
                # Only overwrite on success; preserves any cross-session
                # mutations applied above for sessions whose own tick failed.
                active_sessions[result.card_id] = result.session_state
                sess = result.session_state
                if sess.get("phase", "idle") == "idle" and sess.get("current_card") is None:
                    completed_ids.append(result.card_id)

        for card_id in completed_ids:
            del active_sessions[card_id]
            logger.info("session_completed", card_id=card_id)

        # Clear the per-cycle advocate sentinel so the next cycle runs a fresh scan.
        self._state.pop("_advocate_scan_done", None)  # type: ignore[misc]

        self._state["active_sessions"] = active_sessions
        # Derive global phase from the highest-priority session phase so the
        # dashboard never shows a stale or idle value while work is ongoing.
        self._state["phase"] = _derive_global_phase(active_sessions)  # type: ignore[literal-required]

    async def _conduct_single_symphony(
        self,
        symphony_name: str,
        symphony_config: Any,  # SymphonyConfig
    ) -> None:
        """Run one orchestration cycle for a single symphony (spec 057)."""
        from coordinare.observability import bind_symphony, clear_symphony

        bind_symphony(symphony_name)
        self._state["current_symphony"] = symphony_name
        _propagating = False
        # Save symphony-scoped graph keys before entering the try so the outer
        # finally can always restore them (prevents cross-symphony contamination).
        _prev_current_card = self._state.get("current_card")
        _prev_board_snapshot = self._state.get("board_snapshot")
        _prev_session_skip_reasons = self._state.get("session_skip_reasons")
        _prev_phase = self._state.get("phase")
        try:
            # Check max_concurrent_cards limit per symphony
            symphony_states = self._state.get("symphony_states") or {}
            sym_state = symphony_states.get(symphony_name)
            _global_cfg = self._state.get("config")
            _effective_cfg = (
                symphony_config.effective_config(_global_cfg)
                if hasattr(symphony_config, "effective_config") and _global_cfg is not None
                else _global_cfg
            )
            _sym_sessions = (getattr(sym_state, "active_sessions", None) or {}) if sym_state is not None else {}
            if (
                sym_state is not None
                and _effective_cfg is not None
                and hasattr(_effective_cfg, "max_concurrent_cards")
                and len(_sym_sessions) >= _effective_cfg.max_concurrent_cards
            ):
                logger.debug(
                    "symphony.dispatch_skipped.at_capacity",
                    symphony=symphony_name,
                    active=len(_sym_sessions),
                    limit=_effective_cfg.max_concurrent_cards,
                )
                # Do NOT return here — existing sessions still need to be ticked by
                # the graph. The graph respects active_sessions count and will skip
                # new dispatch naturally while still monitoring in-flight work.

            _eff_max = (
                int(_effective_cfg.max_concurrent_cards)
                if _effective_cfg is not None and hasattr(_effective_cfg, "max_concurrent_cards")
                else self._max_concurrent_cards()
            )
            # Swap state["config"] to the per-symphony effective config so that
            # graph nodes and _invoke_multi_session() see the symphony's limits.
            _prev_config = self._state.get("config")
            # Swap state["github_service"] to the per-symphony service so that
            # graph nodes query the correct project board for this symphony.
            _sym_github_services = self._state.get("symphony_github_services") or {}
            _prev_github = self._state.get("github_service")
            _sym_github = _sym_github_services.get(symphony_name)
            _sym_workspace_managers = self._state.get("symphony_workspace_managers") or {}
            _prev_workspace_manager = self._state.get("workspace_manager")
            _sym_workspace_manager = _sym_workspace_managers.get(symphony_name)
            # Restore per-symphony active_sessions and other graph-scoped keys so
            # the graph sees this symphony's state, not the previous symphony's.
            self._state["active_sessions"] = dict(_sym_sessions)
            if sym_state is not None:
                self._state["current_card"] = sym_state.active_card
                if sym_state.board_snapshot is not None:
                    self._state["board_snapshot"] = sym_state.board_snapshot
                if sym_state.session_skip_reasons is not None:
                    self._state["session_skip_reasons"] = sym_state.session_skip_reasons
                if sym_state.previous_phase is not None:
                    self._state["phase"] = sym_state.previous_phase
            if _effective_cfg is not None and _effective_cfg is not _global_cfg:
                self._state["config"] = _effective_cfg
            if _sym_github is not None:
                self._state["github_service"] = _sym_github
            if _sym_workspace_manager is not None:
                self._state["workspace_manager"] = _sym_workspace_manager
            try:
                if _eff_max > 1:
                    await self._invoke_multi_session()
                else:
                    self._state = await self._graph.ainvoke(self._state)
            finally:
                self._state["config"] = _prev_config
                if _sym_github is not None:
                    self._state["github_service"] = _prev_github
                if _sym_workspace_manager is not None:
                    self._state["workspace_manager"] = _prev_workspace_manager

            # Update symphony state on success
            if sym_state is not None:
                sym_state.cycle_count = getattr(sym_state, "cycle_count", 0) + 1
                sym_state.last_poll_at = datetime.now(UTC)
                board_snap = self._state.get("board_snapshot")
                if board_snap is not None:
                    sym_state.board_snapshot = board_snap
                sym_state.active_sessions = dict(self._state.get("active_sessions") or {})
                sym_state.active_card = self._state.get("current_card")
                skip_reasons = self._state.get("session_skip_reasons")
                sym_state.session_skip_reasons = dict(skip_reasons) if skip_reasons else None
                # Per-symphony phase transition metric (labels each transition with the actual symphony)
                _prev_sym_phase = sym_state.previous_phase
                _cur_sym_phase = self._state.get("phase")
                if _cur_sym_phase != _prev_sym_phase:
                    _sym_transition = _PHASE_TRANSITION_METRIC.get(
                        (str(_prev_sym_phase), str(_cur_sym_phase))
                    )
                    if _sym_transition is not None:
                        METRICS.card_state_transitions_total.labels(
                            symphony=symphony_name,
                            transition_type=_sym_transition,
                        ).inc()
                    sym_state.previous_phase = _cur_sym_phase  # type: ignore[assignment]
        except (asyncio.CancelledError, CircuitOpenError):
            _propagating = True
            # Rebuild aggregate active_sessions from last-known-good symphony states
            # so state is not left in a per-symphony scoped view on abnormal exit.
            _agg: dict = {}
            for _ss in (self._state.get("symphony_states") or {}).values():
                _agg.update(getattr(_ss, "active_sessions", None) or {})
            self._state["active_sessions"] = _agg
            raise
        except Exception as exc:
            logger.error(
                "symphony.cycle_error",
                symphony=symphony_name,
                error=str(exc),
                exc_info=True,
            )
            sym_states = self._state.get("symphony_states") or {}
            s = sym_states.get(symphony_name)
            if s is not None:
                s.error_count = getattr(s, "error_count", 0) + 1
                s.last_error = str(exc)
            # Do NOT re-raise — other symphonies continue
        finally:
            clear_symphony()
            if not _propagating:
                self._state["current_symphony"] = None
            # Restore symphony-scoped graph keys so the next symphony starts clean.
            self._state["current_card"] = _prev_current_card
            self._state["board_snapshot"] = _prev_board_snapshot
            self._state["session_skip_reasons"] = _prev_session_skip_reasons
            self._state["phase"] = _prev_phase

    async def _handle_config_reload(self) -> None:
        """Reload configuration from disk and update symphony state (spec 057)."""
        config_path = self._state.get("config_path")
        if config_path is None:
            logger.warning("config_reload.no_path")
            return
        try:
            from coordinare.config import CoordinareConfiguration
            from coordinare.config_validation import (
                _load_raw_yaml,
                coerce_multi_symphony_raw,
                is_multi_symphony_config,
                validate_config,
                wrap_legacy_config,
            )
            from coordinare.graph.state import SymphonyRuntimeState

            validation = validate_config(config_path)
            if not validation.passed:
                errs = "; ".join(e.fix_hint for e in validation.errors)
                logger.error("config_reload.validation_failed", errors=errs)
                return

            raw = _load_raw_yaml(config_path)

            if is_multi_symphony_config(raw):
                coordinare_cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
            else:
                wrapped = wrap_legacy_config(raw)
                coordinare_cfg = CoordinareConfiguration(**wrapped)

            old_names = set(self._state.get("symphony_configs") or {})
            new_configs = {s.name: s for s in coordinare_cfg.symphonies}
            new_names = set(new_configs)

            added = new_names - old_names
            removed = old_names - new_names

            # Preflight: check active sessions BEFORE mutating any daemon state so
            # an aborted reload cannot leave symphony_configs/config/config_version
            # out of sync with the still-running symphony_states.
            sym_states = dict(self._state.get("symphony_states") or {})
            for name in added:
                sym_states[name] = SymphonyRuntimeState(name=name)
            for name in removed:
                sym_state = sym_states.get(name)
                if sym_state is not None and getattr(sym_state, "active_sessions", None):
                    logger.error(
                        "config_reload.aborted_active_sessions",
                        symphony=name,
                        active_sessions=list(sym_state.active_sessions.keys()),
                        reason="Reload would orphan in-flight sessions; retry once sessions complete",
                    )
                    return
                sym_states.pop(name, None)

            # Rebuild the full per-symphony GitHubService map on every successful
            # reload so that changed project numbers or GitHub settings in existing
            # symphonies are reflected, not just added/removed symphonies.
            # Build new services first; only swap (and close old) once all are ready
            # so a failed initialize() leaves the daemon in a consistent state.
            _global_gh = self._state.get("github_service")
            if _global_gh is not None:
                import contextlib

                from coordinare.auth import build_auth as _build_auth
                from coordinare.observability import bind_symphony, clear_symphony
                from coordinare.services.github import GitHubService as _GHSvc

                _sym_svcs: dict[str, Any] = {}
                try:
                    for _sym_name, _sym_cfg in new_configs.items():
                        _new_eff = _sym_cfg.effective_config(coordinare_cfg.global_config)
                        _r = _new_eff.resilience.github_retry
                        _new_svc = _GHSvc(
                            auth=_build_auth(_new_eff),
                            org=_new_eff.github_org,
                            project_number=_new_eff.github_project_number,
                            endpoint=_new_eff.github_graphql_url,
                            circuit_breaker=_global_gh._circuit_breaker,
                            retry_kwargs={
                                "attempts": _r.attempts,
                                "wait_initial": _r.wait_initial_seconds,
                                "wait_max": _r.wait_max_seconds,
                                "wait_jitter": _r.wait_jitter_seconds,
                                "wait_exp_base": _r.wait_exp_base,
                            },
                        )
                        _new_svc._project_name = _new_eff.project_name
                        bind_symphony(_sym_name)
                        try:
                            await _new_svc.initialize()
                        finally:
                            clear_symphony()
                        _sym_svcs[_sym_name] = _new_svc
                except Exception:
                    # Initialization failed — close any partially-built services
                    # and re-raise so the outer handler logs and keeps old state.
                    for _partial in _sym_svcs.values():
                        if hasattr(_partial, "aclose"):
                            with contextlib.suppress(Exception):
                                await _partial.aclose()
                    raise

                # All new services ready — close old ones then swap atomically.
                _old_svcs: dict[str, Any] = self._state.get("symphony_github_services") or {}
                for _old_svc in _old_svcs.values():
                    if hasattr(_old_svc, "aclose"):
                        with contextlib.suppress(Exception):
                            await _old_svc.aclose()
                self._state["symphony_github_services"] = _sym_svcs

                # Rebuild per-symphony WorkspaceManager instances for the new config.
                from coordinare.workspace import WorkspaceManager as _WorkspaceManager
                _sym_wms: dict[str, Any] = {}
                for _sym_name2, _sym_cfg2 in new_configs.items():
                    _wm_eff = _sym_cfg2.effective_config(coordinare_cfg.global_config)
                    _sym_wms[_sym_name2] = _WorkspaceManager(
                        _wm_eff,
                        auth=_build_auth(_wm_eff),
                        github_service=_sym_svcs.get(_sym_name2),
                    )
                self._state["symphony_workspace_managers"] = _sym_wms

            # Atomic state swap: all config fields updated only after all
            # preflights and service builds have succeeded without raising.
            self._state["symphony_configs"] = new_configs  # type: ignore
            self._state["coordinare_config"] = coordinare_cfg  # type: ignore
            self._state["config"] = coordinare_cfg.global_config  # type: ignore
            self._state["orchestra_config"] = coordinare_cfg.orchestra  # type: ignore
            self._state["symphony_states"] = sym_states  # type: ignore
            self._state["config_mode"] = "multi_symphony" if is_multi_symphony_config(raw) else "legacy"
            self._state["config_version"] = (self._state.get("config_version") or 0) + 1  # type: ignore

            logger.info("config_reloaded", added=list(added), removed=list(removed))
        except Exception as exc:
            logger.error("config_reload.failed", error=str(exc), exc_info=True)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:
                return

    def _emit(self, **event: Any) -> None:
        category = event.get("category", "activity")
        if category == "failure":
            logger.error("runtime_event", **event)
        elif category in {"heartbeat", "activity"}:
            logger.debug("runtime_event", **event)
        else:
            logger.info("runtime_event", **event)

    async def start(self) -> None:
        self._main_task = asyncio.current_task()
        self._running = True
        self._install_signal_handlers()
        last_heartbeat = monotonic()
        cycle_count = 0

        # T018: Startup recovery — load persisted state before poll loop
        if self._state_store is not None:
            try:
                snapshot = await self._state_store.load()
                if snapshot is not None:
                    self._restore_from_snapshot(snapshot)
                    self._emit(
                        **build_runtime_event(
                            category="startup",
                            message="prior state loaded",
                            phase=snapshot.phase,
                            active_card_id=snapshot.active_card_id,
                        )
                    )
                    # T020: Board reconciliation after restore
                    if snapshot.active_card_id:
                        await self._reconcile_with_board(snapshot)
                else:
                    self._emit(
                        **build_runtime_event(
                            category="startup",
                            message="no prior state found",
                        )
                    )
            except StateLoadError as exc:
                self._emit(
                    **build_runtime_event(
                        category="warning",
                        message="state load failed",
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                )
                # Fresh start — self._state already initialised by initial_state()

        previous_phase = self._state.get("phase")
        self._emit(
            **build_runtime_event(
                category="startup",
                message="daemon startup complete",
                run_mode=self._run_mode,
                poll_interval_seconds=self._poll_interval_seconds,
            )
        )

        # T018: Dispatch daemon_restart notification
        notification_service = self._state.get("notification_service")
        if notification_service is not None:
            from coordinare.models.notification import (
                EventType,
                NotificationEvent,
                NotificationSeverity,
            )

            try:
                await notification_service.dispatch(
                    NotificationEvent(
                        event_type=EventType.daemon_restart,
                        severity=NotificationSeverity.info,
                        source="daemon",
                        payload={
                            "event_type": "daemon_restart",
                            "severity": "info",
                            "source": "daemon",
                            "run_mode": self._run_mode,
                            "summary": f"🔄 Coordinare restarted (mode: {self._run_mode})",
                        },
                    )
                )
            except Exception as exc:
                logger.warning("daemon_restart_notification_failed", error=str(exc))

        if self._poll_interval_seconds == 0:
            logger.info("polling_disabled")

        # T019: Track prolonged idle
        last_activity_at = monotonic()

        failure: RuntimeExecutionError | None = None
        while self._running and not self._stop_event.is_set():
            try:
                self._cycle_active = True
                # 057: Check for config reload at cycle start
                if self._config_reload_trigger.is_set():
                    self._config_reload_trigger.clear()
                    await self._handle_config_reload()

                # US2: bind a unique cycle_id for log correlation
                cycle_id = str(uuid4())
                bind_cycle_id(cycle_id)
                _cycle_t0 = perf_counter()

                # 035: Multi-card parallelism — when concurrency > 1,
                # iterate over active sessions independently.
                self._state["session_skip_reasons"] = {}
                # Free stale slots on every cycle (not just in multi-card mode).
                _slot_mgr = self._state.get("slot_manager")
                if _slot_mgr is not None and hasattr(_slot_mgr, "sync_from_sessions"):
                    _active_sessions = self._state.get("active_sessions") or {}
                    _slot_mgr.sync_from_sessions(_active_sessions)

                # 057: Multi-symphony orchestration
                symphony_configs = self._state.get("symphony_configs") or {}
                _multi_symphony = bool(symphony_configs)
                if symphony_configs:
                    for sym_name, sym_cfg in symphony_configs.items():
                        if not self._running or self._stop_event.is_set():
                            break
                        if not getattr(sym_cfg, "enabled", True):
                            continue
                        await self._conduct_single_symphony(sym_name, sym_cfg)
                    # Rebuild aggregate active_sessions from all symphony states so
                    # downstream metrics, slot sync, and dashboard see the full picture.
                    _agg_sessions: dict = {}
                    for _ss in (self._state.get("symphony_states") or {}).values():
                        _agg_sessions.update(getattr(_ss, "active_sessions", None) or {})
                    self._state["active_sessions"] = _agg_sessions
                    self._state["phase"] = _derive_global_phase(_agg_sessions)  # type: ignore[literal-required]
                else:
                    # Legacy single-symphony mode (backward compat)
                    if self._max_concurrent_cards() > 1:
                        await self._invoke_multi_session()
                    else:
                        self._state = await self._graph.ainvoke(self._state)

                # US1: record cycle metrics
                _cycle_elapsed = perf_counter() - _cycle_t0
                METRICS.cycles_completed_total.inc()
                METRICS.cycle_duration_seconds.observe(_cycle_elapsed)
                # 035: Update active session gauge
                _active = self._state.get("active_sessions") or {}
                METRICS.active_sessions.set(len(_active))
                # US3: mark external service subsystems healthy after a successful poll cycle
                HEALTH.update("github", HealthStatus.healthy)
                HEALTH.update("agent", HealthStatus.healthy)
                # config and notifications don't change mid-run; refresh timestamps
                # so the stale-detection window doesn't expire between cycles.
                HEALTH.update("config", HealthStatus.healthy)
                if self._state.get("notification_service") is not None:
                    HEALTH.update("notifications", HealthStatus.healthy)

                self._cycle_active = False
                cycle_count += 1
                self._state["error_count"] = 0

                # Dashboard: record cycle and broadcast updated snapshot to all open tabs
                if self._dashboard_store is not None:
                    _current_phase = str(self._state.get("phase", "idle"))
                    self._dashboard_store.record_cycle(
                        duration_seconds=_cycle_elapsed,
                        phase=_current_phase,
                        outcome="success",
                    )
                    _snapshot = self._dashboard_store.build_snapshot(self, METRICS, HEALTH)
                    self._dashboard_store.broadcaster.broadcast(_snapshot)
                self._emit(
                    **build_runtime_event(
                        category="activity",
                        message="processing cycle completed",
                        cycle=cycle_count,
                        phase=self._state.get("phase", "unknown"),
                    )
                )
                current_phase = self._state.get("phase")
                if current_phase != previous_phase:
                    self._emit(
                        **build_runtime_event(
                            category="state_change",
                            message="state transition detected",
                            previous_phase=previous_phase,
                            current_phase=current_phase,
                        )
                    )
                    # In legacy mode, emit the phase-transition metric here.
                    # In multi-symphony mode it is emitted per-symphony inside
                    # _conduct_single_symphony() with the actual symphony label.
                    if not _multi_symphony:
                        _transition_label = _PHASE_TRANSITION_METRIC.get(
                            (str(previous_phase), str(current_phase))
                        )
                        if _transition_label is not None:
                            METRICS.card_state_transitions_total.labels(
                                symphony="__default__",
                                transition_type=_transition_label,
                            ).inc()
                    previous_phase = current_phase
                    # 028: Track when the phase was entered
                    self._state["phase_entered_at"] = datetime.now(UTC)
                    # T021: Persist snapshot on every phase transition
                    if self._state_store is not None:
                        await self._state_store.save(self._build_snapshot())

                # T019: Prolonged idle detection
                current_phase = self._state.get("phase")
                if current_phase != "idle":
                    last_activity_at = monotonic()
                elif notification_service is not None:
                    idle_seconds = monotonic() - last_activity_at
                    if idle_seconds >= self._idle_threshold_seconds:
                        from coordinare.models.notification import (
                            EventType,
                            NotificationEvent,
                            NotificationSeverity,
                        )

                        try:
                            await notification_service.dispatch(
                                NotificationEvent(
                                    event_type=EventType.prolonged_idle,
                                    severity=NotificationSeverity.warning,
                                    source="daemon",
                                    payload={
                                        "event_type": "prolonged_idle",
                                        "severity": "warning",
                                        "source": "daemon",
                                        "idle_seconds": str(int(idle_seconds)),
                                        "summary": f"💤 Coordinare has been idle for {int(idle_seconds // 60)} minutes — no cards to process",
                                    },
                                    dedup_key="prolonged_idle",
                                )
                            )
                        except Exception as exc:
                            logger.warning("prolonged_idle_notification_failed", error=str(exc))

                # 028: Stuck card detection (with cooldown to avoid alert spam)
                _stuck_phase = self._state.get("phase")
                _stuck_excluded = {"idle", "system_error"}
                if _stuck_phase and _stuck_phase not in _stuck_excluded and notification_service is not None:
                    _config = self._state.get("config")
                    _phase_entered = self._state.get("phase_entered_at")
                    if _config is not None and _phase_entered is not None and hasattr(_config, "stuck_alerts"):
                        _stuck_cfg = _config.stuck_alerts
                        _threshold = _stuck_cfg.per_phase_thresholds.get(_stuck_phase, _stuck_cfg.threshold_seconds)
                        _raw_cooldown = getattr(_stuck_cfg, "cooldown_seconds", None)
                        _cooldown = _raw_cooldown if _raw_cooldown is not None else _threshold
                        if _threshold > 0:
                            _elapsed = (datetime.now(UTC) - _phase_entered).total_seconds()
                            _last_stuck = getattr(self, "_last_stuck_alert_at", None)
                            _cooldown_ok = _last_stuck is None or (monotonic() - _last_stuck) >= _cooldown
                            if _elapsed > _threshold and _cooldown_ok:
                                from coordinare.models.notification import (
                                    EventType,
                                    NotificationEvent,
                                    NotificationSeverity,
                                )
                                _card = self._state.get("current_card") or {}
                                _card_title = str(_card.get("title", ""))[:50]
                                _card_num = _card.get("issue_number", "")
                                _card_ref = f"#{_card_num} " if _card_num else ""
                                _summary = f"⏰ {_card_ref}{_card_title} — stuck in {_stuck_phase} for {round(_elapsed // 60)} min"
                                try:
                                    await notification_service.dispatch(
                                        NotificationEvent(
                                            event_type=EventType.card_stuck,
                                            severity=NotificationSeverity.warning,
                                            payload={
                                                "phase": _stuck_phase,
                                                "elapsed_seconds": str(round(_elapsed)),
                                                "threshold_seconds": str(_threshold),
                                                "card_title": str(_card.get("title", "")),
                                                "card_id": str(_card.get("id", "")),
                                                "summary": _summary,
                                            },
                                            source="daemon",
                                            dedup_key=f"stuck:{_card.get('id', '')}:{_stuck_phase}",
                                        )
                                    )
                                    self._last_stuck_alert_at = monotonic()
                                except Exception as _exc:
                                    logger.warning("stuck_card_notification_failed", error=str(_exc))

                now = monotonic()
                if now - last_heartbeat >= self._heartbeat_interval_seconds:
                    self._emit(
                        **build_runtime_event(
                            category="heartbeat",
                            message="daemon heartbeat",
                            cycle=cycle_count,
                            phase=self._state.get("phase", "unknown"),
                        )
                    )
                    last_heartbeat = now

                if self._max_cycles is not None and cycle_count >= self._max_cycles:
                    self.stop()
                    clear_cycle_id()
                    break
                await self._wait_for_next_cycle()
            except asyncio.CancelledError:
                self._cycle_active = False
                clear_cycle_id()
                break  # exit loop cleanly so shutdown log can emit
            except CircuitOpenError as exc:
                self._cycle_active = False
                current_sym = self._state.get("current_symphony") or "__default__"
                self._state["current_symphony"] = None
                METRICS.service_calls_total.labels(
                    symphony=current_sym,
                    service=exc.service_name,
                    action="call_blocked",
                    outcome="circuit_open",
                ).inc()
                logger.warning(
                    "circuit_open.call_skipped",
                    service=exc.service_name,
                )
                # Mark the isolated service degraded so /ready reflects the circuit state.
                # Use the mapping to translate circuit service names to health subsystem names.
                _health_subsystem = _CIRCUIT_TO_HEALTH_SUBSYSTEM.get(exc.service_name)
                if _health_subsystem is not None:
                    HEALTH.update(
                        _health_subsystem,
                        HealthStatus.degraded,
                        details=f"circuit open: {exc.service_name}",
                    )
                # Do NOT set self._running = False — continue the poll loop
                clear_cycle_id()
                # Use a dedicated backoff rather than _wait_for_next_cycle():
                # in poll=0 (webhook-only) mode, _wait_for_next_cycle blocks
                # until a webhook fires — but if GitHub is down the circuit is
                # open AND no webhooks arrive, causing an indefinite hang.
                # This backoff always makes forward progress and respects stop().
                _backoff = (
                    self._poll_interval_seconds
                    if self._poll_interval_seconds > 0
                    else _CIRCUIT_OPEN_BACKOFF_SECONDS
                )
                _sleep_t = asyncio.ensure_future(self._sleep(_backoff))
                _stop_t = asyncio.ensure_future(self._stop_event.wait())
                try:
                    _cb_done, _cb_pending = await asyncio.wait(
                        {_sleep_t, _stop_t}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for _t in _cb_pending:
                        _t.cancel()
                except asyncio.CancelledError:
                    _sleep_t.cancel()
                    _stop_t.cancel()
                    break  # treat external cancellation as stop signal
            except Exception as exc:
                self._cycle_active = False
                self._state["error_count"] = self._state.get("error_count", 0) + 1
                self._emit(
                    **build_runtime_event(
                        category="failure",
                        message="runtime processing cycle failed",
                        error=str(exc),
                        failing_step="cycle_execution",
                        error_count=self._state["error_count"],
                    )
                )
                previous_phase = self._state.get("phase", "unknown")
                self._state["phase"] = "recovery"
                self._emit(
                    **build_runtime_event(
                        category="state_change",
                        message="state transition detected",
                        previous_phase=previous_phase,
                        current_phase="recovery",
                    )
                )
                failure = RuntimeExecutionError(phase="runtime", step="cycle_execution", cause=exc)
                # Dashboard: record error cycle and broadcast
                if self._dashboard_store is not None:
                    _err_phase = str(self._state.get("phase", "recovery"))
                    self._dashboard_store.record_cycle(
                        duration_seconds=perf_counter() - _cycle_t0,
                        phase=_err_phase,
                        outcome="error",
                    )
                    _err_snapshot = self._dashboard_store.build_snapshot(self, METRICS, HEALTH)
                    self._dashboard_store.broadcaster.broadcast(_err_snapshot)
                self._running = False
                clear_cycle_id()
            else:
                # Happy-path cycle end — clear cycle_id before inter-cycle sleep
                clear_cycle_id()

        self._emit(
            **build_runtime_event(
                category="shutdown",
                message="daemon stopped",
                graceful=failure is None,
                cycle_interrupted=self._stop_during_cycle,
                run_mode=self._run_mode,
            )
        )
        if failure is not None:
            raise failure
