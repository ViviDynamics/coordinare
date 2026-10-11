"""Phase helpers for the monitor_performer body (435 batch 3).

Split from ``monitor_performer._monitor_performer_body`` verbatim: each
``_phase_*`` helper receives ``(state, ctx)`` and returns a state to
short-circuit-return or ``None`` to fall through to the next phase.
"""


from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from coordinare.graph.nodes.monitor.activity import (
    _ACTIVITY_TYPE_BY_EVENT,  # noqa: F401 -- re-export
    _activity_attribution,  # noqa: F401 -- re-export
    _record_activity,
    _record_activity_batch,
)
from coordinare.graph.nodes.monitor.artefacts import (
    _lift_review_findings,
    _lift_security_findings,
    _parse_job_id_from_details_url,  # noqa: F401 -- re-export
    _pr_url_parts,
    _record_pr_artefacts,
)
from coordinare.graph.nodes.monitor.baseline import (
    _build_baseline_index,  # noqa: F401 -- re-export
    _classify_head_failures,  # noqa: F401 -- re-export
    _get_persona_check_map,  # noqa: F401 -- re-export
    _get_session_persona_scope,  # noqa: F401 -- re-export
    _plain,  # noqa: F401 -- re-export
)
from coordinare.graph.nodes.monitor.board import (
    _find_card_column,
    _reconcile_board_mismatch,
    _reset_token_counters,  # noqa: F401 -- re-export
    _teardown_workspace,
)
from coordinare.graph.nodes.monitor.constants import (
    _DEFAULT_RESUME_DIRECTIVE,
    _ROLE_RESUME_DIRECTIVES,
    _TERMINAL_MARKERS_FOR_HEAD,
    ABSOLUTE_CEILING_MULTIPLIER,
    EXPECTED_STAGE_MARKER,
    MAX_PERFORMER_EVENTS,  # noqa: F401 -- re-export
    PHASE_TO_EXPECTED_COLUMN,
    SENTINEL_STAGES,
    TERMINAL_SUCCESS_STATES,
    VERDICT_STAGES,
    ZERO_PROGRESS_REVIEW_STAGES,
)
from coordinare.graph.nodes.monitor.errors import (
    _FORMAT_CONTRACT_ERROR_MARKERS,  # noqa: F401 -- re-export
    _FORMAT_ERROR_PREFIX,
    _TRANSIENT_BACKEND_ERROR_MARKERS,  # noqa: F401 -- re-export
    _WORKFLOW_PUSH_REJECTION_MARKERS,  # noqa: F401 -- re-export
    _is_format_contract_error,
    _is_transient_backend_error,
    _is_workflow_push_permission_error,
)
from coordinare.graph.nodes.monitor.events import (
    merge_performer_events,
)
from coordinare.graph.nodes.monitor.gate_config import (
    _get_baseline_classification_gate_config,  # noqa: F401 -- re-export
    _get_baseline_prevention_gate_config,  # noqa: F401 -- re-export
    _get_ci_gate_config,
    _get_closer_pr_checks_config,  # noqa: F401 -- re-export
    _get_env_blocked_gate_config,
    _get_inherited_repair_gate_config,  # noqa: F401 -- re-export
    _get_local_test_gate_config,
)
from coordinare.graph.nodes.monitor.gates import (
    _CI_GATE_API_ERROR_COOLDOWN_SECONDS,  # noqa: F401 -- re-export
    _CI_GATE_API_ERROR_LAST_WARN_AT,  # noqa: F401 -- re-export
    _evaluate_baseline_prevention_gate,  # noqa: F401 -- re-export
    _evaluate_ci_gate,
    _evaluate_pr_checks_gate,
    _implementer_session_gone,
    _maybe_env_blocked_hold,  # noqa: F401 -- re-export
    _reset_ci_gate_api_error_cooldown,  # noqa: F401 -- re-export
    _retain_infrastructure_hold,
    _warn_ci_gate_api_error,  # noqa: F401 -- re-export
)
from coordinare.graph.nodes.monitor.repair import (
    _REPAIR_CANDIDATE_COMMENT,  # noqa: F401 -- re-export
    _REPAIR_INSTRUCTION,  # noqa: F401 -- re-export
    _build_repair_mandate,  # noqa: F401 -- re-export
    _dispatch_repair_reviewer,  # noqa: F401 -- re-export
    _evaluate_repair_guard,
    _l3_budget_exhausted,  # noqa: F401 -- re-export
    _pending_repair_dispatch,
    _post_repair_comment,  # noqa: F401 -- re-export
    _repair_record,  # noqa: F401 -- re-export
)
from coordinare.graph.nodes.monitor.ui import (
    _STATS_POLL_INTERVAL_SECONDS,  # noqa: F401 -- re-export
    _apply_assessor_decline,
    _refresh_backend_ui,
)
from coordinare.graph.nodes.monitor.verdict import (
    _FEEDBACK_DIGEST_MAX,  # noqa: F401 -- re-export
    _advance_stage,
    _apply_feedback_dispositions,  # noqa: F401 -- re-export
    _apply_pending_override,
    _carry_dispatched_feedback,
    _evaluate_success_floor,
    _feedback_cycle_budget,  # noqa: F401 -- re-export
    _feedback_cycle_exhausted,
    _record_stage_verdict,
    _resolve_dispute_round,
    _settled_head,
    _stamp_feedback_bounce,
    _summarise_feedback_items,  # noqa: F401 -- re-export
)
from coordinare.graph.state import _set_current_card
from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
from coordinare.services.assessor_failure import classify_assessor_failure
from coordinare.services.board_provider import board_of, move_card_or_warn
from coordinare.services.conducting import build_model_endpoint_backend
from coordinare.services.github import PermanentGitHubError
from coordinare.services.progress_fingerprint import progress_fingerprint
from coordinare.services.workflow_step import (
    latch_declared_steps,
    latch_workflow_step,
)
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

from coordinare.services import dispatch_guard  # noqa: E402
from coordinare.services.attempt_telemetry import close_attempt  # noqa: E402
from coordinare.services.docker_executor import DockerExecutor  # noqa: E402
from coordinare.services.no_progress import (  # noqa: E402
    MAX_NO_PROGRESS_RELAYS,
    note_no_progress,
    note_progress,
    should_block,
)
from coordinare.services.observer import (  # noqa: E402
    MAX_REPRIEVES,
    OBSERVER_TRIGGERS,
    ObserverQuery,
    ObserverVerdict,
    TriggerSnapshot,
    build_prompt,
    correction_signature,
    evaluate_triggers,
    observe,
    record_correction,
    record_recent_verdict,
)
from coordinare.services.progress_evidence import (  # noqa: E402
    evaluate_stall,
    production_advanced,
    production_cursor,
    production_fingerprint,
    read_evidence,
)
from coordinare.services.retry_counter import record_observer_kill  # noqa: E402
from coordinare.services.retune import apply_retune  # noqa: E402


@dataclass

class _BodyCtx:
    """Cross-phase locals threaded through the monitoring body."""

    bail: bool = False
    _budget: Any = None
    _budget_exceeded: Any = None
    _clar_at_dispatch: Any = None
    _clar_now: Any = None
    _dd_cfg: Any = None
    _drain_b: Any = None
    _env_cache_svc: Any = None
    _ev: Any = None
    _ha: Any = None
    _hb: Any = None
    _is_idle_timeout: Any = None
    _lp: Any = None
    _made_progress: Any = None
    _now_w: Any = None
    _reap_b: Any = None
    _reason: Any = None
    _sm: Any = None
    _stall: Any = None
    _stall_secs: Any = None
    _sym_name: Any = None
    _window_h: Any = None
    assessor_retry: Any = None
    board_provider: Any = None
    body: Any = None
    bot_comment_delta: Any = None
    card: Any = None
    card_id: Any = None
    comments: Any = None
    config: Any = None
    decision: Any = None
    exhausted: Any = None
    existing: Any = None
    f: Any = None
    failure_shape: Any = None
    findings: Any = None
    github: Any = None
    head_after: Any = None
    head_before: Any = None
    issue_id: Any = None
    key: Any = None
    lifecycle: Any = None
    marker: Any = None
    new_events: Any = None
    new_metrics: Any = None
    next_focus: Any = None
    notification_service: Any = None
    perf_svc: Any = None
    performer_services: Any = None
    policy: Any = None
    production_advanced: bool = False
    pr_url: Any = None
    prior_session_id: Any = None
    reason: Any = None
    relay_body: Any = None
    service: Any = None
    session_id: Any = None
    stage: Any = None
    status: Any = None
    status_payload: Any = None
    status_pre_polled: bool = False
    target_role: Any = None
    target_stage: Any = None
    teardown_on_exit: bool = True
    timeout_secs: Any = None
    tokens_delta: Any = None
    updates: Any = None
    value: Any = None



async def _phase_override(
    state: CoordinareState,
    card: Any,
    github: Any,
    board_provider: Any,
) -> CoordinareState | None:
    """031: human-override handling; returns state when handled, None to continue."""
    # 031: Check for a pending human override before polling status.
    # Done after card/github extraction so skip-final-stage can move the card.
    override_result = _apply_pending_override(state)
    if override_result is not None:
        # Teardown workspace on override paths to avoid resource leaks.
        await _teardown_workspace(override_result)

        # When skip advances to monitoring_pr (final stage), move card on board
        # and validate PR fields — same as the normal terminal-success path.
        if override_result.get("phase") == "monitoring_pr" and github is not None:
            effective_card = override_result.get("current_card", card)
            if not isinstance(effective_card, dict):
                return override_result
            card_id = str(effective_card.get("id", ""))
            pr_url = effective_card.get("pr_url")
            pr_node_id = effective_card.get("pr_node_id")
            if pr_url and pr_node_id:
                try:
                    await move_card_or_warn(board_provider, card_id, "IN_REVIEW")
                except Exception:
                    logger.warning("override.skip_move_card_failed", card_id=card_id)
            else:
                logger.error(
                    "override.skip_final_missing_pr_fields",
                    card_id=card_id,
                    pr_url_present=bool(pr_url),
                    pr_node_id_present=bool(pr_node_id),
                )
                # Mirror normal terminal-success behavior: missing PR fields
                # is a system error. Restore card status to pre-override value
                # since we never actually moved it on the board.
                override_result["phase"] = "system_error"
                override_result["system_error_count"] = state.get("system_error_count", 0) + 1
                override_result["system_error_reason"] = (
                    "Skip override reached final stage but pr_url or pr_node_id is missing"
                )
                override_result["system_error_last_at"] = datetime.now(UTC)
                override_result["system_error_notified"] = False
                updated_card = override_result.get("current_card")
                if isinstance(updated_card, dict):
                    previous_status = updated_card.get("previous_status")
                    if previous_status is not None:
                        updated_card["status"] = previous_status
                    override_result["current_card"] = updated_card
        return override_result
    return None

def _build_monitor_ctx(state: CoordinareState) -> _BodyCtx:
    """Bind the phase-threading locals into a _BodyCtx (verbatim from the body prologue)."""
    stage: str = state.get("performer_stage", "implementing")
    performer_services: dict[str, Any] = state.get("performer_services") or {}

    # card_id must be extracted before SlotManager lookup.
    # Also guards against missing/invalid current_card — if card is not a
    # dict we can't acquire a meaningful slot and should bail early.
    bail = not isinstance(state.get("current_card"), dict)
    if bail:
        state["phase"] = "idle"
    card = state.get("current_card")
    card_id = str(card.get("id", "")) if isinstance(card, dict) else ""
    return _BodyCtx(
        stage=stage,
        performer_services=performer_services,
        bail=bail,
        card=card,
        card_id=card_id,
        github=state.get("github_service"),
        board_provider=board_of(state),
        teardown_on_exit=True,
    )

async def _finish_ephemeral_advance(state: CoordinareState, ctx: _BodyCtx) -> CoordinareState:
    updates = _advance_stage(state, None)
    if updates.get("phase") == "monitoring_pr":
        ctx.updates = updates
        stopped = await _phase_terminal_success_s4_s1(state, ctx)
        if stopped is not None:
            return stopped
    elif updates.get("phase") == "dispatching" and updates.get("performer_stage") != ctx.stage:
        state["dispatched_feedback"] = {}
    cast("dict[str, Any]", state).update(updates)
    return state


async def _phase_ephemeral_gate(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """077: ephemeral-implementer CI-gate re-evaluation (no live session to poll)."""
    card = ctx.card
    card_id = ctx.card_id
    stage = ctx.stage
    # 077: ephemeral-implementer CI-gate re-evaluation (no live session to poll).
    # When the implementer ran on an ephemeral performer, succeeded, and the 075
    # ci_gate HOLD cleared agent_dispatch (the one-shot container is already torn
    # down), there is no live session to poll on the next cycle. Polling it would
    # lookup-miss and cascade into a false transport-error block. Re-evaluate the
    # gate directly against GitHub instead of polling a dead container, and
    # advance on PASS using the card's persisted PR identifiers.
    #
    # The discriminator must be robust: gate keyed on the *live* session state
    # (`_implementer_session_gone`) + an open PR + the gate being enabled — NOT
    # on `latest_ci_gate_decision.verdict`, which does not reliably survive the
    # multi-session state round-trip. Runs BEFORE slot acquisition — a gate-only
    # wait holds no performer slot. Persistent implementers keep a live session
    # (`_implementer_session_gone` is False), so this branch never fires for them.
    if (
        stage == "implementing"
        and state.get("phase") == "monitoring_performer"
        and _implementer_session_gone(state)
    ):
        _pr_url = card.get("pr_url") if isinstance(card, dict) else None
        _gate_cfg = _get_ci_gate_config(state)
        if _pr_url and _gate_cfg is not None and getattr(_gate_cfg, "enabled", False):
            # Held/gone ephemeral implementer WITH an open PR: re-evaluate the
            # gate directly against GitHub (no live session to poll) and advance
            # on PASS using the card's persisted PR identifiers.
            ci_updates, ci_stop = await _evaluate_ci_gate(state, card_id, _pr_url)
            for _k, _v in ci_updates.items():
                state[_k] = _v  # type: ignore[literal-required]
            # 095 (FR-008): any non-env-hold verdict means the infra block is not
            # active this cycle — clear the dedup state so a later recurrence of
            # the same (head, pattern) re-notifies (auto-resume / flapping).
            if "env_blocked" not in ci_updates and not _retain_infrastructure_hold(state, ci_updates):
                state["env_blocked"] = None
            if ci_stop:
                return state
            # The worker succeeded, but final PR checks still own acknowledgement.
            return await _finish_ephemeral_advance(state, ctx)
        # Gone ephemeral implementer with nothing to gate on (no PR yet, or the
        # gate is disabled): the one-shot container is already torn down, so
        # polling it would lookup-miss into a false transport-error block. Re-
        # dispatch cleanly instead — a fresh container redoes the turn — without
        # burning the system-error retry budget (mirrors check_board's clean
        # restart re-dispatch). Persistent implementers keep a live session, so
        # this never fires for them.
        logger.info(
            "monitor_performer.ephemeral_implementer_redispatch",
            card_id=card_id,
            performer_stage=stage,
            has_pr=bool(_pr_url),
        )
        state["phase"] = "dispatching"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    return None

async def _phase_slot_setup(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """048/027: slot acquisition, session id, role timeout, teardown latch."""
    card = ctx.card
    card_id = ctx.card_id
    performer_services = ctx.performer_services
    service = ctx.service
    session_id = ctx.session_id
    stage = ctx.stage
    timeout_secs = ctx.timeout_secs
    # 048: Resolve the card's specific service instance via SlotManager so
    # status polls go to the correct transport (not just the primary).
    slot_manager = state.get("slot_manager")
    dispatch = state.get("agent_dispatch") or {}
    # Restored turns keep their transport identity even if current role
    # aliases or slot defaults changed. Poll that existing instance before
    # allocating a slot or falling back to the current stage default.
    by_id = state.get("performer_services_by_id") or {}
    service = by_id.get(str(dispatch.get("performer_id") or "")) if dispatch.get("session_id") else None
    if service is None and slot_manager is not None and hasattr(slot_manager, "acquire") and card_id:
        # acquire() returns the already-allocated service for this card
        # (idempotent — doesn't consume a new slot).
        service = slot_manager.acquire(stage, card_id, config=state.get("config"))
    if service is None:
        service = performer_services.get(stage)

    # Fallback to legacy agent_service only when performer_services is empty
    # (backward compatibility with pre-019 configurations).
    if service is None and not performer_services:
        service = state.get("agent_service")

    if service is None:
        state["phase"] = "idle"
        return state

    session_id = state.get("agent_dispatch", {}).get("session_id", "")
    card_id = str(card.get("id", ""))  # re-extract with confirmed dict type

    # 027: Enforce per-role session timeout at coordinare level
    # Only enforced when explicitly configured (role_timeouts[stage] > 0).
    role_timeouts: dict[str, int] = state.get("role_timeouts") or {}
    timeout_secs = role_timeouts.get(stage, 0)

    # Assume terminal by default; cleared only when the performer is still working.
    # The finally block guarantees teardown even on unexpected exceptions.
    # teardown latch: ctx.teardown_on_exit defaults True (see _BodyCtx).
    ctx.card_id = card_id
    ctx.service = service
    ctx.session_id = session_id
    ctx.timeout_secs = timeout_secs
    return None

async def _phase_board_reconcile(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _stall = ctx._stall
    card = ctx.card
    card_id = ctx.card_id
    timeout_secs = ctx.timeout_secs
    # 032: Board reconciliation — check card column before polling status
    current_phase = state.get("phase", "")
    expected_column = PHASE_TO_EXPECTED_COLUMN.get(current_phase)
    if expected_column and isinstance(card, dict):
        board_snapshot = state.get("board_snapshot") or {}
        # Only reconcile when board_snapshot has at least one card in any column.
        # An empty snapshot (all columns []) means either check_board hasn't run
        # yet this cycle, or the board is genuinely empty. In the latter case,
        # the card would be detected as "disappeared" by check_board's 026 logic.
        has_data = any(isinstance(v, list) and len(v) > 0 for v in board_snapshot.values())
        if has_data:
            actual_column = _find_card_column(card_id, board_snapshot)
            if _reconcile_board_mismatch(state, card_id, expected_column, actual_column):
                return state
    # 027: Check session timeout before polling status.
    # 380: measured from the last thing the performer PRODUCED, not from
    # dispatch. A blind stopwatch fails in both directions -- it kills work
    # that is progressing slowly and grants a full budget to work that is
    # going nowhere. `last_production_at` is set below, after each poll's
    # events are merged, and stays None until the performer does something,
    # so a run that has produced nothing is measured from dispatch exactly
    # as it was before.
    dispatch_at = state.get("agent_dispatch_at")
    _stall = evaluate_stall(
        now=datetime.now(UTC),
        dispatch_at=dispatch_at,
        last_production_at=state.get("last_production_at"),
        timeout_secs=timeout_secs,
        absolute_timeout_secs=timeout_secs * ABSOLUTE_CEILING_MULTIPLIER,
    )
    ctx._stall = _stall
    return None

async def _phase_stall_expired(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _ev = ctx._ev
    _sm = ctx._sm
    _stall = ctx._stall
    card_id = ctx.card_id
    stage = ctx.stage
    timeout_secs = ctx.timeout_secs
    if _stall.expired:
        elapsed = _stall.elapsed_s
        _ev = read_evidence(state.get("performer_events"))
        # 430: the floor decided WHEN to look, and the observer is the judge
        # (the standalone #389 convergence ask is retired — one judge, not
        # two). The #389 "only ever grant time" policy survives for the
        # grant-time verdicts (continue, correction, retune); a kill verdict
        # does what #389 deliberately deferred, under the #427 guardrails;
        # escalate and a dead observer leave the floor in charge, exactly as
        # it decides when no judge is reachable.
        _reprieves = int(state.get("convergence_reprieves") or 0)
        observer_cfg = _enabled_observer_cfg(state)
        _fold: tuple[str, CoordinareState | None] = ("block", None)
        if observer_cfg is not None and _reprieves < MAX_REPRIEVES:
            try:
                _fold = await _observer_stall_judgement(
                    state, ctx, observer_cfg, _ev, elapsed, _reprieves,
                )
            except Exception as exc:
                logger.warning(
                    "monitor_performer.observer_stall_unavailable",
                    card_id=card_id, performer_stage=stage,
                    error=type(exc).__name__,
                )
                _fold = ("block", None)
            if _fold[0] != "block":
                # "short": the fold owns the outcome (reprieve or executed
                # kill). "continue": the cycle keeps running normally — a
                # deferred kill retries next cycle, a terminal turn routes
                # through the poll. Both reuse the fall-through tail.
                ctx._ev = _ev
                ctx._sm = _sm
                return _fold[1]
        logger.warning(
            "monitor_performer.session_timeout",
            performer_stage=stage,
            card_id=card_id,
            elapsed_seconds=round(elapsed),
            timeout_seconds=timeout_secs,
            anchor=_stall.anchor,
            tool_uses=_ev.tool_uses,
        )
        # 048: release slot on timeout
        _sm = state.get("slot_manager")
        if _sm is not None and hasattr(_sm, "release"):
            _sm.release(stage, card_id)
        state["phase"] = "blocked"
        state["open_questions"] = [
            # 380: keeps the "timed out" wording operators and tests grep
            # for, and adds what the clock alone could never say: whether
            # anything was actually happening. A run that emitted hundreds
            # of events while running no commands reads very differently
            # from one that was quietly working, and the old message could
            # not tell them apart.
            f"Performer ({stage}) timed out after {round(elapsed)}s with nothing produced "
            f"(limit: {timeout_secs}s, measured from {_stall.anchor}). "
            f"In that window it emitted {_ev.total_events} events, "
            f"ran {_ev.tool_uses} commands and completed {_ev.completions} steps."
            + (f" Judged not converging: {state['convergence_reason']}"
               if state.get("convergence_reason") else ""),
        ]
        return state
    ctx._ev = _ev
    ctx._sm = _sm
    return None

def _enabled_observer_cfg(state: CoordinareState) -> Any:
    """430: resolve the active symphony's observer config, or None if off.

    One resolver for both wake sites (the trigger-driven core and the stall
    fold) so a disabled observer is disabled everywhere — and enabling it
    arms both with the same switch.
    """
    sym_name = state.get("current_symphony")
    sym_cfg = (state.get("symphony_configs") or {}).get(sym_name) if sym_name else None
    observer_cfg = getattr(sym_cfg, "observer", None) if sym_cfg is not None else None
    if observer_cfg is None or not getattr(observer_cfg, "enabled", False):
        return None
    return observer_cfg

async def _observer_stall_judgement(
    state: CoordinareState,
    ctx: _BodyCtx,
    observer_cfg: Any,
    ev: Any,
    elapsed: float,
    reprieves: int,
) -> tuple[str, CoordinareState | None]:
    """430: judge a stalled turn with the observer.

    Returns ``("short", state)`` when the fold owns the outcome (a grant-time
    verdict, or an executed kill), ``("continue", None)`` when the cycle must
    keep running normally (a deferred or failed kill retries next cycle; a
    terminal turn's probed status is preserved for the status gate to route),
    and ``("block", None)`` when the floor must decide as it always has
    (observer unreachable, malformed, or unable to tell whether the turn is
    even live).
    """
    card_id = ctx.card_id
    stage = ctx.stage
    now = datetime.now(UTC)
    backend = state.get("observer_backend")
    if backend is None:
        global_cfg = getattr(state.get("coordinare_config"), "global_config", None)
        backend = build_model_endpoint_backend(global_cfg, observer_cfg.model_endpoint)
        if backend is None:
            return ("block", None)

    # Metadata only (425 review): the prompt never carries raw event text.
    evidence = {
        "stall_elapsed_s": round(elapsed),
        "tool_uses": ev.tool_uses,
        "completions": ev.completions,
        "total_events": ev.total_events,
    }
    recent_meta = [
        f"{e.get('type', 'event')}/{len(str(e.get('text', '')))}c"
        for e in (state.get("performer_events") or [])[-5:]
        if isinstance(e, dict)
    ]
    verdict = await observe(
        backend,
        ObserverQuery(
            card_id=card_id,
            stage=stage,
            triggers=["stalled_turn"],
            evidence=evidence,
            prompt=build_prompt(
                evidence,
                ["stalled_turn"],
                recent_meta,
                retune_bounds=getattr(observer_cfg, "retune_bounds", None),
            ),
        ),
    )
    if verdict is None:
        return ("block", None)
    state["observer_verdict"] = verdict.verdict
    _record_observer_verdict(
        state, card_id=card_id, stage=stage, verdict=verdict,
        triggers=["stalled_turn"], evidence=evidence, now=now,
    )
    if verdict.verdict == "kill":
        return await _observer_stall_kill(state, ctx, verdict, evidence)
    if verdict.verdict == "escalate":
        state["convergence_reason"] = verdict.reason
        return ("block", None)
    # continue / correction / retune all grant the turn more time; correction
    # and retune additionally fold their payloads in, and the next poll
    # dispatches with them (correction rides inject_observer_correction,
    # retune rides the override store).
    if verdict.verdict == "correction":
        pending = record_correction(state.get("observer_correction"), verdict.reason, evidence)
        if pending is not None:
            state["observer_correction"] = pending
            logger.info(
                "observer.correction_pending",
                card_id=card_id, signature=pending["signature"],
            )
        else:
            logger.info(
                "observer.correction_collapsed",
                card_id=card_id, signature=correction_signature(verdict.reason),
            )
    if verdict.retune:
        # 428's retune-on-continue: a structured retune request may ride ANY
        # non-kill verdict — the stall fold must behave like the trigger wake.
        _apply_observer_retune(state, ctx, verdict, observer_cfg)
    state["convergence_reprieves"] = reprieves + 1
    state["last_production_at"] = now
    # The turn keeps running, and the phase loop is about to short-circuit —
    # past _phase_in_progress, which is what normally latches the workspace
    # as still-active. Latch it here or the finally tears down a live
    # workspace (a latent #389 bug this fold inherits and fixes).
    ctx.teardown_on_exit = False
    logger.info(
        "monitor_performer.convergence_reprieve",
        card_id=card_id, performer_stage=stage,
        reprieve=reprieves + 1, of=MAX_REPRIEVES,
        verdict=verdict.verdict, elapsed_seconds=round(elapsed),
        reason=verdict.reason[:200],
    )
    return ("short", state)

async def _observer_stall_kill(
    state: CoordinareState,
    ctx: _BodyCtx,
    verdict: ObserverVerdict,
    evidence: dict[str, Any],
) -> tuple[str, CoordinareState | None]:
    """430: a stall-fold kill. 427 liveness first — this fold runs BEFORE the
    poll, so the turn's status is unknown and must be polled here. Terminal
    work advances through its normal routing, not a kill; a kill that cannot
    be executed keeps monitoring and retries next cycle.
    """
    card_id = ctx.card_id
    stage = ctx.stage
    session_id = ctx.session_id
    service = ctx.service
    if not isinstance(session_id, str) or not session_id or service is None:
        return ("block", None)
    try:
        status = await service.check_status(session_id, payload={})
    except Exception as exc:
        logger.warning(
            "monitor_performer.observer_stall_status_failed",
            card_id=card_id, performer_stage=stage,
            error=type(exc).__name__,
        )
        return ("block", None)
    if not isinstance(status, dict) or status.get("status") != "working":
        logger.info(
            "observer.kill_ignored_turn_not_live",
            card_id=card_id, performer_stage=stage,
            turn_status=str(status.get("status") if isinstance(status, dict) else status),
        )
        if isinstance(status, dict) and status.get("status") != "working":
            # For an ephemeral service this probe already consumed the job:
            # check_status cleans up the active job on a terminal result, so
            # poll_service must not re-poll the same session this cycle or
            # the terminal result would surface as a transport-error miss.
            ctx.status = status
            ctx.status_pre_polled = True
        return ("continue", None)
    ctx.status = status
    dd_cfg = getattr(state.get("coordinare_config"), "dispatcher_dedup", None)
    kill_result = await _phase_observer_kill(state, ctx, verdict, evidence, dd_cfg)
    if kill_result is None:
        # Deferred (mutex held) or failed teardown: keep monitoring and
        # retry next cycle — the 427 fail-safe. Never block a live turn.
        return ("continue", None)
    return ("short", kill_result)

async def phase_458(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    service = ctx.service
    session_id = ctx.session_id
    stage = ctx.stage
    # Include a fresh GitHub token in the status check so the performer
    # can refresh its credentials mid-session (App tokens expire after 1 hour).
    # Use the public ``get_fresh_github_token`` accessor rather than
    # reaching into the workspace manager's private ``_auth`` — keeps
    # the coupling narrow and testable.  Log failures at warning level
    # with exc_type so repeated silent refresh failures (which would
    # eventually produce 401 cascades from the performer) are visible.
    status_payload: dict[str, Any] = {}
    workspace_manager = state.get("workspace_manager")
    if workspace_manager is not None and hasattr(workspace_manager, "get_fresh_github_token"):
        try:
            fresh_token = await workspace_manager.get_fresh_github_token()
            if fresh_token:
                status_payload["github_token"] = fresh_token
        except Exception as exc:
            logger.warning(
                "monitor_performer.token_refresh_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
                card_id=card_id,
                performer_stage=stage,
            )
    # 069 diagnostic: confirm the polling caller resolved to the same
    # HttpPerformerService instance that dispatched the job (i.e. that
    # session_id is still tracked in _active_jobs for ephemeral performers).
    _has_live = None
    if hasattr(service, "has_live_session") and session_id:
        try:
            _has_live = service.has_live_session(str(session_id))
        except Exception:
            _has_live = "error"
    logger.info(
        "monitor_performer.pre_check_status",
        card_id=card_id,
        performer_stage=stage,
        session_id=str(session_id),
        service_class=type(service).__name__,
        service_instance_id=id(service),
        has_live_session=_has_live,
    )
    ctx.status_payload = status_payload
    return None

async def poll_service(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Poll the active performer service (verbatim except for handler extraction)."""
    if ctx.status_pre_polled:
        # 430: the stall-fold liveness probe already polled (and, for an
        # ephemeral service, cleaned up) this terminal turn; reuse its result.
        ctx.status_pre_polled = False
        return None
    status_payload = ctx.status_payload
    service = ctx.service
    session_id = ctx.session_id
    try:
        status = await service.check_status(str(session_id), payload=status_payload)
    except (TransportError, ConnectionError, TimeoutError) as exc:
        return await poll_transport_error(state, ctx, exc)
    except PermanentGitHubError as exc:
        return await poll_permanent_error(state, ctx, exc)
    ctx.status = status
    return None

async def poll_transport_error(
    state: CoordinareState,
    ctx: _BodyCtx,
    exc: Exception,
) -> CoordinareState:
    """Transport-error handler (verbatim except clause)."""
    _sm = ctx._sm
    board_provider = ctx.board_provider
    card_id = ctx.card_id
    github = ctx.github
    reason = ctx.reason
    service = ctx.service
    session_id = ctx.session_id
    stage = ctx.stage
    if isinstance(exc, PerformerAuthError):
        _refresh_failed_at = getattr(service, "secret_refresh_failed_at", None)
        failed_at = (
            _refresh_failed_at(str(session_id))
            if callable(_refresh_failed_at)
            else None
        )
        if failed_at is not None:
            reason = (
                f"Performer auth failure attributed to stale credentials "
                f"(refresh failed at {failed_at.isoformat()}): {exc}"
            )
            logger.error(
                "monitor_performer.stale_credentials_blocked",
                card_id=card_id,
                performer_stage=stage,
                secret_refresh_failed_at=failed_at.isoformat(),
                error=str(exc),
            )
            if github is not None:
                try:
                    await move_card_or_warn(board_provider, card_id, "BLOCKED")
                except Exception:
                    logger.warning(
                        "move_card_to_blocked_failed", card_id=card_id,
                    )
            state["phase"] = "blocked"
            state["open_questions"] = [reason]
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            _sm = state.get("slot_manager")
            if _sm is not None and hasattr(_sm, "release"):
                _sm.release(stage, card_id)
            return state
    # Network / transport failure — transient, route through retry logic.
    # ResilientAgentService re-raises TransportError after exhausting
    # retries; ConnectionError/TimeoutError cover bare transport errors.
    logger.warning(
        "monitor_performer.transport_error",
        card_id=card_id,
        performer_stage=stage,
        exc_type=type(exc).__name__,
        error=str(exc),
    )
    # If system_error_notified is True we're inheriting stale state from
    # a previous card's exhausted retry cycle (that card was BLOCKED and
    # can no longer appear in monitor_performer).  Reset so this card gets
    # its full retry budget and operator notification fires if needed.
    if state.get("system_error_notified"):
        state["system_error_count"] = 0
        state["system_error_notified"] = False
    state["system_error_count"] = state.get("system_error_count", 0) + 1
    state["system_error_last_at"] = datetime.now(UTC)
    state["system_error_reason"] = (
        f"Transport failure during status check: {type(exc).__name__}: {exc}"
    )
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    state["phase"] = "system_error"
    # 048: release slot on transport error
    _sm = state.get("slot_manager")
    if _sm is not None and hasattr(_sm, "release"):
        _sm.release(stage, card_id)
    # 065: belt-and-suspenders — if the service still tracks an ephemeral
    # container for this session, tear it down here in case check_status
    # raised before its own cleanup ran. Safe no-op if already cleaned.
    _cleanup = getattr(service, "_cleanup_ephemeral_job_by_id", None)
    if _cleanup is not None and session_id:
        try:
            await _cleanup(str(session_id))
        except Exception as cleanup_exc:
            logger.warning(
                "monitor_performer.transport_error.cleanup_failed",
                card_id=card_id,
                performer_stage=stage,
                session_id=str(session_id),
                exc_type=type(cleanup_exc).__name__,
                error=str(cleanup_exc),
            )
    return state

async def poll_permanent_error(
    state: CoordinareState,
    ctx: _BodyCtx,
    exc: Exception,
) -> CoordinareState:
    """Permanent-error handler (verbatim except clause)."""
    _sm = ctx._sm
    board_provider = ctx.board_provider
    card_id = ctx.card_id
    github = ctx.github
    stage = ctx.stage
    logger.error(
        "permanent_service_failure.card_blocked",
        card_id=card_id,
        performer_stage=stage,
        error=str(exc),
    )
    if github is not None:
        try:
            await move_card_or_warn(board_provider, card_id, "BLOCKED")
        except Exception:
            logger.warning("move_card_to_blocked_failed", card_id=card_id)
    state["phase"] = "blocked"
    state["open_questions"] = [f"Permanent service failure: {exc}"]
    # 048: release slot on permanent error
    _sm = state.get("slot_manager")
    if _sm is not None and hasattr(_sm, "release"):
        _sm.release(stage, card_id)
    return state

async def _phase_merge_events(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    existing = ctx.existing
    new_events = ctx.new_events
    new_metrics = ctx.new_metrics
    stage = ctx.stage
    status = ctx.status
    # Accumulate backend events (capped at 100 entries).
    new_events = status.get("events")
    if isinstance(new_events, list) and new_events:
        existing = list(state.get("performer_events") or [])
        # 358: merge, don't concatenate — the reported list is itself
        # cumulative, so appending it whole stored the same events again
        # on every poll until the buffer held nothing else.
        state["performer_events"] = merge_performer_events(existing, new_events)
        # 380: the clock resets only when something was PRODUCED. Talking
        # does not count: the turn this was filed for emitted 164 progress
        # deltas while changing zero files and running zero commands.
        _fingerprint = production_fingerprint(state["performer_events"])
        _cursor = production_cursor(state["performer_events"])
        _previous_cursor = state.get("last_production_cursor")
        _advanced = production_advanced(_fingerprint, state.get("last_production_fingerprint")) or (
            _cursor is not None and _previous_cursor is not None and _cursor != _previous_cursor
        )
        # Seed a restored count-only checkpoint without counting its replay as
        # fresh work. Future rolling-window production changes the cursor.
        state["last_production_cursor"] = _cursor or _previous_cursor
        if _advanced:
            state["last_production_fingerprint"] = _fingerprint
            state["last_production_at"] = datetime.now(UTC)
        ctx.production_advanced = _advanced
        # 138 T023: record in the same invocation that observed the batch —
        # the earliest anything can, and what makes SC-004 measurable. Hand
        # over the WHOLE reported list without pre-diffing: backends
        # re-report their entire accumulated events, and suppression by
        # content is the log's job (FR-022, FR-023). A positional cursor
        # would break anyway — the source list is a rolling [-100:].
        _record_activity_batch(state, new_events, card_id=card_id, stage=stage)
        # 343: latch the workflow step here, in the same invocation that
        # observed the batch. Latching at observation is what makes it
        # durable -- the activity feed evicts step markers during long
        # turns, so anything deriving the step at render time reads a lie.
        latch_workflow_step(state, new_events)
    # 343: the sequence the workflow says it will run, straight off the
    # wire. The trail above records what HAS happened; this is what is
    # coming, and together they are what lets the dashboard show "step 2 of
    # 6" with the rest greyed ahead rather than a bare current step.
    #
    # Adversarial review found this missing entirely: the performer sent
    # workflow_steps, coordinare declared the field in three places and
    # initialised it to [], and nothing ever read it. The field was inert,
    # so the self-description slice -- the whole remaining point of this
    # change -- did nothing.
    #
    # The rule itself lives beside latch_workflow_step, where it can be
    # tested without standing up this node.
    latch_declared_steps(state, status)
    # Store latest performer metrics for dashboard visibility.
    new_metrics = status.get("metrics")
    if isinstance(new_metrics, dict):
        state["performer_metrics"] = new_metrics
    ctx.existing = existing
    ctx.new_events = new_events
    ctx.new_metrics = new_metrics
    return None

async def phase_651(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _env_cache_svc = ctx._env_cache_svc
    _sym_name = ctx._sym_name
    card_id = ctx.card_id
    new_metrics = ctx.new_metrics
    stage = ctx.stage
    status = ctx.status
    tokens_delta = ctx.tokens_delta
    # Spec 063 Phase 4 (T024): performer signalled non-zero services-health.sh
    # → flag the symphony's env-cache for forced regeneration on the next
    # check_and_trigger cycle.
    if status.get("env_cache_health_failed"):
        _env_cache_svc = state.get("env_cache_service")
        _sym_name = state.get("current_symphony")
        if _env_cache_svc is not None and _sym_name:
            try:
                _env_cache_svc.mark_runtime_health_failed(_sym_name, state)
            except Exception as _exc:
                logger.warning(
                    "monitor_performer.mark_runtime_health_failed_error",
                    card_id=card_id,
                    symphony=_sym_name,
                    error=str(_exc),
                )
    # 034: Accumulate token usage and estimate cost.
    # Spec: only accept real integers; treat floats/bools/other types as invalid.
    raw_tokens = (new_metrics or {}).get("tokens_processed", 0) if isinstance(new_metrics, dict) else 0
    # 061: performers may emit tokens_processed=None before any LLM call has
    # been counted; treat that as "not reported" rather than invalid.
    if raw_tokens is None:
        logger.debug("monitor_performer.tokens_processed_unreported", card_id=card_id, performer_stage=stage)
        tokens_delta = 0
    elif isinstance(raw_tokens, bool):
        logger.warning("monitor_performer.invalid_tokens_processed", value=raw_tokens, card_id=card_id, performer_stage=stage)
        tokens_delta = 0
    elif isinstance(raw_tokens, float):
        logger.warning("monitor_performer.non_integer_tokens_processed", value=raw_tokens, card_id=card_id, performer_stage=stage)
        tokens_delta = 0
    elif not isinstance(raw_tokens, int):
        logger.warning("monitor_performer.invalid_tokens_processed", value=raw_tokens, card_id=card_id, performer_stage=stage)
        tokens_delta = 0
    elif raw_tokens < 0:
        logger.warning("monitor_performer.negative_tokens_processed", value=raw_tokens, card_id=card_id, performer_stage=stage)
        tokens_delta = 0
    else:
        tokens_delta = raw_tokens
    ctx._env_cache_svc = _env_cache_svc
    ctx._sym_name = _sym_name
    ctx.tokens_delta = tokens_delta
    return None

async def _phase_token_counters(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    config = ctx.config
    marker = ctx.marker
    notification_service = ctx.notification_service
    stage = ctx.stage
    status = ctx.status
    tokens_delta = ctx.tokens_delta
    if tokens_delta > 0:
        state["card_tokens_total"] = state.get("card_tokens_total", 0) + tokens_delta
        from coordinare.config import CostTrackingConfig
        config = state.get("config")
        cost_rate = CostTrackingConfig().cost_per_million_tokens
        if config is not None and hasattr(config, "cost_tracking"):
            cost_rate = config.cost_tracking.cost_per_million_tokens
        state["card_cost_estimate"] = state["card_tokens_total"] / 1_000_000 * cost_rate
        # Update Prometheus metrics
        from coordinare.metrics import METRICS
        METRICS.card_tokens_total.labels(role=stage).inc(tokens_delta)
        METRICS.card_cost_estimate_dollars.set(state["card_cost_estimate"])
        # 034: Budget alert — fire once per card
        budget = None
        if config is not None and hasattr(config, "cost_tracking"):
            budget = config.cost_tracking.cost_budget_per_card
        if budget is not None and state["card_cost_estimate"] > budget and not state.get("card_budget_alert_sent"):
            notification_service = state.get("notification_service")
            if notification_service is not None:
                try:
                    from coordinare.models.notification import (
                        EventType,
                        NotificationEvent,
                        NotificationSeverity,
                    )
                    await notification_service.dispatch(NotificationEvent(
                        event_type=EventType.card_budget_exceeded,
                        severity=NotificationSeverity.warning,
                        source="monitor_performer",
                        payload={
                            "event_type": EventType.card_budget_exceeded.value,
                            "severity": NotificationSeverity.warning.value,
                            "source": "monitor_performer",
                            "card_id": card_id,
                            "tokens": str(state["card_tokens_total"]),
                            "cost": f"${state['card_cost_estimate']:.2f}",
                            "budget": f"${budget:.2f}",
                            "summary": f"Card {card_id} exceeded cost budget (${state['card_cost_estimate']:.2f} > ${budget:.2f})",
                        },
                    ))
                    state["card_budget_alert_sent"] = True
                except Exception as exc:
                    logger.warning("monitor_performer.budget_alert_failed", card_id=card_id, error=str(exc), exc_info=True)
    marker = status.get("status", "working")
    ctx.config = config
    ctx.marker = marker
    ctx.notification_service = notification_service
    return None

async def _phase_security_scan_gate(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    existing = ctx.existing
    f = ctx.f
    key = ctx.key
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # --- 083 security-scan-gate: coordinare-authoritative floor ---
    # At the security stage the coordinare ran semgrep/bandit ONCE at
    # dispatch and stashed the findings in state["scanner_findings"]. Those
    # findings are authoritative: any critical/high finding forces
    # security_failed regardless of the model's self-reported verdict, and
    # a synthetic scanner_unavailable finding (routing=halt) blocks the card
    # fail-closed. Reuse the dispatch findings — never re-scan here. This
    # MUST run before the terminal-success handling below so an overridden
    # security_passed never advances the stage.
    # 170: when the report carries a "security" key (workflow report), skip
    # the 083 floor merge because the workflow already ran its floor inside
    # the performer.
    # 412 round 10: restored for NON-workflow security roles — the example
    # config promises "remove the workflow line to restore the prose path
    # exactly", so the prose path keeps its coordinare-side scan gate.
    if stage == "security" and marker != "working":
        _workflow_report = status.get("report") if isinstance(status.get("report"), dict) else {}
        if not isinstance(_workflow_report.get("security"), dict):
            raw_scanner = state.get("scanner_findings") or []
            scanner_findings = [f for f in raw_scanner if isinstance(f, dict)]
            gating = [
                f for f in scanner_findings
                if str(f.get("severity", "")).lower() in ("critical", "high")
            ]
            if gating:
                if marker != "security_failed":
                    logger.warning(
                        "monitor_performer.security_floor_override",
                        performer_stage=stage,
                        card_id=card_id,
                        model_marker=marker,
                        gating_count=len(gating),
                        severities=sorted(
                            {str(f.get("severity")) for f in gating},
                        ),
                    )
                    marker = "security_failed"
                # Merge scanner findings into the response findings, deduped by
                # (file, line, category). Copy status so we never mutate the
                # performer service's response object.
                existing_raw = status.get("findings", [])
                existing = list(existing_raw) if isinstance(existing_raw, list) else []
                seen = {
                    (f.get("file"), f.get("line"), f.get("category"))
                    for f in existing
                    if isinstance(f, dict)
                }
                merged = list(existing)
                for f in scanner_findings:
                    key = (f.get("file"), f.get("line"), f.get("category"))
                    if key not in seen:
                        merged.append(f)
                        seen.add(key)
                status = {**status, "findings": merged}
        else:
            logger.info(
                "monitor_performer.security_floor_skipped",
                performer_stage=stage,
                card_id=card_id,
            )
    ctx.existing = existing
    ctx.f = f
    ctx.key = key
    ctx.marker = marker
    ctx.status = status
    return None

async def phase_811(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # --- 120 (US1): QA evidence-integrity floor — coordinare-authoritative ---
    # A QA "qa_passed" that verified ZERO acceptance criteria (criteria were
    # checked but criteria_passed==0) or lacks the visual evidence a UI change
    # requires is UNSUBSTANTIATED and must not advance — regardless of the
    # performer's self-report (mirrors the 083 security floor: override the
    # marker BEFORE terminal handling). An accompanying environment signal
    # (report.environment_error / env_cache_health_failed) routes to the
    # qa_env_blocked HOLD path (repair the cache + re-run); otherwise it
    # bounces to the implementer as a synthetic failure. A substantiated pass
    # (>=1 criterion with evidence) and a genuine no-criteria scope
    # (criteria_checked==0) advance unchanged — no regression of real passes.
    # 165: lift the architect workflow's blueprint into the session. It is
    # the single source the implementer, documenter and QA briefs are
    # projected from at dispatch (dispatch_performer.inject_briefs). A new
    # blueprint replaces the old one and resets the documenter side run, so
    # the once-per-hash rule keys on the current plan. The prose architect
    # reports no blueprint and leaves state untouched (164 FR-005).
    if stage == "architecting" and marker == "plan_committed":
        _bp_report = status.get("report") if isinstance(status.get("report"), dict) else {}
        _bp = _bp_report.get("blueprint")
        if isinstance(_bp, dict) and _bp.get("milestones"):
            state["blueprint"] = dict(_bp)
            # 331: stamp the requirements this plan answers, so a later
            # dispatch can tell "still valid" from "needs replanning"
            # instead of unconditionally re-running the architect.
            from coordinare.graph.nodes.dispatch_performer import (
                stamp_blueprint_signature,
            )

            stamp_blueprint_signature(state)
            _doc_side = state.get("documenting_side")
            if not isinstance(_doc_side, dict) or (_doc_side.get("status") != "running" and not _doc_side.get("writer_active")):
                state["documenting_side"] = None
            logger.info(
                "blueprint.lifted",
                card_id=card_id,
                size=_bp.get("size"),
                milestones=len(_bp.get("milestones") or []),
                criteria=len(_bp.get("criteria") or []),
                docs=len(_bp.get("docs") or []),
                blueprint_hash=_bp.get("blueprint_hash"),
            )
    return None

async def _phase_assessment_complete(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # 166: lift the assessor workflow's assessment into the session. It is
    # the structured product reading of the card: goal, expected behaviour,
    # out-of-scope items, questions, assumptions, criteria with their source,
    # and carried clarifications. A new assessment replaces the old one.
    # The prose assessor reports no assessment and leaves state untouched.
    if stage == "assessing" and marker == "assessment_complete":
        _assess_report = status.get("report") if isinstance(status.get("report"), dict) else {}
        _assess = _assess_report.get("assessment")
        if isinstance(_assess, dict) and _assess.get("goal"):
            _assessment = dict(_assess)
            if "recorded_at" not in _assessment:
                _assessment["recorded_at"] = datetime.now(UTC).isoformat()
            state["assessment"] = _assessment
            logger.info(
                "assessment.lifted",
                card_id=card_id,
                ready=_assess.get("ready"),
                questions=len(_assess.get("questions") or []),
                criteria_source=_assess.get("criteria_source"),
                assessment_hash=_assess.get("assessment_hash"),
            )
        else:
            logger.info(
                "assessment.not_lifted",
                card_id=card_id,
                has_report=isinstance(_assess_report, dict),
                has_assessment=isinstance(_assess, dict),
            )
    # 169: lift reviewer findings when reviewer reports changes_requested.
    # Uses helper _lift_review_findings to validate and copy the ReviewRecord.
    if stage == "reviewing" and marker == "changes_requested":
        _review_report = status.get("report") if isinstance(status.get("report"), dict) else {}
        _lift_review_findings(state, _review_report, stage)
    return None

async def phase_875(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    f = ctx.f
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # 164: stash the QA repair brief for the next implementer dispatch,
    # mirroring how review findings already travel. Reports what failed
    # and how to reproduce it; never prescribes a fix (FR-014). Lifted on
    # BOTH terminal verdicts: a failing QA is the one whose brief the
    # implementer actually needs, and lifting only on qa_passed dropped it.
    if stage == "qa" and marker in ("qa_passed", "qa_failed"):
        _brief_report = status.get("report") if isinstance(status.get("report"), dict) else {}
        _qa_findings = _brief_report.get("qa_findings")
        if isinstance(_qa_findings, list):
            state["qa_findings"] = [f for f in _qa_findings if isinstance(f, dict)]
        else:
            # A QA round that emitted no brief must not leave an earlier
            # round's brief in state (review finding: the stale list would
            # be injected into the next implementer as if it were current).
            # pop, not assign: a QA role WITHOUT a workflow emits no
            # qa_findings, and writing an empty list would add a state key
            # that did not exist before 164. FR-005 promises the no-workflow
            # path behaves exactly as it did, and "exactly" includes not
            # growing state.
            state.pop("qa_findings", None)
    ctx.f = f
    return None

async def _phase_qa_passed(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _ev = ctx._ev
    card_id = ctx.card_id
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    if stage == "qa" and marker == "qa_passed":
        from coordinare.services.qa_verdict import (
            classify_qa_verdict,
            qa_unsubstantiated_reason,
        )

        _qa_report = status.get("report") if isinstance(status.get("report"), dict) else {}
        # 129 US2: the capture-unavailable→HOLD branch is coupled to the same
        # operator flag that enables US1's blocked-card recovery (which is what
        # would pick the HOLD back up). Off → prior bounce behavior, no regression.
        _capture_recovery = os.getenv("COORDINARE_BLOCKED_RECOVERY", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        _qa_route = classify_qa_verdict(
            marker,
            _qa_report,
            bool(status.get("env_cache_health_failed")),
            capture_recovery_enabled=_capture_recovery,
        )
        if _qa_route != "advance":
            _downgrade_reason = (
                qa_unsubstantiated_reason(_qa_report) or "unsubstantiated_pass"
            )
            _visual_evidence_n = sum(
                1
                for _ev in (_qa_report.get("visual_evidence") or [])
                if isinstance(_ev, dict) and _ev.get("path_or_url")
            )
            logger.warning(
                "monitor_performer.qa_evidence_floor_override",
                performer_stage=stage,
                card_id=card_id,
                route=_qa_route,
                criteria_checked=_qa_report.get("criteria_checked"),
                criteria_passed=_qa_report.get("criteria_passed"),
                visual_required=bool(_qa_report.get("visual_validation_required")),
                visual_evidence_count=_visual_evidence_n,
                reason=_downgrade_reason,
            )
            if _qa_route == "hold":
                # Reuse the existing qa_env_blocked handler below (HOLD + cache
                # repair + notify). The handler reads status["reason"].
                marker = "qa_env_blocked"
                status = {
                    **status,
                    "reason": f"qa_evidence_floor: {_downgrade_reason}",
                }
            else:  # bounce → reuse the qa_failed fix-feedback handler below.
                marker = "qa_failed"
                _synthetic_failure = {
                    "type": "unsubstantiated_pass",
                    "criterion": "QA reported a pass it did not substantiate",
                    "expected": (
                        "At least one acceptance criterion verified with execution "
                        "evidence (plus a screenshot for visual changes)."
                    ),
                    "actual": (
                        f"Coordinare evidence floor rejected the pass: "
                        f"{_downgrade_reason}."
                    ),
                }
                _existing_failures = status.get("failures")
                _failures = (
                    list(_existing_failures)
                    if isinstance(_existing_failures, list)
                    else []
                )
                _failures.append(_synthetic_failure)
                status = {**status, "failures": _failures}
    ctx._ev = _ev
    ctx.marker = marker
    ctx.status = status
    return None

async def phase_970(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _ha = ctx._ha
    _hb = ctx._hb
    config = ctx.config
    marker = ctx.marker
    policy = ctx.policy
    status = ctx.status
    # 072 FR-072-8..11: head-delta audit trail. Capture head_at_dispatch
    # the first time we see a non-empty head_before for this card's
    # current pass, and overwrite head_at_last_turn on every terminal
    # response that carries a non-null head_after. Allowlist the
    # terminal markers (matching the slot-release set just below) so
    # new non-terminal markers cannot accidentally trip this write.
    _hb = status.get("head_before")
    _ha = status.get("head_after")
    if isinstance(_hb, str) and _hb and not state.get("head_at_dispatch"):
        state["head_at_dispatch"] = _hb
    if marker in _TERMINAL_MARKERS_FOR_HEAD and isinstance(_ha, str) and _ha:
        state["head_at_last_turn"] = _ha
    # 030: Live requirement sync — detect card changes mid-cycle.
    # Only check when the performer is still working; terminal statuses
    # (success, error, etc.) take priority and must not be preempted.
    config = state.get("config")
    policy = "warn"
    if config is not None and hasattr(config, "requirement_change_policy"):
        policy = config.requirement_change_policy
    ctx._ha = _ha
    ctx._hb = _hb
    ctx.config = config
    ctx.policy = policy
    return None

async def _phase_status_gate(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    board_provider = ctx.board_provider
    card = ctx.card
    card_id = ctx.card_id
    github = ctx.github
    issue_id = ctx.issue_id
    marker = ctx.marker
    policy = ctx.policy
    stage = ctx.stage
    if (
        policy != "ignore"
        and marker == "working"
        and isinstance(card, dict)
        and github is not None
    ):
        issue_id_raw = card.get("issue_id")
        issue_id = str(issue_id_raw).strip() if issue_id_raw is not None else ""
        if issue_id:
            try:
                fresh = await board_provider.get_card(issue_id)
                old_desc = str(card.get("description", ""))
                new_desc = fresh.get("body") or fresh.get("description") or ""
                has_new_field = ("body" in fresh) or ("description" in fresh)
                if old_desc.strip() != new_desc.strip() and (new_desc.strip() or has_new_field):
                    state["requirements_changed"] = True
                    state["requirements_changed_details"] = {
                        "old_length": len(old_desc),
                        "new_length": len(new_desc),
                    }
                    if policy == "warn":
                        logger.warning(
                            "monitor_performer.requirements_changed",
                            card_id=card_id,
                            performer_stage=stage,
                            policy=policy,
                        )
                    elif policy == "re-dispatch":
                        logger.info(
                            "monitor_performer.requirements_changed.re_dispatch",
                            card_id=card_id,
                            performer_stage=stage,
                            workspace_policy="restart",
                        )
                        card["description"] = new_desc
                        with contextlib.suppress(Exception):
                            card["acceptance_criteria"] = parse_acceptance_criteria(new_desc)
                        _set_current_card(state, card)
                        state["phase"] = "dispatching"
                        state["agent_dispatch"] = {}
                        state["agent_dispatch_at"] = None
                        return state
            except asyncio.CancelledError:
                raise
            except (ConnectionError, TimeoutError, OSError, ValueError) as exc:
                logger.debug(
                    "monitor_performer.requirement_check_failed",
                    card_id=card_id,
                    error=str(exc),
                    exc_info=True,
                )
    ctx.issue_id = issue_id
    return None

async def _phase_stall_watchdog_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _dd_cfg = ctx._dd_cfg
    _lp = ctx._lp
    _made_progress = ctx._made_progress
    _now_w = ctx._now_w
    _stall_secs = ctx._stall_secs
    new_events = ctx.new_events
    _dd_cfg = getattr(state.get("coordinare_config"), "dispatcher_dedup", None)
    _stall_secs = int(getattr(_dd_cfg, "stall_timeout_seconds", 0) or 0)
    _now_w = datetime.now(UTC)
    # Progress = the work actually CHANGED since the last poll. NOT
    # ``bool(new_events)``: backends (e.g. codex) return their full
    # accumulated events list (capped) on every poll, so it is non-empty
    # and *stable* while the turn is wedged — using bool() there made the
    # watchdog think every poll was progress and never trip.
    #
    # Fingerprint recent event TEXT only. Deliberately NOT the event
    # count, NOT the token total, and NOT repr(): a model looping on
    # byte-identical output still grows the count, still burns tokens,
    # and repr() carries a per-event timestamp — each of those makes an
    # infinite repeat-loop read as progress on every poll, so the
    # watchdog could never trip on it (observed: an implementer emitted
    # the same "Task Complete" message 89+ times over 2h and was only
    # stopped by the performer's own session timeout).
    #
    # 327: the text itself is canonicalised so that a model looping on a
    # short CYCLE delivered as token deltas also reads as a stall. The
    # chunk boundaries land at a different point in the cycle on every
    # poll, so the raw text of the last few events is a different
    # rotation each time and kept resetting the timer (website #160
    # burned ~50 minutes that way with zero tool calls).
    _fp = progress_fingerprint(new_events)
    _prev_fp = state.get("last_progress_fingerprint")
    _made_progress = (_prev_fp is None) or (_fp != _prev_fp)
    state["last_progress_fingerprint"] = _fp
    _lp = state.get("last_progress_at")
    if _made_progress or not isinstance(_lp, datetime):
        state["last_progress_at"] = _now_w
        _lp = _now_w
    ctx._dd_cfg = _dd_cfg
    ctx._lp = _lp
    ctx._made_progress = _made_progress
    ctx._now_w = _now_w
    ctx._stall_secs = _stall_secs
    return None

async def _phase_stall_watchdog_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _budget = ctx._budget
    _dd_cfg = ctx._dd_cfg
    _lp = ctx._lp
    _made_progress = ctx._made_progress
    _now_w = ctx._now_w
    _sm = ctx._sm
    _stall_secs = ctx._stall_secs
    _window_h = ctx._window_h
    card_id = ctx.card_id
    decision = ctx.decision
    service = ctx.service
    session_id = ctx.session_id
    stage = ctx.stage
    if _stall_secs > 0:
        _stalled_for = (_now_w - _lp).total_seconds()
        logger.debug(
            "monitor_performer.stall_watchdog_checked",
            card_id=card_id,
            threshold_seconds=_stall_secs,
            made_progress=_made_progress,
            stalled_seconds=round(_stalled_for),
        )
        if _stalled_for > _stall_secs:
            logger.warning(
                "monitor_performer.stall_watchdog_tripped",
                card_id=card_id,
                performer_stage=stage,
                stalled_seconds=round(_stalled_for),
                threshold_seconds=_stall_secs,
            )
            # 138 T039: surface the trip in the UI whether or not a
            # notification channel exists (FR-010).
            _record_activity(
                state,
                "stall",
                f"no progress in {stage} for {round(_stalled_for)}s "
                f"(threshold {_stall_secs}s)",
                card_id=card_id,
                stage=stage,
            )
            # Kill the wedged turn first (best-effort) — it must not linger.
            try:
                from coordinare.services.dispatch_guard import drain_or_reap
                from coordinare.services.docker_executor import DockerExecutor
                if isinstance(session_id, str) and session_id:
                    await drain_or_reap(
                        session_id,
                        service=service,
                        docker_executor=DockerExecutor(),
                        drain_budget=float(getattr(_dd_cfg, "drain_budget_seconds", 5.0)),
                        reap_budget=float(getattr(_dd_cfg, "reap_budget_seconds", 5.0)),
                    )
            except Exception:  # pragma: no cover — defensive: kill failure must not block the decision
                pass
            from coordinare.services.retry_counter import record_idle_timeout
            _budget = int(getattr(_dd_cfg, "idle_timeout_retries", 2))
            _window_h = int(getattr(_dd_cfg, "idle_timeout_window_hours", 24))
            decision = record_idle_timeout(
                state, card_id, stage, budget=_budget, window_hours=_window_h,
            )
            state["last_progress_at"] = None
            if decision == "retry":
                state["performer_stage"] = stage
                state["phase"] = "dispatching"
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                return state
            # budget exhausted → BLOCK for operator triage
            _sm = state.get("slot_manager")
            if _sm is not None and hasattr(_sm, "release"):
                _sm.release(stage, card_id)
            state["phase"] = "blocked"
            state["system_error_reason"] = (
                f"performer stage '{stage}' stalled (no progress for "
                f"{round(_stalled_for)}s) and exhausted {_budget} retries"
            )
            state["open_questions"] = [
                f"The `{stage}` performer made no forward progress for "
                f"{round(_stalled_for)}s (stall watchdog) and exhausted the "
                f"{_budget}-retry budget in {_window_h}h — likely a wedged "
                f"upstream model. Operator intervention required.",
            ]
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
    ctx._budget = _budget
    ctx._sm = _sm
    ctx._window_h = _window_h
    ctx.decision = decision
    return None

async def _phase_stall_watchdog(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'working':
        for _sub in (_phase_stall_watchdog_s1, _phase_stall_watchdog_s2):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_terminal_markers(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _env_cache_svc = ctx._env_cache_svc
    _reason = ctx._reason
    _sym_name = ctx._sym_name
    card_id = ctx.card_id
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # 048: Release the performer slot on ANY terminal marker (success,
    # changes_requested, failed, error, blocked, session_expired) so
    # the next queued card can use the freed slot.  Must happen before
    # any branching because non-success paths (changes_requested,
    # security_failed, qa_failed, error) return early.
    _terminal_markers = TERMINAL_SUCCESS_STATES | {
        "changes_requested", "security_failed", "qa_failed", "qa_env_blocked",
        "env_blocked",
        "error", "blocked", "session_expired", "token_limit",
        "partial_progress",
        # 410: the decline intercept below returns early, so the slot
        # must release here like every other terminal marker.
        "assessment_not_work", "assessment_needs_split",
    }
    if marker in _terminal_markers:
        _slot_mgr = state.get("slot_manager")
        if _slot_mgr is not None and hasattr(_slot_mgr, "release"):
            _slot_mgr.release(stage, card_id)
    # --- 088 (FR-002/FR-003): QA environment-blocked — terminal non-success ---
    # The QA run claimed a pass it could not evidence because the container
    # environment was broken. There is no code defect to fix, so the card
    # must neither advance nor enter the fix-feedback cycle: hold it on the
    # SAME stage (the env-cache dispatch gate defers it until the cache is
    # repaired) and route the symphony to the forced re-verify machinery.
    if marker == "qa_env_blocked":
        _reason = str(status.get("reason") or "").strip() or (
            "QA reported an environment blocker with zero execution evidence"
        )
        logger.warning(
            "monitor_performer.qa_env_blocked",
            performer_stage=stage,
            card_id=card_id,
            reason=_reason,
        )
        _env_cache_svc = state.get("env_cache_service")
        _sym_name = state.get("current_symphony")
        if _env_cache_svc is not None and _sym_name:
            try:
                _env_cache_svc.mark_runtime_health_failed(_sym_name, state)
            except Exception as _exc:
                logger.warning(
                    "monitor_performer.qa_env_blocked_mark_failed",
                    card_id=card_id,
                    symphony=_sym_name,
                    error=str(_exc),
                )
        state["env_health_hold_reason"] = f"qa_env_blocked: {_reason}"
        state["phase"] = "dispatching"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    ctx._env_cache_svc = _env_cache_svc
    ctx._reason = _reason
    ctx._sym_name = _sym_name
    return None

async def phase_1206(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    pr_url = ctx.pr_url
    status = ctx.status
    # 263: remote CI infrastructure is unrelated to the dependency cache.
    ci_infra = (status.get("report") or {}).get("ci_infrastructure")
    if marker == "env_blocked" and isinstance(ci_infra, dict):
        state.update(_record_pr_artefacts(state, status))
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["system_error_reason"] = str(ci_infra.get("cause") or status.get("reason") or "CI infrastructure unavailable")
        pr_url = status.get("pr_url") or (state.get("current_card") or {}).get("pr_url")
        state["env_blocked"] = {**ci_infra, "pattern_id": "ci_infrastructure", "blocked_at": datetime.now(UTC).isoformat(),
                                "action": "Repair the shared CI infrastructure; the PR will recheck its own jobs"}
        # The next monitor cycle re-evaluates the shared CI gate, without a
        # model dispatch, local-cache invalidation or repair-counter update.
        state["phase"] = "monitoring_performer" if (pr_url and getattr(_get_ci_gate_config(state), "enabled", False)
                                    and getattr(_get_env_blocked_gate_config(state), "enabled", False)) else "blocked"
        return state
    ctx.pr_url = pr_url
    return None

async def _phase_local_test_gate_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _budget_exceeded = ctx._budget_exceeded
    _env_cache_svc = ctx._env_cache_svc
    _reason = ctx._reason
    _sym_name = ctx._sym_name
    card_id = ctx.card_id
    stage = ctx.stage
    status = ctx.status
    _reason = str(status.get("reason") or "").strip() or (
        "local tests failed with an environment blocker (no code defect)"
    )
    logger.warning(
        "monitor_performer.env_blocked",
        performer_stage=stage,
        card_id=card_id,
        reason=_reason,
    )
    _env_cache_svc = state.get("env_cache_service")
    _sym_name = state.get("current_symphony")
    # 402: a suite that outlived its budget with HEALTHY services is a gate
    # configuration finding the performer reports structurally. Hold the
    # card (an operator has to raise the budget) but do not regenerate a
    # cache that is fine; on 2026-09-13 that regen cost two bootstraps.
    _gate_report = (status.get("report") or {}).get("local_test_gate") if isinstance(status.get("report"), dict) else None
    _budget_exceeded = bool(isinstance(_gate_report, dict) and _gate_report.get("budget_exceeded"))
    if _budget_exceeded and isinstance(_gate_report, dict):
        logger.warning("monitor_performer.env_blocked_budget_exceeded", card_id=card_id,
                       performer_stage=stage, timeout_seconds=_gate_report.get("timeout_seconds"),
                       duration_seconds=_gate_report.get("duration_seconds"))
    if _env_cache_svc is not None and _sym_name and not _budget_exceeded:
        try:
            _env_cache_svc.mark_runtime_health_failed(_sym_name, state)
        except Exception as _exc:
            logger.warning(
                "monitor_performer.env_blocked_mark_failed",
                card_id=card_id,
                symphony=_sym_name,
                error=str(_exc),
            )
    ctx._budget_exceeded = _budget_exceeded
    ctx._env_cache_svc = _env_cache_svc
    ctx._reason = _reason
    ctx._sym_name = _sym_name
    return None

async def _phase_local_test_gate_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _budget_exceeded = ctx._budget_exceeded
    _reason = ctx._reason
    stage = ctx.stage
    # 399: leave the evidence recovery keys on. Spec-129a lifts an
    # ENV_BLOCKED card once the env cache is healthy again, but it can
    # only recognise the card via ``sess["env_blocked"]``. Only the
    # CI-infrastructure branch above stamped it; this branch, the one
    # every local-test-gate block takes, never did, so three cards sat
    # in BLOCKED with blockers=[] while every oracle said "recovered".
    # ``check_names`` is deliberately empty: recovery reads any names as
    # a CI hold that re-evaluates its own checks, and refuses to
    # env-recover it.
    #
    # 402: NOT for a budget finding. There the cache is healthy already, so
    # a recovery marker would lift the card on the next cycle, re-run the
    # same over-budget suite, block it again, and oscillate at a full run
    # per cycle. Raising the budget is a human action; the hold waits for it.
    if not _budget_exceeded:
        state["env_blocked"] = {
            "pattern_id": "local_test_gate",
            "stage": stage,
            "reason": _reason[:500],
            "blocked_at": datetime.now(UTC).isoformat(),
            "check_names": [],
            "action": "Nothing to do by hand: the env cache regenerates and "
                      "recovery lifts the card once it verifies healthy",
        }
    state["env_health_hold_reason"] = (
        f"env_blocked (test budget exceeded, services healthy): {_reason}"
        if _budget_exceeded else f"env_blocked: {_reason}"
    )
    state["phase"] = "blocked"
    # 123 FR-006: env_blocked is an infrastructure/transient failure — count
    # it toward the per-card transient budget (accumulates across the card's
    # lifetime; reset only on un-block) so it stays off the content budget
    # and the accounting is consistent with the system_error/unknown path.
    state["transient_error_cycles"] = int(
        state.get("transient_error_cycles") or 0,
    ) + 1
    return None

async def _phase_local_test_gate_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _reason = ctx._reason
    stage = ctx.stage
    if stage in ("reviewing", "closing_review"):
        # 169: the reviewer workflow holds when it could not post its one
        # review or could not read every changed file; neither is a test
        # environment problem and no code change fixes it.
        state["system_error_reason"] = f"the review could not complete (no code defect):\n{_reason}"
        state["open_questions"] = [
            f"The reviewer hit an environment blocker ({_reason}). The card is parked in the "
            "blocked column: check GitHub API access from the performer and the size of the "
            "injected diff, then re-run the review stage.",
        ]
    elif stage == "documenting":
        # 171: the documenter workflow holds when the tree was dirty at
        # start or the docs commit or push failed. No code change fixes it.
        state["system_error_reason"] = f"the documentation update could not complete (no code defect):\n{_reason}"
        state["open_questions"] = [
            f"The documenter hit an environment blocker ({_reason}). The card is parked in the "
            "blocked column: check the workspace for leftover changes and the push path for the "
            "branch, then re-run the documenting stage.",
        ]
    elif stage == "closing_review":
        # 172: the closer workflow holds when it could not read the review
        # threads, could not post its one review, or could not resolve a
        # thread it judged addressed. None of these is a code defect.
        state["system_error_reason"] = f"the closing review could not complete (no code defect):\n{_reason}"
        state["open_questions"] = [
            f"The closer hit an environment blocker ({_reason}). The card is parked in the blocked "
            "column: check GitHub API access from the performer, then re-run the closing review.",
        ]
    elif stage == "security":
        # 170: the security workflow holds when a scanner is missing or broken,
        # when it could not post its one review, or when it could not read
        # every changed file. No code change fixes any of these.
        state["system_error_reason"] = f"the security review could not complete (no code defect):\n{_reason}"
        state["open_questions"] = [
            f"The security stage hit an environment blocker ({_reason}). The card is parked in the "
            "blocked column: the hold names the tools the model chose and what went wrong with each, "
            "so check those run in the performer image, then GitHub API access from the performer and "
            "the size of the injected diff, then re-run the security stage.",
        ]
    else:
        state["system_error_reason"] = (
            f"local tests could not run due to an environment blocker "
            f"(no code defect); not pushing:\n{_reason}"
        )
        state["open_questions"] = [
            "The implementer's local test gate hit an environment blocker "
            f"(env-cache reason: {_reason}). The card is parked in the blocked "
            "column — no code change will fix this; an operator must repair the "
            "performer environment / env cache before the stage can re-run.",
        ]
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state
    return None

async def _phase_local_test_gate(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'env_blocked':
        for _sub in (_phase_local_test_gate_s1, _phase_local_test_gate_s2, _phase_local_test_gate_s3):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_documentation_findings(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    board_provider = ctx.board_provider
    card = ctx.card
    card_id = ctx.card_id
    github = ctx.github
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    from coordinare.services.documentation_findings import (
        collect as collect_documentation_findings,
    )
    collect_documentation_findings(state, stage, marker, status)
    # 410: an assessor verdict that declines the card ends the run here,
    # before any lifecycle advancement can read it as a terminal success.
    if stage == "assessing" and marker in ("assessment_not_work", "assessment_needs_split"):
        return await _apply_assessor_decline(state, card, card_id, marker, status, board_provider, github)
    return None

async def _phase_terminal_success_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # 098 US1 (FR-005 / contract invariant 2): the system-error retry
    # budget counts CONSECUTIVE failures, so a clean assessor response
    # must reset the counter — otherwise a flaky-then-clean assessment
    # carries a stale count forward and could block prematurely on a
    # later transient error. Scoped to the assessing stage so other
    # stages' behavior is unchanged (FR-007). Leaves system_error_notified
    # to the existing notified-recovery reset paths.
    if stage == "assessing" and state.get("system_error_count", 0):
        state["system_error_count"] = 0
        state["system_error_reason"] = None
    # 088 (FR-004): a terminal success whose own status payload carries
    # env_cache_health_failed is tainted — the services health check
    # failed in the very environment that produced the "success". The
    # flag already routed mark_runtime_health_failed above (063 wiring);
    # here it must also stop the advancement so the stage re-runs once
    # the cache is repaired.
    if status.get("env_cache_health_failed"):
        logger.warning(
            "monitor_performer.terminal_success_env_health_failed",
            performer_stage=stage,
            card_id=card_id,
            marker=marker,
        )
        state["env_health_hold_reason"] = "terminal_success_env_health_failed"
        state["phase"] = "dispatching"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    return None

async def _phase_terminal_success_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    card_id = ctx.card_id
    key = ctx.key
    stage = ctx.stage
    status = ctx.status
    value = ctx.value
    # 075: implementer CI gate runs at implementer→reviewer boundary
    # before stage advancement.  If the gate stops (bounce/hold/
    # escalate), apply updates and return without advancing.
    if stage == "implementing":
        # 077: persist the PR identifiers from the implementer's terminal
        # status BEFORE evaluating the gate. A HOLD verdict skips
        # _advance_stage (which is where artefacts are normally recorded),
        # so without this the card would lose its pr_url and the ephemeral
        # gate-only re-evaluation on the next cycle would have no PR to
        # gate on. Idempotent: _advance_stage re-records on PASS.
        _artefacts = _record_pr_artefacts(state, status)
        if "current_card" in _artefacts:
            state["current_card"] = _artefacts["current_card"]
            card = _artefacts["current_card"]
        # 126 (contract F1-F6): the terminal-success progress floor —
        # a feedback-driven completion must move the head past the
        # feedback-origin SHA or explicitly dispute items. Runs BEFORE
        # the CI gate (no point CI-gating a no-op); never touches the
        # content/transient budgets.
        floor_updates, floor_stop = _evaluate_success_floor(
            state, status if isinstance(status, dict) else {},
        )
        for key, value in floor_updates.items():
            state[key] = value  # type: ignore[literal-required]
        if floor_stop:
            return state
        pr_url_for_gate = status.get("pr_url") if status else None
        if not pr_url_for_gate and isinstance(card, dict):
            pr_url_for_gate = card.get("pr_url")
        # 090-L3 (US3): adjudicate a pending candidate repair via the dual
        # test-integrity guard BEFORE the CI gate. A weakened test could
        # turn the inherited check green precisely BECAUSE the requirement
        # was removed, so the guard must catch it before the CI gate would
        # forward it. No-op at all flag defaults (SC-006).
        if _pending_repair_dispatch(state) is not None:
            guard_updates, guard_stop = await _evaluate_repair_guard(
                state, card_id, pr_url_for_gate,
            )
            for key, value in guard_updates.items():
                state[key] = value  # type: ignore[literal-required]
            if guard_stop:
                return state
        ci_updates, ci_stop = await _evaluate_ci_gate(
            state, card_id, pr_url_for_gate,
        )
        for key, value in ci_updates.items():
            state[key] = value  # type: ignore[literal-required]
        # 095 (FR-008): clear stale env-hold dedup state on any non-env
        # verdict so a recurrence re-notifies (auto-resume / flapping).
        if "env_blocked" not in ci_updates and not _retain_infrastructure_hold(state, ci_updates):
            state["env_blocked"] = None
        if ci_stop:
            return state
    ctx.card = card
    ctx.key = key
    ctx.value = value
    return None

async def _phase_terminal_success_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    updates = ctx.updates
    # 126 US3 (contract U2): a docs_committed that changed nothing —
    # zero reported files AND no head movement this session — advances
    # (documentation is not a gating verdict; the honest "docs already
    # current" case is legitimate) but must NOT mint a documentation
    # pass: 125's last-documented SHA has to mean "docs were actually
    # produced at this head" or the doc-gate compare baseline lies.
    _doc_noop = False
    if stage == "documenting" and marker == "docs_committed":
        _files = status.get("files_modified") if isinstance(status, dict) else None
        _head_now = _settled_head(status if isinstance(status, dict) else {})
        _head_before = str(state.get("head_at_dispatch") or "")
        if not _files and (not _head_now or _head_now == _head_before):
            _doc_noop = True
            logger.info(
                "monitor_performer.documenting_noop_completion",
                card_id=card_id,
                head=_head_now[:12] if _head_now else "",
            )
    # 125 (contract R1-R4): record the stage's passing verdict against
    # the settled head BEFORE advancing, so the verdict-cache skip can
    # recognise this head as already verified on a later bounce.  Only
    # here — skips (persona-scope/override/cache) advance without a
    # record because nothing was verified.
    if not _doc_noop:
        _record_stage_verdict(state, marker, status if isinstance(status, dict) else {})
    # 126 (contract D2): a verdict stage passing while its disputes
    # were pending withdraws the demand — the dispute round resolves
    # accepted.
    if stage in VERDICT_STAGES and marker in EXPECTED_STAGE_MARKER.get(stage, frozenset()):
        _resolve_dispute_round(state, stage, passed=True)
    updates = _advance_stage(state, status, acknowledge_final_feedback=False)
    ctx.updates = updates
    return None

async def _phase_terminal_success_s4_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    card_id = ctx.card_id
    key = ctx.key
    pr_url = ctx.pr_url
    stage = ctx.stage
    updates = ctx.updates
    value = ctx.value
    updated_card = updates.get("current_card", card)
    pr_url = updated_card.get("pr_url")
    pr_node_id = updated_card.get("pr_node_id")
    if not pr_url or not pr_node_id:
        logger.error(
            "monitor_performer.final_stage_missing_pr_fields",
            card_id=card_id,
            performer_stage=stage,
            pr_url_present=bool(pr_url),
            pr_node_id_present=bool(pr_node_id),
        )
        # Reset stale error state inherited from a previous card so
        # this card gets its full retry budget and operator notification fires.
        if state.get("system_error_notified"):
            state["system_error_count"] = 0
            state["system_error_notified"] = False
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = (
            "Performer reported terminal success but pr_url or pr_node_id is missing"
        )
        state["phase"] = "system_error"
        return state
    # 064: Closer PR-checks gate — block handoff until required
    # GitHub checks pass on the PR's HEAD commit.
    gate_updates, gate_stop = await _evaluate_pr_checks_gate(
        state, card_id, pr_url,
    )
    if gate_stop and gate_updates.get("phase") == "dispatching":
        _carry_dispatched_feedback(state, gate_updates)
    for key, value in gate_updates.items():
        state[key] = value  # type: ignore[literal-required]
    if gate_stop:
        if gate_updates.get("phase") == "monitoring_performer":
            # The worker already succeeded. Its gone runtime is not a stale
            # execution to replace; retain the computed handoff until CI settles.
            state["current_card"] = {
                **updated_card, "status": updated_card.get("previous_status", "IN_PROGRESS"),
            }
            completed_at = ctx.updates.get("lifecycle_completed_at")
            state["pending_pr_handoff"] = {
                **(state.get("pending_pr_handoff") or {}),
                "stage": ctx.stage,
                "completed_at": completed_at.isoformat() if isinstance(completed_at, datetime) else completed_at,
            }
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            state["reconciled_dispatch_pending"] = False
        else:
            state["pending_pr_handoff"] = None
        # HOLD or BOUNCE — skip the move_card / reviewer / notification
        # side-effects.
        return state
    state["dispatched_feedback"] = {}
    state["pending_pr_handoff"] = None
    ctx.key = key
    ctx.pr_url = pr_url
    ctx.value = value
    return None

async def _phase_terminal_success_s4_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    board_provider = ctx.board_provider
    card_id = ctx.card_id
    github = ctx.github
    notification_service = ctx.notification_service
    pr_url = ctx.pr_url
    if github is not None:
        try:
            await move_card_or_warn(board_provider, card_id, "IN_REVIEW")
        except Exception:
            logger.warning("move_card_to_in_review_failed", card_id=card_id)
        # Request human reviewers configured on the project
        human_reviewers = state.get("human_reviewers")
        if pr_url and isinstance(human_reviewers, list) and human_reviewers:
            parsed = _pr_url_parts(pr_url)
            if parsed is None:
                logger.warning("request_human_reviewers_unparseable_pr_url", pr_url=pr_url)
            else:
                owner, repo, pr_num = parsed
                try:
                    await github.request_reviewers(owner, repo, pr_num, human_reviewers)
                except Exception as exc:
                    logger.warning("request_human_reviewers_failed", error=str(exc))
    # Notify that the card is ready for human review
    notification_service = state.get("notification_service")
    ctx.notification_service = notification_service
    return None

async def _phase_terminal_success_s4_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    card_id = ctx.card_id
    notification_service = ctx.notification_service
    pr_url = ctx.pr_url
    if notification_service is not None:
        from coordinare.models.notification import (
            EventType,
            NotificationEvent,
            NotificationSeverity,
        )
        card_title = str(card.get("title", ""))[:50]
        card_num = card.get("issue_number", "")
        card_ref = f"#{card_num} " if card_num else ""
        summary = f"👀 {card_ref}{card_title} — ready for human review"
        if pr_url:
            summary += f"\n   PR: {pr_url}"
        try:
            await notification_service.dispatch(
                NotificationEvent(
                    event_type=EventType.card_transition,
                    severity=NotificationSeverity.info,
                    payload={
                        "event_type": "card_ready_for_review",
                        "severity": "info",
                        "source": "lifecycle",
                        "summary": summary,
                        "card_title": str(card.get("title", "")),
                        "card_id": card_id,
                    },
                    source="lifecycle",
                    dedup_key=f"ready_for_review:{card_id}",
                ),
            )
        except Exception as exc:
            logger.warning("ready_for_review_notification_failed", error=str(exc))
    return None

async def _phase_terminal_success_s4(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    updates = ctx.updates
    if updates.get('phase') == 'monitoring_pr':
        for _sub in (_phase_terminal_success_s4_s1, _phase_terminal_success_s4_s2, _phase_terminal_success_s4_s3):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_terminal_success_s5(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    key = ctx.key
    updates = ctx.updates
    value = ctx.value
    # GC any per-card checks-gate cache now that the card is handing off
    # to monitoring_pr (gate already FORWARDed above).
    if updates.get("phase") == "monitoring_pr":
        ccs = state.get("card_checks_state") or {}
        if card_id in ccs:
            ccs = dict(ccs)
            ccs.pop(card_id, None)
            state["card_checks_state"] = ccs
    # Apply the computed state updates.
    for key, value in updates.items():
        state[key] = value  # type: ignore[literal-required]
    return state
    ctx.key = key
    ctx.value = value
    return None

async def _phase_terminal_success(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker in TERMINAL_SUCCESS_STATES:
        for _sub in (_phase_terminal_success_s1, _phase_terminal_success_s2, _phase_terminal_success_s3, _phase_terminal_success_s4, _phase_terminal_success_s5):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_review_routes_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    body = ctx.body
    card_id = ctx.card_id
    comments = ctx.comments
    stage = ctx.stage
    status = ctx.status
    raw_comments = status.get("comments", [])
    comments = raw_comments if isinstance(raw_comments, list) else []
    raw_body = status.get("body")
    body = raw_body.strip() if isinstance(raw_body, str) else ""
    logger.info(
        "monitor_performer.changes_requested",
        performer_stage=stage,
        card_id=card_id,
        comment_count=len(comments),
        has_body=bool(body),
    )
    ctx.body = body
    ctx.comments = comments
    return None

async def _phase_review_routes_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    body = ctx.body
    card_id = ctx.card_id
    comments = ctx.comments
    stage = ctx.stage
    status = ctx.status
    # --- 089 (US3): bounded local-test self-fix loop ---------------
    # The implementer ran the detected test command locally before
    # pushing and it failed for a CODE reason (env_blocked is handled
    # earlier as its own terminal marker). This is NOT a reviewer
    # bounce: it never touches spec-075's bounce_counter /
    # _feedback_cycle_exhausted machinery (SC-004). Instead we keep a
    # per-head local_fix_counter and re-dispatch the implementer with
    # the failing output while within the coordinare-only
    # max_fix_attempts budget; once exhausted we block the card
    # carrying the failing output — never pushing.
    if status.get("local_test_failed") and stage == "implementing":
        _head = str(status.get("head_after") or "").strip()
        _gate_cfg = _get_local_test_gate_config(state)
        _max_attempts = (
            int(getattr(_gate_cfg, "max_fix_attempts", 2))
            if _gate_cfg is not None
            else 2
        )
        _counter = dict(state.get("local_fix_counter") or {})
        _count = _counter.get(_head, 0) + 1
        _counter[_head] = _count
        state["local_fix_counter"] = _counter
        if _count <= _max_attempts:
            logger.info(
                "monitor_performer.local_test_failed_redispatch",
                performer_stage=stage,
                card_id=card_id,
                head=_head,
                attempt=_count,
                max_fix_attempts=_max_attempts,
            )
            state["relay_feedback"] = comments
            state["performer_stage"] = "implementing"
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
        _fail_blob = body or "\n".join(
            str(c.get("body", "")) for c in comments if isinstance(c, dict)
        )
        logger.warning(
            "monitor_performer.local_test_failed_escalate",
            performer_stage=stage,
            card_id=card_id,
            head=_head,
            attempt=_count,
            max_fix_attempts=_max_attempts,
        )
        state["phase"] = "blocked"
        state["system_error_reason"] = (
            f"local tests still failing after {_max_attempts} self-fix "
            f"attempt(s); not pushing:\n{_fail_blob}"
        )
        state["open_questions"] = [
            f"The implementer's local test gate failed "
            f"{_count - 1} consecutive self-fix attempt(s) (budget "
            f"{_max_attempts}) without converging. Failing output:\n"
            f"{_fail_blob}",
        ]
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    return None

async def _phase_review_routes_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    body = ctx.body
    card_id = ctx.card_id
    comments = ctx.comments
    exhausted = ctx.exhausted
    stage = ctx.stage
    # 065 Fix 4c: safety net — if performer reported changes_requested
    # but supplied neither structured comments nor a prose body, the
    # implementer would have no information to act on.
    #
    # 077: weak-model reviewers can flip-flop and emit a bare
    # changes_requested with no comments AND no body (observed: the same
    # reviewer APPROVED this exact PR hours earlier, then rejected it with
    # no rationale). Re-dispatch the reviewer ONCE before blocking — a
    # transient empty verdict usually resolves on a re-review. Only block
    # if it is STILL empty after the bounded retry, so the implementer is
    # never spun on empty feedback (065 Fix 4c intent preserved).
    if not comments and not body:
        _empty_retries = int(state.get("review_empty_retry_count", 0) or 0)
        if stage == "reviewing" and _empty_retries < 1:
            state["review_empty_retry_count"] = _empty_retries + 1
            logger.warning(
                "monitor_performer.changes_requested_empty_re_review",
                performer_stage=stage,
                card_id=card_id,
                attempt=_empty_retries + 1,
            )
            # Re-dispatch the SAME (reviewing) stage on the same PR.
            state["performer_stage"] = stage
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
        logger.warning(
            "monitor_performer.changes_requested_no_actionable_feedback",
            performer_stage=stage,
            card_id=card_id,
            empty_retries=_empty_retries,
        )
        state["review_empty_retry_count"] = 0
        state["phase"] = "blocked"
        state["system_error_reason"] = (
            f"performer reported changes_requested with no actionable "
            f"feedback after {_empty_retries} re-review(s) (stage={stage})"
        )
        state["open_questions"] = [
            f"The `{stage}` performer rejected this card "
            f"(`status=changes_requested`) but returned no structured "
            f"comments and no prose body, even after a re-review. "
            f"Nothing to relay to the implementer. Operator triage required.",
        ]
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    # Actionable feedback present — reset the empty-review retry counter.
    state["review_empty_retry_count"] = 0
    # Preserve the summary alongside inline comments. Each may contain
    # distinct instructions, and both must enter the feedback ledger.
    if body:
        comments = [*comments, {"body": body, "author_login": "coordinare"}]
    exhausted = _feedback_cycle_exhausted(state, card_id, stage, "changes_requested", comments)
    if exhausted is not None:
        return exhausted
    ctx.comments = comments
    ctx.exhausted = exhausted
    return None

async def _phase_review_routes_s4(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    body = ctx.body
    card_id = ctx.card_id
    comments = ctx.comments
    stage = ctx.stage
    status = ctx.status
    # 126 (D3): a raiser bouncing again while its disputes were
    # pending rejects them — the new round below stamps re_raised.
    _resolve_dispute_round(state, stage, passed=False)
    # 126 (L1): stamp the round — ids/raiser/origin head — so the
    # implementer success floor and the disposition contract can hold
    # this completion to account.
    comments = _stamp_feedback_bounce(
        state, comments, raiser=stage, origin_sha=_settled_head(status),
    )
    state["relay_feedback"] = comments
    # 123 US5 (FR-012/FR-013/FR-014): when REVIEWER feedback spans 2+
    # distinct concern categories, the work needs re-scoping — route it
    # through the assessor first (feedback rides along as relay context)
    # rather than straight back to the implementer. Uses the existing
    # keyword concern classifier (NO new AI call, FR-012). Single-category
    # or unclassifiable feedback routes directly to implementing (safe
    # default, FR-014). Gated on the assessing stage being in this
    # symphony's lifecycle.
    next_stage = "implementing"
    if stage == "reviewing":
        lifecycle_seq = list(state.get("lifecycle_sequence") or [])
        if "assessing" in lifecycle_seq:
            from coordinare.graph.nodes.classify_human_feedback import (
                classify_feedback_concerns,
            )
            categories = classify_feedback_concerns(
                [{"body": body, "comments": comments}],
            )
            if len(set(categories)) >= 2:
                next_stage = "assessing"
                # Reassessment scopes the correction; implementation still
                # owns it. Carry these exact requests through intervening roles.
                for item in comments:
                    item["delivery_stage"] = "implementing"
                logger.info(
                    "monitor_performer.multi_concern_route_to_assessing",
                    card_id=card_id,
                    categories=sorted(set(categories)),
                )
    state["performer_stage"] = next_stage
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state
    ctx.comments = comments
    return None

async def _phase_review_routes(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'changes_requested':
        for _sub in (_phase_review_routes_s1, _phase_review_routes_s2, _phase_review_routes_s3, _phase_review_routes_s4):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_security_failed_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    f = ctx.f
    findings = ctx.findings
    lifecycle = ctx.lifecycle
    stage = ctx.stage
    status = ctx.status
    target_role = ctx.target_role
    target_stage = ctx.target_stage
    raw_findings = status.get("findings", [])
    findings = raw_findings if isinstance(raw_findings, list) else []
    # 083 fail-closed: a scanner_unavailable / routing=halt finding means
    # the security floor could not be established (diff fetch or scanner
    # failure). Block the card for operator triage rather than routing it
    # to a performer — never let an unscanned change proceed.
    halt_findings = [
        f for f in findings
        if isinstance(f, dict) and f.get("routing") == "halt"
    ]
    if halt_findings:
        logger.warning(
            "monitor_performer.security_halt",
            performer_stage=stage,
            card_id=card_id,
            halt_count=len(halt_findings),
        )
        state["relay_feedback"] = findings
        state["phase"] = "blocked"
        state["system_error_reason"] = (
            "security scan floor unavailable — fail-closed halt"
        )
        state["open_questions"] = [
            str(f.get("description", "security scanner unavailable"))
            for f in halt_findings
        ]
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    lifecycle = state.get("lifecycle_sequence") or []
    # Determine earliest routing target from findings
    targets = set()
    for f in findings:
        if isinstance(f, dict):
            targets.add(f.get("routing", "implementer"))
    # Route to earliest: architect before implementer
    target_stage = "architecting" if "architect" in targets else "implementing"
    if target_stage not in lifecycle:
        target_stage = lifecycle[0] if lifecycle else "implementing"
    # Only relay findings targeted at this stage's role
    target_role = "architect" if target_stage == "architecting" else "implementer"
    ctx.f = f
    ctx.findings = findings
    ctx.lifecycle = lifecycle
    ctx.target_role = target_role
    ctx.target_stage = target_stage
    return None

async def _phase_security_failed_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    exhausted = ctx.exhausted
    f = ctx.f
    findings = ctx.findings
    stage = ctx.stage
    status = ctx.status
    target_role = ctx.target_role
    target_stage = ctx.target_stage
    relevant_findings = [
        f for f in findings
        if isinstance(f, dict) and f.get("routing", "implementer") == target_role
    ]
    logger.info(
        "monitor_performer.security_failed",
        performer_stage=stage,
        card_id=card_id,
        finding_count=len(findings),
        routed_count=len(relevant_findings),
        routing_target=target_stage,
    )
    exhausted = _feedback_cycle_exhausted(state, card_id, stage, "security_failed", relevant_findings)
    if exhausted is not None:
        return exhausted
    # 126 (D3 + L1): resolve pending disputes as rejected, then stamp
    # the new round (implementer-bound only — the floor and
    # dispositions apply to the implementing stage's completions).
    _resolve_dispute_round(state, "security", passed=False)
    if target_stage == "implementing":
        relevant_findings = _stamp_feedback_bounce(
            state, relevant_findings, raiser="security",
            origin_sha=_settled_head(status),
        )
    # 170: lift security workflow findings into review_findings when the
    # workflow ran and findings route to the implementer.
    _security_report = status.get("report") if isinstance(status.get("report"), dict) else {}
    _lift_security_findings(state, _security_report, target_stage)
    state["relay_feedback"] = relevant_findings
    state["performer_stage"] = target_stage
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state
    ctx.exhausted = exhausted
    ctx.f = f
    return None

async def _phase_security_failed(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'security_failed':
        for _sub in (_phase_security_failed_s1, _phase_security_failed_s2):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_qa_failed(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    exhausted = ctx.exhausted
    f = ctx.f
    failure_shape = ctx.failure_shape
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # --- QA failed (023-qa-performer) ---
    # Non-terminal: relay failures to implementer for remediation.
    if marker == "qa_failed":
        raw_failures = status.get("failures", [])
        failures = [f for f in (raw_failures if isinstance(raw_failures, list) else []) if isinstance(f, dict)]
        logger.info(
            "monitor_performer.qa_failed",
            performer_stage=stage,
            card_id=card_id,
            failure_count=len(failures),
        )
        exhausted = _feedback_cycle_exhausted(state, card_id, stage, "qa_failed", failures)
        if exhausted is not None:
            return exhausted
        # 126 (D3): reject pending QA disputes, then stamp the new round.
        _resolve_dispute_round(state, "qa", passed=False)
        # 126 (L1): stamp the QA round for the implementer floor.
        failures = _stamp_feedback_bounce(
            state, failures, raiser="qa", origin_sha=_settled_head(status),
        )
        state["relay_feedback"] = failures
        state["performer_stage"] = "implementing"
        state["phase"] = "dispatching"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    # 163: budget exhaustion is actionable before parse/empty-output retries.
    # Structured finish metadata is authoritative; older backends retain prose fallback.
    failure_shape = None
    if marker in {"error", "blocked", "token_limit"}:
        finish = status.get("finish_reason", status.get("stop_reason"))
        failure_shape = classify_assessor_failure(
            str(status.get("reason") or ""),
            finish_reason=finish if isinstance(finish, str) else None,
        )
        if failure_shape == "truncated":
            marker = "token_limit"
    ctx.exhausted = exhausted
    ctx.f = f
    ctx.failure_shape = failure_shape
    ctx.marker = marker
    return None

async def _phase_token_limit(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    card_id = ctx.card_id
    config = ctx.config
    github = ctx.github
    issue_id = ctx.issue_id
    marker = ctx.marker
    reason = ctx.reason
    stage = ctx.stage
    status = ctx.status
    # --- Token-cap exhaustion (055) ---
    if marker == "token_limit":
        from coordinare.services.attempt_telemetry import close_attempt
        close_attempt(state, "timeout", "system", "blocked")
        reason = str(status.get("reason", ""))
        from coordinare.graph.nodes.dispatch_performer import _persona_role_for_stage

        role = _persona_role_for_stage(stage) or stage
        config = state.get("config")
        role_config = (
            config.performers.resolved_role(role)
            if config is not None and hasattr(config, "performers") else None
        )
        current_max = getattr(role_config, "max_tokens", None)
        if current_max:
            advice = (
                f"The {stage} performer hit its output token cap ({current_max:,} tokens). "
                f"Raise `performers.{role}.max_tokens` to a larger positive value in your config."
            )
        else:
            advice = (
                f"The {stage} performer hit the backend's output token cap. "
                f"Set `performers.{role}.max_tokens` to a larger positive value."
            )
        logger.warning(
            "monitor_performer.token_limit",
            performer_stage=stage,
            card_id=card_id,
            max_tokens=current_max,
            reason=reason,
        )
        if github is not None:
            try:
                issue_id = card.get("issue_id") or card.get("content_id")
                if issue_id:
                    await github.add_comment(
                        str(issue_id),
                        f"**Performer blocked — output token limit reached**\n\n{advice}",
                    )
            except Exception as exc:
                logger.warning(
                    "monitor_performer.token_limit_comment_failed",
                    card_id=card_id,
                    error=str(exc),
                )
        state["phase"] = "blocked"
        state["open_questions"] = [advice]
        return state
    ctx.config = config
    ctx.issue_id = issue_id
    ctx.reason = reason
    return None

async def phase_1946(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _is_idle_timeout = ctx._is_idle_timeout
    marker = ctx.marker
    status = ctx.status
    # --- Idle-timeout (076 FR-019 / clarification Q3) ---
    # Performers that hit the claude-code reader idle timeout surface
    # the signal in one of three shapes (see
    # agent/performer/src/performer/backends/claude_code.py):
    #   1. explicit ``status=="idle_timeout"`` outcome (future-proof);
    #   2. ``stop_reason=="idle_timeout"`` field (the done-with-output
    #      case — handled as terminal-success elsewhere, but we still
    #      detect it here as a guard); or
    #   3. ``status=="error"`` with an ``error_reason`` of the form
    #      ``"claude CLI idle for 600s with no terminal event"`` (the
    #      no-output case — what card #101 actually hit).
    # The reason string says "idle for Ns", NOT "idle timeout", so we
    # match on the robust signature: any reason mentioning "idle" with
    # either "no terminal" or "idle for".  Route matches through the
    # rolling-window retry counter: 2 retries per (card, stage) per
    # 24h, then BLOCKED.
    _reason_lc = str(status.get("reason", "")).lower()
    _is_idle_reason = "idle" in _reason_lc and (
        "no terminal" in _reason_lc
        or "idle for" in _reason_lc
        or "idle timeout" in _reason_lc
    )
    _is_idle_timeout = (
        marker == "idle_timeout"
        or str(status.get("stop_reason", "")).lower() == "idle_timeout"
        or (marker == "error" and _is_idle_reason)
    )
    ctx._is_idle_timeout = _is_idle_timeout
    return None

async def _phase_idle_branch(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _budget = ctx._budget
    _dd_cfg = ctx._dd_cfg
    _drain_b = ctx._drain_b
    _is_idle_timeout = ctx._is_idle_timeout
    _reap_b = ctx._reap_b
    _window_h = ctx._window_h
    card_id = ctx.card_id
    decision = ctx.decision
    perf_svc = ctx.perf_svc
    prior_session_id = ctx.prior_session_id
    stage = ctx.stage
    if _is_idle_timeout:
        from coordinare.services.retry_counter import record_idle_timeout

        _dd_cfg = getattr(state.get("coordinare_config"), "dispatcher_dedup", None)
        _budget = int(getattr(_dd_cfg, "idle_timeout_retries", 2))
        _window_h = int(getattr(_dd_cfg, "idle_timeout_window_hours", 24))
        decision = record_idle_timeout(
            state, card_id, stage, budget=_budget, window_hours=_window_h,
        )
        if decision == "retry":
            # 076 (FR-007): drain the prior container before fresh
            # dispatch.  Best-effort; tolerates a hung drain.
            # Drain/reap budgets come from dispatcher_dedup config
            # so an operator can tune them per-deployment.
            try:
                from coordinare.services.dispatch_guard import drain_or_reap
                from coordinare.services.docker_executor import DockerExecutor

                prior_session_id = (state.get("agent_dispatch") or {}).get("session_id")
                if isinstance(prior_session_id, str) and prior_session_id:
                    perf_svc = (state.get("performer_services") or {}).get(stage)
                    _drain_b = float(getattr(_dd_cfg, "drain_budget_seconds", 5.0))
                    _reap_b = float(getattr(_dd_cfg, "reap_budget_seconds", 5.0))
                    await drain_or_reap(
                        prior_session_id,
                        service=perf_svc,
                        docker_executor=DockerExecutor(),
                        drain_budget=_drain_b,
                        reap_budget=_reap_b,
                    )
            except Exception:  # pragma: no cover — defensive: drain failure must not block the retry
                pass
            state["performer_stage"] = stage
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
        # decision == "block" — exhausted retry budget; move to BLOCKED
        from coordinare.services.attempt_telemetry import close_attempt
        close_attempt(state, "timeout", "system", "blocked")
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Performer stage '{stage}' idle-timed-out the configured "
            f"max ({_budget}) retries in {_window_h}h for card {card_id}. "
            "Operator intervention required.",
        ]
        return state
    ctx._budget = _budget
    ctx._dd_cfg = _dd_cfg
    ctx._drain_b = _drain_b
    ctx._reap_b = _reap_b
    ctx._window_h = _window_h
    ctx.decision = decision
    ctx.perf_svc = perf_svc
    ctx.prior_session_id = prior_session_id
    return None

async def phase_2016(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _dd_cfg = ctx._dd_cfg
    card_id = ctx.card_id
    decision = ctx.decision
    marker = ctx.marker
    stage = ctx.stage
    status = ctx.status
    # --- Empty-output terminal error (076 T171) ---
    # A performer that returns a terminal error whose reason indicates
    # empty / unusable output (e.g. "Backend produced an empty
    # architecture plan", "produced an empty implementation") would,
    # under the pre-T171 path, go blocked → handle_blocked finds no
    # questions → requeue → re-dispatch → empty again — an infinite
    # churn loop that re-runs a full (often 15+ min) stage every cycle
    # and pings Slack each time.  Route it through a low-budget
    # retry counter (default 1) so a deterministic capability failure
    # fails fast to BLOCKED-for-human instead of looping.  Populating
    # open_questions on the BLOCK keeps handle_blocked from requeuing.
    _empty_reason_lc = str(status.get("reason", "")).lower()
    _is_empty_output = marker == "error" and (
        "empty architecture plan" in _empty_reason_lc
        or "produced an empty" in _empty_reason_lc
        or "empty implementation" in _empty_reason_lc
        or "empty output" in _empty_reason_lc
    )
    if _is_empty_output:
        from coordinare.services.retry_counter import record_empty_output

        _dd_cfg = getattr(state.get("coordinare_config"), "dispatcher_dedup", None)
        _eo_budget = int(getattr(_dd_cfg, "empty_output_retries", 1))
        _eo_window = int(getattr(_dd_cfg, "empty_output_window_hours", 24))
        decision = record_empty_output(
            state, card_id, stage, budget=_eo_budget, window_hours=_eo_window,
        )
        if decision == "retry":
            state["performer_stage"] = stage
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
        # Budget exhausted → BLOCK for a human (do NOT requeue).  The
        # open_questions entry makes handle_blocked keep the card
        # blocked instead of re-dispatching it.
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Performer stage '{stage}' produced empty/unusable output "
            f"({str(status.get('reason', '')).strip()[:160]}) on "
            f"{_eo_budget + 1} attempt(s) for card {card_id}. This is "
            "typically a model-capability limit on a card too large for "
            "the configured model — consider a stronger model for this "
            "role or splitting the card. Operator intervention required.",
        ]
        return state
    ctx._dd_cfg = _dd_cfg
    ctx.decision = decision
    return None

async def _phase_error_status_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    assessor_retry = ctx.assessor_retry
    card_id = ctx.card_id
    failure_shape = ctx.failure_shape
    reason = ctx.reason
    stage = ctx.stage
    status = ctx.status
    reason = str(status.get("reason", ""))
    state["system_error_reason"] = reason or "The performer failed without a diagnostic reason"
    # 098 US1 (FR-001/FR-007): the assessor (junie) harness terminal-errors
    # whenever its strict parser cannot build issue.md from a flaky upstream
    # response (empty answer / control chars / empty body). Scoped to the
    # ``assessing`` stage so other backends' paths are untouched, classify
    # the failure *shape*; a recognised parse/empty shape routes into the
    # existing bounded system-error retry instead of the default terminal
    # block. A genuine model-capability (prose) failure returns None and is
    # NOT reclassified — it must not retry forever. The shape is recorded on
    # the persisted system_error_reason (re-classified at exhaustion for US3)
    # and emitted as a secret-free observability record (FR-004).
    assessor_shape = failure_shape
    assessor_retry = stage == "assessing" and assessor_shape in {
        "empty_answer", "empty_body", "malformed_body",
    }
    if assessor_shape is not None:
        logger.info(
            "assessor.parse_failure" if stage == "assessing" else "performer.parse_failure",
            card_id=card_id,
            stage=stage,
            shape=assessor_shape,
            attempt=int(state.get("system_error_count", 0)) + 1,
        )
    ctx.assessor_retry = assessor_retry
    ctx.reason = reason
    return None

async def _phase_error_status_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    assessor_retry = ctx.assessor_retry
    reason = ctx.reason
    if (
        reason.startswith(_FORMAT_ERROR_PREFIX)
        or _is_transient_backend_error(reason)
        or _is_format_contract_error(reason)
        or assessor_retry
    ):
        # Treat backend format-contract failures AND transient backend/
        # infrastructure crashes (subprocess_exit, server disconnected,
        # readiness timeout, transport reset, container start failure) as
        # retryable system errors — a flaky CLI or container hiccup must
        # not permanently park the card in Blocked. handle_system_error
        # controls backoff + budget and blocks after N consecutive fails.
        if state.get("system_error_notified"):
            state["system_error_count"] = 0
            state["system_error_notified"] = False
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        # 098 US1/US3: tag an assessor-shape failure at the reason source so
        # the persisted reason carries the format-error marker (matched by
        # the retry gate above on the next cycle) AND remains re-classifiable
        # by shape at exhaustion (empty_body → ENV_BLOCKED). Idempotent — do
        # not double-prefix an already-tagged reason.
        if (
            (assessor_retry or _is_format_contract_error(reason))
            and not reason.startswith(_FORMAT_ERROR_PREFIX)
        ):
            state["system_error_reason"] = f"{_FORMAT_ERROR_PREFIX} {reason}"
        else:
            state["system_error_reason"] = reason
        state["open_questions"] = []
        state["relay_feedback"] = []
        state["observer_correction"] = None
        state["phase"] = "system_error"
        return state
    return None

async def _phase_error_status_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    exhausted = ctx.exhausted
    lifecycle = ctx.lifecycle
    marker = ctx.marker
    reason = ctx.reason
    stage = ctx.stage
    if _is_workflow_push_permission_error(reason):
        logger.warning(
            "monitor_performer.workflow_push_permission_error",
            performer_stage=stage,
            card_id=card_id,
        )
        feedback = [{
            "body": (
                "Git push was rejected because the branch attempted to modify "
                "a GitHub Actions workflow file under `.github/workflows`, "
                "but the coordinare app token does not have `workflows` write "
                "permission. Revert workflow-file changes and continue with "
                "task-related code changes only."
            ),
        }]
        exhausted = _feedback_cycle_exhausted(
            state, card_id, stage, "workflow_push_permission", feedback,
        )
        if exhausted is not None:
            return exhausted
        lifecycle = list(state.get("lifecycle_sequence") or [])
        if "implementing" in lifecycle:
            state["relay_feedback"] = feedback
            state["performer_stage"] = "implementing"
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            state["open_questions"] = []
            return state
        logger.info(
            "monitor_performer.workflow_push_permission_no_implementing_stage",
            performer_stage=stage,
            card_id=card_id,
            lifecycle_sequence=lifecycle,
        )
    # Default terminal-error path: no smart branch matched. Emit a WARN so
    # an unclassified terminal failure is never silently parked in Blocked
    # with no operator-visible signal. An empty reason here points at the
    # upstream observability collapse (performer returned a terminal failure
    # with no `reason`), which the coordinare reason-fallback chain in
    # http_performer_service should normally backfill.
    logger.warning(
        "monitor_performer.terminal_error",
        performer_stage=stage,
        card_id=card_id,
        marker=marker,
        reason=reason or "<empty>",
    )
    state["phase"] = "blocked"
    state["open_questions"] = [
        f"Performer ({stage}) encountered an error: {reason}" if reason
        else f"Performer ({stage}) encountered an error.",
    ]
    return state
    ctx.exhausted = exhausted
    ctx.lifecycle = lifecycle
    return None

async def _phase_error_status(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'error':
        for _sub in (_phase_error_status_s1, _phase_error_status_s2, _phase_error_status_s3):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_blocked_questions_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    # Preserve any unanswered questions so the next performer receives them.
    open_qs = [str(q) for q in (state.get("open_questions") or [])]
    if open_qs:
        existing_clarifications = list(state.get("card_clarifications") or [])
        state["card_clarifications"] = [
            *existing_clarifications,
            {"questions": open_qs, "answer": ""},
        ]
    state["open_questions"] = []
    return None

async def _phase_blocked_questions_s2_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    card_id = ctx.card_id
    reason = ctx.reason
    stage = ctx.stage
    status = ctx.status
    # 044: Relay retry budget.  If session keeps expiring on the
    # same relay (e.g., performer stdout contaminated by CI tool
    # output), stop the loop after 3 consecutive failures and
    # block with a diagnostic so a human can investigate.
    error_count = state.get("system_error_count", 0)
    if error_count >= 3:
        from coordinare.services.attempt_telemetry import close_attempt
        close_attempt(state, "timeout", "system", "blocked")
        reason = str(status.get("reason", "unknown"))
        logger.warning(
            "monitor_performer.relay_retry_budget_exceeded",
            card_id=card_id,
            performer_stage=stage,
            system_error_count=error_count,
            reason=reason,
        )
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Relay failed {error_count} consecutive times on stage '{stage}'. "
            f"Last error: {reason}. Check performer logs for transport errors.",
        ]
        return state
    logger.info(
        "monitor_performer.session_expired_resume_monitoring_pr",
        card_id=card_id,
        performer_stage=stage,
        pr_node_id=card["pr_node_id"],
        system_error_count=error_count,
        msg="Session expired but PR already open — resuming monitoring_pr",
    )
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    state["phase"] = "monitoring_pr"
    # Update local card state immediately so check_board doesn't
    # keep seeing IN_PROGRESS and re-route to monitoring_agent
    # next cycle, which would create an infinite session_expired
    # loop.  The GitHub board move is a best-effort side effect;
    # if it fails, the local IN_REVIEW status still breaks the
    # loop, but we block the card so the operator can resolve the
    # board-state mismatch manually rather than letting it silently
    # drift.
    card["previous_status"] = card.get("status", "IN_PROGRESS")
    card["status"] = "IN_REVIEW"
    _set_current_card(state, card)
    ctx.reason = reason
    return None

async def _phase_blocked_questions_s2_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    board_provider = ctx.board_provider
    card = ctx.card
    card_id = ctx.card_id
    github = ctx.github
    stage = ctx.stage
    if github is not None:
        try:
            await move_card_or_warn(board_provider, card_id, "IN_REVIEW")
        except Exception as exc:
            logger.warning(
                "session_expired.move_card_in_review_failed",
                card_id=card_id,
                performer_stage=stage,
                pr_node_id=card.get("pr_node_id"),
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            state["phase"] = "blocked"
            state["open_questions"] = [
                f"Failed to move card {card_id!r} to IN_REVIEW after session "
                f"expiry for stage {stage!r}: {exc}. Blocking to avoid an "
                "infinite monitoring loop on stale board state.",
            ]
    return None

async def _phase_blocked_questions_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    if card.get('pr_node_id'):
        for _sub in (_phase_blocked_questions_s2_s1, _phase_blocked_questions_s2_s2):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_blocked_questions_s3_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    board_provider = ctx.board_provider
    card_id = ctx.card_id
    github = ctx.github
    reason = ctx.reason
    stage = ctx.stage
    status = ctx.status
    reason = str(status.get("reason", ""))
    logger.warning(
        "monitor_performer.session_expired_requeue",
        card_id=card_id,
        performer_stage=stage,
        reason=reason,
        msg="Session expired — moving card back to TODO for re-dispatch",
    )
    if github is not None:
        try:
            await move_card_or_warn(board_provider, card_id, "TODO")
        except Exception:
            logger.warning("move_card_to_todo_failed", card_id=card_id)
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    state["phase"] = "idle"
    ctx.reason = reason
    return None

async def _phase_blocked_questions_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card = ctx.card
    if not (card.get('pr_node_id')):
        for _sub in (_phase_blocked_questions_s3_s1,):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_blocked_questions_s4(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    return state
    return None

async def _phase_blocked_questions(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'session_expired':
        for _sub in (_phase_blocked_questions_s1, _phase_blocked_questions_s2, _phase_blocked_questions_s3, _phase_blocked_questions_s4):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_feedback_bounce_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _ha = ctx._ha
    _hb = ctx._hb
    card_id = ctx.card_id
    marker = ctx.marker
    next_focus = ctx.next_focus
    stage = ctx.stage
    status = ctx.status
    # 390: this relay is free only when the turn produced something.
    # The comment above says "committed/pushed work but did not
    # finish", and that premise is what makes the relay safe -- a run
    # that pushes nothing leaves the head unmoved, so bounce_counter
    # (keyed by head SHA) never counts it and the loop is unbounded.
    # Measured on card #160: two identical empty relays, ~25 min and a
    # 620s suite run each, with no ceiling.
    _hb, _ha = status.get("head_before"), status.get("head_after")
    # Produced only when BOTH heads are known AND they differ. Written
    # the other way round -- "not (known and known and equal)" -- an
    # absent head reads as produced and CLEARS the budget, which is the
    # unbounded loop this change exists to prevent, reintroduced through
    # a fail-open default. head_before/head_after are `str | None = None`
    # in the protocol, so a performer may legitimately omit them.
    # Not knowing whether work was produced is not evidence that it was.
    _produced = (
        isinstance(_hb, str) and isinstance(_ha, str)
        and bool(_hb) and bool(_ha) and _hb != _ha
    )
    if _produced:
        note_progress(state, stage)
    else:
        _spent = note_no_progress(state, stage)
        if should_block(state, stage):
            logger.warning(
                "monitor_performer.no_progress_budget_exhausted",
                performer_stage=stage, card_id=card_id,
                relays=_spent, limit=MAX_NO_PROGRESS_RELAYS, marker=marker,
            )
            _sm_np = state.get("slot_manager")
            if _sm_np is not None and hasattr(_sm_np, "release"):
                _sm_np.release(stage, card_id)
            state["phase"] = "blocked"
            state["open_questions"] = [
                f"Performer ({stage}) relayed {_spent} times without "
                "committing anything. Each relay repeated the same "
                "outcome, so more attempts will not help. "
                + (str(status.get("reason") or "").strip()
                   or "The last run reported no reason."),
            ]
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
    next_focus = status.get("next_focus")
    ctx._ha = _ha
    ctx._hb = _hb
    ctx.next_focus = next_focus
    return None

async def _phase_feedback_bounce_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _drain_b = ctx._drain_b
    _reap_b = ctx._reap_b
    card_id = ctx.card_id
    next_focus = ctx.next_focus
    perf_svc = ctx.perf_svc
    prior_session_id = ctx.prior_session_id
    relay_body = ctx.relay_body
    stage = ctx.stage
    focus_text = next_focus.strip() if isinstance(next_focus, str) else ""
    relay_body = (
        f"Continue from your previous partial_progress checkpoint. "
        f"Next focus: {focus_text}"
        if focus_text
        else "Continue from your previous partial_progress checkpoint."
    )
    logger.info(
        "monitor_performer.partial_progress",
        performer_stage=stage,
        card_id=card_id,
        has_next_focus=bool(focus_text),
    )
    # 076 (T085, FR-007 / clarification Q5): drain (then reap) the
    # prior container BEFORE clearing agent_dispatch.  The relay
    # handoff must not enter a state where agent_dispatch={} but a
    # container under the prior session_id is still making LLM
    # calls.  drain_or_reap has a hard cap of drain+reap budget
    # (default 5s+5s = 10s); budgets are pulled from
    # dispatcher_dedup config for per-deployment tuning.
    try:
        from coordinare.services.dispatch_guard import drain_or_reap
        from coordinare.services.docker_executor import DockerExecutor

        prior_session_id = (state.get("agent_dispatch") or {}).get("session_id")
        if isinstance(prior_session_id, str) and prior_session_id:
            perf_svc = (state.get("performer_services") or {}).get(stage)
            _dd_cfg2 = getattr(state.get("coordinare_config"), "dispatcher_dedup", None)
            _drain_b = float(getattr(_dd_cfg2, "drain_budget_seconds", 5.0))
            _reap_b = float(getattr(_dd_cfg2, "reap_budget_seconds", 5.0))
            await drain_or_reap(
                prior_session_id,
                service=perf_svc,
                docker_executor=DockerExecutor(),
                drain_budget=_drain_b,
                reap_budget=_reap_b,
            )
    except Exception as _exc:  # pragma: no cover — defensive
        logger.warning(
            "monitor_performer.relay_drain_failed",
            card_id=card_id,
            performer_stage=stage,
            error=str(_exc),
        )
    ctx._drain_b = _drain_b
    ctx._reap_b = _reap_b
    ctx.perf_svc = perf_svc
    ctx.prior_session_id = prior_session_id
    ctx.relay_body = relay_body
    return None

async def _phase_feedback_bounce_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    relay_body = ctx.relay_body
    stage = ctx.stage
    state["relay_feedback"] = [
        {"body": relay_body, "author_login": "coordinare", "source": "performer", "stage": stage},
    ]
    # 072: preserve the originating stage rather than coercing to
    # "implementing" — a reviewer that checkpointed should resume
    # as a reviewer, not be demoted into the implementer role.
    state["performer_stage"] = stage
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state
    return None

async def _phase_feedback_bounce(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    stage = ctx.stage
    if marker == 'partial_progress' and stage in SENTINEL_STAGES:
        for _sub in (_phase_feedback_bounce_s1, _phase_feedback_bounce_s2, _phase_feedback_bounce_s3):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_no_progress_relay_s1(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    head_after = ctx.head_after
    head_before = ctx.head_before
    status = ctx.status
    # 070: guardrail. If an implementing-stage turn reports blocked
    # but produced zero new commits, the model punted with a status
    # report instead of doing the work. Route back to dispatching
    # with a stronger directive instead of honoring the verdict.
    head_before = status.get("head_before")
    head_after = status.get("head_after")
    ctx.head_after = head_after
    ctx.head_before = head_before
    return None

async def _phase_no_progress_relay_s2(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    card_id = ctx.card_id
    head_after = ctx.head_after
    head_before = ctx.head_before
    marker = ctx.marker
    stage = ctx.stage
    if (
        stage == "implementing"
        and isinstance(head_before, str)
        and isinstance(head_after, str)
        and head_before
        and head_before == head_after
    ):
        # 390: same budget. Relaying "you pushed nothing, try again"
        # is worth doing once; doing it forever is the same unbounded
        # loop in a second place.
        _spent_nc = note_no_progress(state, stage)
        if should_block(state, stage):
            logger.warning(
                "monitor_performer.no_progress_budget_exhausted",
                performer_stage=stage, card_id=card_id,
                relays=_spent_nc, limit=MAX_NO_PROGRESS_RELAYS, marker=marker,
            )
            state["phase"] = "blocked"
            state["open_questions"] = [
                f"Performer ({stage}) ended {_spent_nc} turns without "
                "pushing any commits, after being told each time to "
                "finish or checkpoint.",
            ]
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
        logger.warning(
            "monitor_performer.blocked_no_commits_retry",
            performer_stage=stage,
            card_id=card_id,
            head=head_before,
            relays=_spent_nc,
        )
        state["relay_feedback"] = [
            {
                "body": (
                    "Your previous turn ended without pushing any new "
                    "commits. Resume the work; do not stop until your "
                    "changes are committed and pushed, or emit the "
                    "`partial_progress` JSON sentinel if you need to "
                    "checkpoint mid-task."
                ),
                "author_login": "coordinare",
            },
        ]
        state["performer_stage"] = "implementing"
        state["phase"] = "dispatching"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    return None

async def _phase_no_progress_relay_s3(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _clar_at_dispatch = ctx._clar_at_dispatch
    _clar_now = ctx._clar_now
    bot_comment_delta = ctx.bot_comment_delta
    status = ctx.status
    # 072 FR-072-5: per-role zero-progress guardrail for review-style
    # stages. Trip when ALL of: (a) head did not move,
    # (b) bot posted no new PR comments this turn, (c) no new
    # clarifications were appended (route_issue_comments and
    # check_board can mutate clarifications mid-turn, so we compare
    # the current length against the snapshot taken at dispatch).
    bot_comment_delta = status.get("bot_pr_comment_delta")
    if not isinstance(bot_comment_delta, int):
        bot_comment_delta = 0
    _current_clarifications = state.get("card_clarifications") or []
    _clar_now = (
        len(_current_clarifications)
        if isinstance(_current_clarifications, list) else 0
    )
    _clar_at_dispatch = int(state.get("clarifications_count_at_dispatch") or 0)
    ctx._clar_at_dispatch = _clar_at_dispatch
    ctx._clar_now = _clar_now
    ctx.bot_comment_delta = bot_comment_delta
    return None

async def _phase_no_progress_relay_s4(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    _clar_at_dispatch = ctx._clar_at_dispatch
    _clar_now = ctx._clar_now
    bot_comment_delta = ctx.bot_comment_delta
    card_id = ctx.card_id
    head_after = ctx.head_after
    head_before = ctx.head_before
    marker = ctx.marker
    stage = ctx.stage
    if (
        stage in ZERO_PROGRESS_REVIEW_STAGES
        and isinstance(head_before, str)
        and isinstance(head_after, str)
        and head_before
        and head_before == head_after
        and bot_comment_delta == 0
        and _clar_now == _clar_at_dispatch
    ):
        resume_directive = _ROLE_RESUME_DIRECTIVES.get(
            stage, _DEFAULT_RESUME_DIRECTIVE,
        )
        # 390: third instance of the same unbounded relay.
        _spent_zp = note_no_progress(state, stage)
        if should_block(state, stage):
            logger.warning(
                "monitor_performer.no_progress_budget_exhausted",
                performer_stage=stage, card_id=card_id,
                relays=_spent_zp, limit=MAX_NO_PROGRESS_RELAYS, marker=marker,
            )
            state["phase"] = "blocked"
            state["open_questions"] = [
                f"Performer ({stage}) produced nothing on {_spent_zp} "
                "consecutive turns: no commits, no PR comments, no "
                "clarifications.",
            ]
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state
        logger.warning(
            "monitor_performer.blocked_zero_progress_retry",
            performer_stage=stage,
            card_id=card_id,
            head=head_before,
            bot_comment_delta=bot_comment_delta,
            clarifications_delta=_clar_now - _clar_at_dispatch,
            relays=_spent_zp,
        )
        state["relay_feedback"] = [
            {"body": resume_directive, "author_login": "coordinare"},
        ]
        state["performer_stage"] = stage
        state["phase"] = "dispatching"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return state
    # A valid clarification response supersedes a prior retry error.
    # Current environment/CI holds retain their independent markers.
    state["system_error_reason"] = None
    state["phase"] = "blocked"
    return None

async def _phase_no_progress_relay_s5(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    stage = ctx.stage
    status = ctx.status
    questions = status.get("questions")
    if isinstance(questions, list) and questions:
        state["open_questions"] = [str(item) for item in questions]
        # 123 US4 (FR-010): persist the assessor's open questions so a
        # later assessor re-dispatch carries them forward as
        # prior_clarifications (FR-011) and does not re-ask them.
        # Distinct from open_questions above (blocked-card diagnostic
        # surface): stored as {"question","answer"} carry-forward Q&A.
        if stage == "assessing":
            state["assessor_open_questions"] = [
                {"question": str(item), "answer": ""} for item in questions
            ]
    else:
        # Blocked with no questions — assessment backend will generate them.
        state["open_questions"] = []
    return state
    return None

async def _phase_no_progress_relay(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """Phase helper: return a state to short-circuit, or None to continue."""
    marker = ctx.marker
    if marker == 'blocked':
        for _sub in (_phase_no_progress_relay_s1, _phase_no_progress_relay_s2, _phase_no_progress_relay_s3, _phase_no_progress_relay_s4, _phase_no_progress_relay_s5):
            _result = await _sub(state, ctx)
            if _result is not None:
                return _result
    return None

async def _phase_in_progress(state: CoordinareState, ctx: _BodyCtx) -> CoordinareState:
    """In-progress (working): workspace stays active; refresh backend UI."""
    ctx.teardown_on_exit = False  # still working — workspace stays active
    state["phase"] = "monitoring_performer"

    # 052: Backend transparency — discover UI URL and poll session stats.
    await _refresh_backend_ui(state, ctx.service, ctx.card_id)

    return state

def _record_observer_verdict(
    state: CoordinareState,
    *,
    card_id: str,
    stage: str,
    verdict: ObserverVerdict,
    triggers: list[str],
    evidence: dict[str, Any],
    now: datetime,
) -> None:
    """429: surface one observer verdict on the feed and the session view.

    The feed entry carries the verdict and its reason — the 138 dedup rules
    then collapse a repeated verdict instead of scrolling it. The session
    view's list additionally keeps the summarised evidence, so an operator
    can see not just what the coordinare did but why. Summaries only: the raw
    event text, diffs and prompts never reach either surface.
    """
    state["observer_verdicts"] = record_recent_verdict(
        state.get("observer_verdicts"), verdict, triggers, evidence, now.isoformat(),
    )
    _record_activity(
        state,
        "observer_verdict",
        f"{verdict.verdict}: {verdict.reason}",
        card_id=card_id,
        stage=stage,
    )

async def _phase_observer(
    state: CoordinareState,
    ctx: _BodyCtx,
) -> CoordinareState | None:
    """425: the observer core — wake a lightweight judge when triggers fire.

    Runs after the status gate and before the stall watchdog, so the
    repetition snapshot compares against LAST cycle's fingerprint (the
    watchdog writes this cycle's). Disabled means zero writes: monitoring is
    byte-identical. A dead or malformed observer is no action — never a
    wedge, never a bounce.
    """
    sym_name = state.get("current_symphony")
    observer_cfg = _enabled_observer_cfg(state)
    if sym_name is None or observer_cfg is None:
        return None

    card_id = ctx.card_id
    stage = ctx.stage
    now = datetime.now(UTC)

    # Repetition: compare this cycle's event-text fingerprint with LAST
    # cycle's. The stall watchdog runs later in the phase list and writes the
    # current fingerprint into the state, so at this point the state still
    # holds the previous poll's value.
    current_fp = progress_fingerprint(ctx.new_events)
    prev_fp = state.get("last_progress_fingerprint")
    repetition_count = (
        int(state.get("observer_repetition_count") or 0) + 1
        if prev_fp is not None and current_fp == prev_fp
        else 1
    )
    state["observer_repetition_count"] = repetition_count

    # Watchdog pending: mirror of the stall-watchdog trip condition, computed
    # from the same values the watchdog phases will read this cycle.
    dd_cfg = getattr(state.get("coordinare_config"), "dispatcher_dedup", None)
    stall_secs = int(getattr(dd_cfg, "stall_timeout_seconds", 0) or 0)
    last_progress_at = state.get("last_progress_at")
    watchdog_pending = (
        stall_secs > 0
        and prev_fp is not None
        and current_fp == prev_fp
        and isinstance(last_progress_at, datetime)
        and (now - last_progress_at).total_seconds() > stall_secs
    )

    # Quiet age: from the last produced-anything instant, falling back to
    # dispatch. Neither anchor is missing information, not evidence of quiet.
    prod_at = state.get("last_production_at")
    if not isinstance(prod_at, datetime):
        prod_at = state.get("agent_dispatch_at")
    quiet_age_s = (now - prod_at).total_seconds() if isinstance(prod_at, datetime) else 0.0

    evidence_stream = state.get("performer_events") or []
    ev = read_evidence(evidence_stream)
    evidence = {
        "quiet_age_s": round(quiet_age_s),
        "tool_uses": ev.tool_uses,
        "completions": ev.completions,
        "total_events": ev.total_events,
        "tokens_delta": int(ctx.tokens_delta or 0),
        "production_advanced": ctx.production_advanced,
        "repetition_count": repetition_count,
    }
    snapshot = TriggerSnapshot(
        quiet_age_s=quiet_age_s,
        quiet_window_s=float(getattr(observer_cfg, "quiet_window_seconds", 0.0) or 0.0),
        repetition_count=repetition_count,
        repetition_threshold=int(getattr(observer_cfg, "repetition_signature_threshold", 3) or 3),
        tokens_delta=evidence["tokens_delta"],
        token_burn_min_tokens=int(getattr(observer_cfg, "token_burn_min_tokens", 200_000) or 0),
        production_moved=ctx.production_advanced,
        watchdog_pending=watchdog_pending,
    )
    configured = getattr(observer_cfg, "triggers", None)
    enabled_triggers = frozenset(configured) if configured is not None else OBSERVER_TRIGGERS
    triggers = evaluate_triggers(snapshot, enabled_triggers)
    if not triggers:
        return None

    # Resolve the backend per wake. It is deliberately NOT cached on state:
    # symphony configs differ per symphony, so a shared cache would send one
    # symphony's evidence to another's model. Construction is trivially cheap
    # next to the observe call itself. A pre-seeded backend (tests, harnesses)
    # always wins.
    backend = state.get("observer_backend")
    if backend is None:
        global_cfg = getattr(state.get("coordinare_config"), "global_config", None)
        backend = build_model_endpoint_backend(global_cfg, observer_cfg.model_endpoint)
        if backend is None:
            return None

    # Metadata only (425 review): the prompt never carries raw event text.
    # Performer events are untrusted telemetry (command output, diffs); type
    # and length preserve the shape of the recent tail without the contents.
    recent_meta = [
        f"{e.get('type', 'event')}/{len(str(e.get('text', '')))}c"
        for e in evidence_stream[-5:]
        if isinstance(e, dict)
    ]
    verdict = await observe(
        backend,
        ObserverQuery(
            card_id=card_id,
            stage=stage,
            triggers=triggers,
            evidence=evidence,
            prompt=build_prompt(
                evidence,
                triggers,
                recent_meta,
                retune_bounds=getattr(observer_cfg, "retune_bounds", None),
            ),
        ),
    )
    if verdict is not None:
        state["observer_verdict"] = verdict.verdict
        _record_observer_verdict(
            state, card_id=card_id, stage=stage, verdict=verdict,
            triggers=triggers, evidence=evidence, now=now,
        )
        if verdict.verdict == "kill":
            # A kill only applies to a LIVE turn. Terminal statuses carry
            # completed or failed work that must advance through its normal
            # routing — killing here would rerun finished work.
            if (ctx.status or {}).get("status") != "working":
                logger.info(
                    "observer.kill_ignored_turn_not_live",
                    card_id=card_id,
                    performer_stage=stage,
                    turn_status=str((ctx.status or {}).get("status")),
                )
                return None
            return await _phase_observer_kill(state, ctx, verdict, evidence, dd_cfg)
        if verdict.verdict == "correction":
            pending = record_correction(state.get("observer_correction"), verdict.reason, evidence)
            if pending is not None:
                state["observer_correction"] = pending
                logger.info(
                    "observer.correction_pending",
                    card_id=card_id,
                    signature=pending["signature"],
                )
            else:
                logger.info(
                    "observer.correction_collapsed",
                    card_id=card_id,
                    signature=correction_signature(verdict.reason),
                )
            # A correction consumes the shared feedback-cycle budget exactly
            # like bounce feedback — including a collapsed repeat, or a
            # never-converging observer would escape the exhaustion bound.
            # Capture the live-run identity BEFORE the exhaustion helper
            # clears agent_dispatch — the cleanup below still needs it.
            _ad = state.get("agent_dispatch") or {}
            _session_id = _ad.get("session_id") if isinstance(_ad, dict) else None
            _svc = (state.get("performer_services") or {}).get(stage)
            exhausted = _feedback_cycle_exhausted(
                state, card_id, stage, "observer_correction", [{"body": verdict.reason}],
            )
            if exhausted is not None:
                # Blocking while the performer is still working orphans the
                # active turn: _phase_terminal_markers never runs for a
                # working poll, so the slot stays held and the container
                # lingers. Mirror the stall watchdog's block path — drain or
                # reap the live session, release the slot — before the early
                # return.
                try:
                    from coordinare.services.dispatch_guard import drain_or_reap
                    from coordinare.services.docker_executor import DockerExecutor
                    if isinstance(_session_id, str) and _session_id and _svc is not None:
                        await drain_or_reap(
                            _session_id,
                            service=_svc,
                            docker_executor=DockerExecutor(),
                            drain_budget=float(getattr(dd_cfg, "drain_budget_seconds", 5.0)),
                            reap_budget=float(getattr(dd_cfg, "reap_budget_seconds", 5.0)),
                        )
                except Exception as _exc:  # pragma: no cover — kill failure must not block the decision
                    logger.warning(
                        "observer.correction_exhaustion_cleanup_failed",
                        card_id=card_id,
                        error=repr(_exc),
                    )
                _slot_mgr = state.get("slot_manager")
                if _slot_mgr is not None and hasattr(_slot_mgr, "release"):
                    _slot_mgr.release(stage, card_id)
                return exhausted
        # 428: a structured retune request may ride ANY verdict (retune-on-
        # continue) — apply it whenever one is present. Kill keeps precedence
        # (it returns above); correction and continue fall through to here.
        if verdict.retune:
            _apply_observer_retune(state, ctx, verdict, observer_cfg)
    return None


_RETUNE_AUDIT_CAP = 50


def _apply_observer_retune(
    state: CoordinareState,
    ctx: _BodyCtx,
    verdict: ObserverVerdict,
    observer_cfg: Any,
) -> None:
    """428: fold a ``retune`` verdict into the symphony's orchestration knobs.

    Bounded by explicit config: only knobs declared in ``retune_bounds`` are
    adjustable, requests clamp into their floor/ceiling, and mode switches are
    refused outright. Fail-safe by construction: the override store is written
    only after the pure apply succeeds, so a retune failure leaves the previous
    value in place and never errors the cycle. Values survive the session and
    reset with it (``_retire_active_session`` clears them; a restart drops
    them — the store is not durably persisted).
    """
    card_id = ctx.card_id
    stage = ctx.stage
    sym_name = state.get("current_symphony")
    bounds = getattr(observer_cfg, "retune_bounds", None)
    if not sym_name or not bounds:
        logger.info(
            "observer.retune_ignored",
            card_id=card_id,
            performer_stage=stage,
            symphony=sym_name,
            reason="no_retunable_knobs_configured" if not bounds else "no_symphony_context",
        )
        return
    requested = verdict.retune or {}
    if not requested:
        logger.info(
            "observer.retune_ignored",
            card_id=card_id,
            performer_stage=stage,
            symphony=sym_name,
            reason="no_structured_request",
        )
        return
    stores = dict(state.get("observer_retunes") or {})
    current = dict(stores.get(sym_name) or {})
    audit = list(state.get("observer_retune_audit") or [])
    try:
        new_overrides, decisions = apply_retune(current, requested, bounds)
    except Exception as exc:  # pragma: no cover — apply_retune is total; belt-and-suspenders fail-safe
        logger.warning(
            "observer.retune_failed",
            card_id=card_id,
            performer_stage=stage,
            symphony=sym_name,
            error=repr(exc),
        )
        return
    now = datetime.now(UTC)
    for decision in decisions:
        audit.append({
            "at": now.isoformat(),
            "symphony": sym_name,
            "card_id": card_id,
            "knob": decision.knob,
            "before": decision.before,
            "requested": decision.requested,
            "applied": decision.applied,
            "outcome": decision.outcome,
        })
        if decision.accepted:
            logger.info(
                "observer.retune_applied",
                card_id=card_id,
                performer_stage=stage,
                symphony=sym_name,
                knob=decision.knob,
                before=decision.before,
                requested=decision.requested,
                applied=decision.applied,
                clamped=decision.outcome == "clamped",
            )
        else:
            logger.warning(
                "observer.retune_refused",
                card_id=card_id,
                performer_stage=stage,
                symphony=sym_name,
                knob=decision.knob,
                requested=decision.requested,
                outcome=decision.outcome,
            )
    if new_overrides != current:
        stores[sym_name] = new_overrides
        state["observer_retunes"] = stores
    state["observer_retune_audit"] = audit[-_RETUNE_AUDIT_CAP:]

async def _phase_observer_kill(
    state: CoordinareState,
    ctx: _BodyCtx,
    verdict: ObserverVerdict,
    evidence: dict[str, Any],
    dd_cfg: Any,
) -> CoordinareState | None:
    """427: a ``kill`` verdict stops the judged-dead turn and re-dispatches.

    The 076 drain-or-reap path owns the teardown (drain budget, then
    force-stop); the PR-artefact write-through runs BEFORE it so partial
    work (branches, PRs) is not forgotten. The kill-and-reprompt counts
    through the retry counter — an observer that keeps killing the same
    card hits the same escalation path the idle-timeout uses. The 076
    per-card mutex applies: a kill is deferred while a dispatch or
    reconciliation pass holds the card, and a kill that cannot be
    executed falls back to today's behavior (turn keeps running,
    failure logged) — never a wedge, never a lost workspace.
    """
    card_id = ctx.card_id
    stage = ctx.stage

    # Module-attribute access (not a from-import): the drain helper is
    # monkeypatched by the fail-safe tests, so it must resolve at call time.
    lock = dispatch_guard.acquire_dispatch_lock(card_id, stage)
    if lock.locked():
        # A dispatch or reconciliation pass holds the card — the kill is
        # deferred to the next cycle, exactly as a dispatch would be.
        logger.info(
            "observer.kill_deferred_mutex",
            card_id=card_id,
            performer_stage=stage,
        )
        return None

    await lock.acquire()
    try:
        # Artefact write-through precedes teardown: a failed kill still
        # records any partial work the dead turn already produced.
        updates = _record_pr_artefacts(state, ctx.status)
        if updates:
            state["current_card"] = updates["current_card"]

        session_id = ctx.session_id
        service = ctx.service
        try:
            if isinstance(session_id, str) and session_id and service is not None:
                # Module-attribute access (not a from-import): the stop
                # helper is monkeypatched by the fail-safe tests, so it
                # must resolve at call time. Unlike drain_or_reap (whose
                # best-effort contract the relay handoff relies on), the
                # honest signal here drives the fail-safe below.
                stopped, detail, _elapsed = await dispatch_guard.stop_session_turn(
                    session_id,
                    service=service,
                    docker_executor=DockerExecutor(),
                    drain_budget=float(getattr(dd_cfg, "drain_budget_seconds", 5.0)),
                    reap_budget=float(getattr(dd_cfg, "reap_budget_seconds", 5.0)),
                )
                if not stopped:
                    # The teardown did not happen (untracked session or the
                    # stop command failed): fail-safe — the turn keeps
                    # running, the operator sees the reason, no wedge and
                    # no duplicate dispatch.
                    logger.warning(
                        "observer.kill_failed",
                        card_id=card_id,
                        performer_stage=stage,
                        reason=detail,
                    )
                    return None
                # Release the service-side session bookkeeping (log-poll
                # task, job entry, client) through the service's own
                # teardown hook, so a kill leaves no stale poller behind.
                release = getattr(service, "_cleanup_ephemeral_job_by_id", None)
                if callable(release):
                    with contextlib.suppress(Exception):
                        await release(session_id)
        except Exception as exc:
            # Fail-safe: the kill could not be executed. The card falls
            # back to today's behavior — the turn keeps running and the
            # failure is logged, never a wedge.
            logger.warning(
                "observer.kill_failed",
                card_id=card_id,
                performer_stage=stage,
                error=repr(exc),
            )
            return None

        decision = record_observer_kill(state, card_id, stage)

        # The judged-dead session is stopped either way: release its
        # dispatch slot and slot-manager hold before routing.
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        slot_mgr = state.get("slot_manager")
        if slot_mgr is not None and hasattr(slot_mgr, "release"):
            slot_mgr.release(stage, card_id)

        if decision == "block":
            close_attempt(state, "timeout", "system", "blocked")
            state["phase"] = "blocked"
            state["open_questions"] = [
                f"Observer killed stage '{stage}' beyond its retry budget "
                f"in the 24h window for card {card_id} "
                f"(reason: {verdict.reason}). Operator intervention required.",
            ]
            logger.warning(
                "observer.kill_escalated",
                card_id=card_id,
                performer_stage=stage,
            )
            return state

        # Fresh run: the correction rides the payload (426 injection), and
        # the dead run's repetition evidence must not leak into it.
        pending = record_correction(state.get("observer_correction"), verdict.reason, evidence)
        if pending is not None:
            state["observer_correction"] = pending
        state["observer_repetition_count"] = 0
        state["phase"] = "dispatching"
        logger.info(
            "observer.kill_executed",
            card_id=card_id,
            performer_stage=stage,
            signature=correction_signature(verdict.reason),
        )
        return state
    finally:
        lock.release()

_PHASES = (
    _phase_board_reconcile,
    _phase_stall_expired,
    phase_458,
    poll_service,
    _phase_merge_events,
    phase_651,
    _phase_token_counters,
    _phase_security_scan_gate,
    phase_811,
    _phase_assessment_complete,
    phase_875,
    _phase_qa_passed,
    phase_970,
    _phase_status_gate,
    _phase_observer,
    _phase_stall_watchdog,
    _phase_terminal_markers,
    phase_1206,
    _phase_local_test_gate,
    _phase_documentation_findings,
    _phase_terminal_success,
    _phase_review_routes,
    _phase_security_failed,
    _phase_qa_failed,
    _phase_token_limit,
    phase_1946,
    _phase_idle_branch,
    phase_2016,
    _phase_error_status,
    _phase_blocked_questions,
    _phase_feedback_bounce,
    _phase_no_progress_relay,
)



async def _phase_pending_pr_handoff(state: CoordinareState) -> CoordinareState | None:
    """Resume a completed turn without polling a worker or cached verdict."""
    handoff = state.get("pending_pr_handoff")
    card = state.get("current_card")
    github = state.get("github_service")
    if handoff and state.get("phase") == "monitoring_performer" and not state.get("board_paused"):
        ctx = _build_monitor_ctx(state)
        if not ctx.bail and handoff.get("stage") == ctx.stage:
            get_context = getattr(github, "get_pr_review_context", None)
            if callable(get_context) and isinstance(card, dict) and card.get("pr_node_id"):
                try:
                    context = await get_context(str(card["pr_node_id"]))
                except Exception as exc:
                    logger.warning("monitor_performer.handoff_lifecycle_unreadable", error_type=type(exc).__name__)
                    return state
                if isinstance(context, dict) and context.get("state") in {"CLOSED", "MERGED"}:
                    from coordinare.graph.nodes.monitor_pr import monitor_pr

                    state["pending_pr_handoff"] = None
                    state["phase"] = "monitoring_pr"
                    return await monitor_pr(state)
            ctx.updates = _advance_stage(state, None, acknowledge_final_feedback=False)
            if ctx.updates.get("phase") == "monitoring_pr" and handoff.get("completed_at"):
                ctx.updates["lifecycle_completed_at"] = datetime.fromisoformat(str(handoff["completed_at"]))
            elif ctx.updates.get("phase") != "monitoring_pr":
                state["pending_pr_handoff"] = None
            # Re-use the normal final-check and board/reviewer handoff, without
            # polling a finished worker or applying cached stage overrides.
            stopped = await _phase_terminal_success_s4(state, ctx)
            if stopped is not None:
                return stopped
            return await _phase_terminal_success_s5(state, ctx) or state

    return None


async def _monitor_performer_body(state: CoordinareState) -> CoordinareState:
    """Poll the active performer and route based on status.

    Reads ``performer_stage`` from state, resolves the service from
    ``performer_services[performer_stage]``, and polls ``check_status``.
    Contains zero role-specific logic — all routing is driven by the
    status string returned by the performer.
    """
    # 030: Reset requirement-change flags at cycle start so stale flags never persist.
    # Placed before all early-return paths (incl. override check, service/card guard).
    state["requirements_changed"] = False
    state["requirements_changed_details"] = {}

    card = state.get("current_card")
    github = state.get("github_service")
    board_provider = board_of(state)

    override_state = await _phase_override(state, card, github, board_provider)
    if override_state is not None:
        if override_state.get("phase") != "monitoring_performer":
            override_state["pending_pr_handoff"] = None
        return override_state

    handoff_state = await _phase_pending_pr_handoff(state)
    if handoff_state is not None:
        return handoff_state

    ctx = _build_monitor_ctx(state)
    if ctx.bail:
        return state

    for prepare in (_phase_ephemeral_gate, _phase_slot_setup):
        prepared = await prepare(state, ctx)
        if prepared is not None:
            return prepared

    try:
        for _phase in _PHASES:
            _result = await _phase(state, ctx)
            if _result is not None:
                return _result
        return await _phase_in_progress(state, ctx)
    finally:
        if ctx.teardown_on_exit:
            await _teardown_workspace(state)
