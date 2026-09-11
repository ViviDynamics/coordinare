"""Generic, role-agnostic performer monitor node (019-performer-lifecycle).

Replaces ``monitor_agent`` with a node that resolves the active service from
``performer_services[performer_stage]`` and contains **zero** role-specific
logic (FR-004).  Terminal success states trigger lifecycle advancement via
``_advance_stage``.  Error status sets ``phase="blocked"`` per FR-006
(changed from the legacy ``system_error`` routing in ``monitor_agent``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

from coordinare.graph.attribution import coordinare_attribution
from coordinare.graph.state import _retire_active_session, _set_current_card
from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
from coordinare.metrics import METRICS
from coordinare.services.assessor_failure import classify_assessor_failure
from coordinare.services.base_gate import evaluate_base_gate
from coordinare.services.board_provider import board_of, move_card_or_warn
from coordinare.services.ci_gate import CIGateDecision, FailedCheck, FailedCheckWithSignature
from coordinare.services.env_signature import match_env_signature
from coordinare.services.failure_classification import BaselineFailure, classify_failure_origin
from coordinare.services.failure_signature import make_failure_signature, normalize_reason
from coordinare.services.github import PermanentGitHubError
from coordinare.services.pr_checks_policy import _is_failure, decide
from coordinare.services.pr_checks_service import PrChecksService
from coordinare.services.progress_fingerprint import progress_fingerprint
from coordinare.services.required_checks_resolver import resolve
from coordinare.services.test_integrity_guard import analyze_diff
from coordinare.services.workflow_step import (
    is_step_event,
    latch_declared_steps,
    latch_workflow_step,
)
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.services.pr_checks_service import CheckRollup

logger = structlog.get_logger(__name__)

# 358: how many performer events the session keeps for the dashboard.
MAX_PERFORMER_EVENTS = 100


def merge_performer_events(
    existing: list[Any], reported: list[Any], *, cap: int = MAX_PERFORMER_EVENTS
) -> list[Any]:
    """Merge a backend's re-reported event list into what we already hold (358).

    Backends re-report their entire accumulated event list on every poll, so
    concatenating produced a buffer full of copies: card #106 showed 40 stored
    entries that were three unique events repeated thirteen times, all carrying
    one timestamp. The Live Events panel renders this, so an operator watching a
    twelve-minute-old performer saw the same three step names over and over.

    Aligned on sequence rather than identity: find the longest suffix of what we
    hold that is also a prefix of what was just reported, and keep only the
    remainder. A set- or key-based dedup would instead drop legitimately
    repeated entries -- two identical delta chunks in a row are real, and
    collapsing them corrupts the stream.

    Correct whether the backend's list is fully cumulative (the overlap is
    everything we hold, so only genuinely new events are appended) or itself
    rolling (the overlap is partial, and nothing already dropped upstream is
    lost from our copy).
    """
    if not reported:
        return list(existing)[-cap:]
    if not existing:
        return list(reported)[-cap:]

    # Largest k where the tail of `existing` equals the head of `reported`.
    # Checked longest-first so a short accidental match cannot win over the
    # real overlap.
    for k in range(min(len(existing), len(reported)), 0, -1):
        if existing[-k:] == reported[:k]:
            return (list(existing) + list(reported[k:]))[-cap:]
    return (list(existing) + list(reported))[-cap:]


TERMINAL_SUCCESS_STATES: frozenset[str] = frozenset({
    "pr_opened",
    "plan_committed",
    "approved",
    "security_passed",
    "qa_passed",
    "docs_committed",
    "assessment_complete",
})
# 125: verdict stages whose passing terminal marker is recorded as a
# stage-verdict slot ({head_sha, verdict, recorded_at}) so dispatch can skip
# re-running a stage whose verdict already covers the current PR head.
# implementing/assessing are deliberately absent (FR-004: never skipped, never
# recorded). The marker↔stage map guards against a cross-wired response (e.g.
# a stray "approved" while the stage is qa) minting a verdict for the wrong
# stage.
VERDICT_STAGES: frozenset[str] = frozenset(
    {"reviewing", "security", "qa", "documenting", "closing_review"}
)
EXPECTED_STAGE_MARKER: dict[str, str] = {
    "reviewing": "approved",
    "security": "security_passed",
    "qa": "qa_passed",
    "documenting": "docs_committed",
    "closing_review": "approved",
}

# 072: performer stages for which a trailing partial_progress sentinel is
# honored. Architecting / assessing / closing-review / env_bootstrap are
# short single-turn roles where checkpointing does not apply.
#
# IMPORTANT: must stay in sync with ``SENTINEL_ROLES`` in
# ``agent/performer/src/performer/main.py``. The set is duplicated across
# the two processes (coordinare + performer container) because there is no
# shared library between them; if you add a role here, add it there too.
SENTINEL_STAGES: frozenset[str] = frozenset(
    {"implementing", "reviewing", "security", "qa", "documenting"}
)

# 072: per-role zero-progress guardrail applies to non-implementer
# review-style stages. The implementing stage uses the simpler
# single-signal (head-delta only) guardrail from 070 — commits ARE the
# progress signal there.
ZERO_PROGRESS_REVIEW_STAGES: frozenset[str] = frozenset(
    {"reviewing", "security", "qa", "documenting"}
)

# 072 FR-072-10: terminal markers that may carry a settled head_after worth
# recording on ``head_at_last_turn``. Mirrors the slot-release allowlist so
# new non-terminal markers cannot accidentally trip the audit-trail write.
_TERMINAL_MARKERS_FOR_HEAD: frozenset[str] = TERMINAL_SUCCESS_STATES | frozenset({
    "changes_requested", "security_failed", "qa_failed", "qa_env_blocked",
    "error", "blocked", "session_expired", "token_limit",
    "partial_progress",
})

# 072 FR-072-5: per-role "resume" relay-feedback text used when the
# zero-progress guardrail trips. Lifted to module scope so we don't rebuild
# the dict on every monitor pass.
_ROLE_RESUME_DIRECTIVES: dict[str, str] = {
    "reviewing": (
        "Resume your review of the PR diff; post comments on specific "
        "changes or emit `partial_progress` if you need to checkpoint."
    ),
    "security": (
        "Resume your security audit; surface findings as PR comments or "
        "emit `partial_progress` if you need to checkpoint."
    ),
    "qa": (
        "Resume your QA pass; post test results as PR comments or emit "
        "`partial_progress` if you need to checkpoint."
    ),
    "documenting": (
        "Resume your documentation pass; commit doc changes or emit "
        "`partial_progress` if you need to checkpoint."
    ),
}
_DEFAULT_RESUME_DIRECTIVE = "Resume your work on this card."

_FORMAT_ERROR_PREFIX = "BACKEND_FORMAT_ERROR:"
_WORKFLOW_PUSH_REJECTION_MARKERS: tuple[str, ...] = (
    "refusing to allow a github app to create or update workflow",
    "lacks `workflows` permission",
)

def _reset_token_counters(state: CoordinareState) -> None:
    """034: Clear token/cost counters when card is no longer active."""
    state["card_tokens_total"] = 0
    state["card_cost_estimate"] = 0.0
    state["card_budget_alert_sent"] = False
    from coordinare.metrics import METRICS
    METRICS.card_cost_estimate_dollars.set(0)


# 032: Phase → expected board column mapping for reconciliation
PHASE_TO_EXPECTED_COLUMN: dict[str, str] = {
    "monitoring_agent": "IN_PROGRESS",
    "monitoring_performer": "IN_PROGRESS",
    "monitoring_pr": "IN_REVIEW",
    "merging": "IN_REVIEW",
}


def _find_card_column(card_id: str, board_snapshot: dict[str, Any]) -> str | None:
    """Find which board column a card is in, or None if not found."""
    for column, items in board_snapshot.items():
        if isinstance(items, list) and card_id in items:
            return column
    return None


def _reconcile_board_mismatch(
    state: dict[str, Any],
    card_id: str,
    expected_column: str,
    actual_column: str | None,
) -> bool:
    """Check for board mismatch and reconcile state if needed.

    Returns True if reconciliation occurred (caller should return early).
    """
    if actual_column == expected_column:
        return False  # consistent — no action

    if actual_column is None:
        # Card disappeared from board — handled by 026 cancel logic
        logger.warning(
            "monitor_performer.reconcile.card_not_found",
            card_id=card_id,
            expected_column=expected_column,
        )
        state["phase"] = "idle"
        _retire_active_session(state, trigger="board_card_missing")
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        _reset_token_counters(state)
        return True

    # Backward move (e.g., IN_PROGRESS → TODO)
    if actual_column in ("TODO", "BACKLOG"):
        logger.warning(
            "monitor_performer.reconcile.backward_move",
            card_id=card_id,
            expected_column=expected_column,
            actual_column=actual_column,
        )
        state["phase"] = "idle"
        _retire_active_session(state, trigger="board_backward_move")
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["relay_feedback"] = []
        state["pending_reviews"] = []
        lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
        state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
        _reset_token_counters(state)
        return True

    # Forward move to DONE
    if actual_column == "DONE":
        logger.info(
            "monitor_performer.reconcile.forward_to_done",
            card_id=card_id,
            expected_column=expected_column,
        )
        lifecycle_seq = state.get("lifecycle_sequence") or ["implementing"]
        state["phase"] = "idle"
        _retire_active_session(state, trigger="board_card_done")
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["relay_feedback"] = []
        state["pending_reviews"] = []
        state["open_questions"] = []
        state["performer_stage"] = lifecycle_seq[0] if lifecycle_seq else "implementing"
        state["system_error_count"] = 0
        state["system_error_reason"] = None
        state["system_error_notified"] = False
        _reset_token_counters(state)
        return True

    # Move to BLOCKED
    if actual_column == "BLOCKED":
        logger.info(
            "monitor_performer.reconcile.moved_to_blocked",
            card_id=card_id,
            expected_column=expected_column,
        )
        state["phase"] = "blocked"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        return True

    # Any other column mismatch — log but don't act
    logger.debug(
        "monitor_performer.reconcile.unknown_column",
        card_id=card_id,
        expected_column=expected_column,
        actual_column=actual_column,
    )
    return False


async def _teardown_workspace(state: CoordinareState) -> None:
    """Tear down the workspace if one was prepared for this session.

    Clears workspace_path and workspace_branch from state regardless of
    teardown outcome so stale paths never accumulate.
    """
    workspace_manager = state.get("workspace_manager")
    workspace_path = state.get("workspace_path")
    try:
        if workspace_manager is not None and workspace_path is not None:
            await workspace_manager.teardown(workspace_path)
    except Exception:
        logger.warning("workspace_teardown_failed", workspace_path=str(workspace_path))
    finally:
        state["workspace_path"] = None
        state["workspace_branch"] = None
        # 052: Clear backend transparency fields when session ends.
        state["backend_ui_url"] = None
        state["session_stats"] = None
        state.pop("_backend_stats_fetched_at", None)  # type: ignore[typeddict-unknown-key]


_FEEDBACK_DIGEST_MAX = 200


def _stamp_feedback_bounce(
    state: CoordinareState,
    items: list[dict[str, Any]],
    raiser: str,
    origin_sha: str,
) -> list[dict[str, Any]]:
    """126 (contract L1-L3): stamp a new feedback round onto the ledger.

    Assigns stable per-card ids (``fb-<n>``), records the origin head the
    raising verdict was issued against, rolls the prior round to ``previous``
    (pruning anything older), resets the no-op retry budget ONLY when the
    origin head changed (contract L2 — a cross-raiser bounce at the same head
    is not progress and must not re-arm the F4/F5 floor), and returns enriched
    copies of ``items`` (``id``/``raiser``/``re_raised`` inline) for
    ``relay_feedback``.  New items are marked ``re_raised`` when the same
    raiser's previous round ended in a rejected dispute (contract D3).
    """
    ledger = [dict(r) for r in (state.get("feedback_ledger") or []) if isinstance(r, dict)]

    # D3: did this raiser's previous round end in a rejected dispute?
    re_raised = any(
        r.get("raiser") == raiser and r.get("disposition") == "dispute_rejected"
        for r in ledger
        if r.get("round_status") == "current"
    )

    # Roll rounds: current -> previous, previous -> pruned. Entries that never
    # reached a terminal disposition are closed as superseded first.
    rolled: list[dict[str, Any]] = []
    for r in ledger:
        if r.get("round_status") == "current":
            if r.get("disposition") in ("open", "addressed", "disputed"):
                r["disposition"] = "superseded"
            r["round_status"] = "previous"
            rolled.append(r)
    next_n = 1 + max(
        (int(str(r.get("id", "fb-0")).rsplit("-", 1)[-1]) for r in ledger
         if str(r.get("id", "")).startswith("fb-") and str(r.get("id")).rsplit("-", 1)[-1].isdigit()),
        default=0,
    )

    enriched: list[dict[str, Any]] = []
    for item in items:
        src = item if isinstance(item, dict) else {"body": str(item)}
        fb_id = f"fb-{next_n}"
        next_n += 1
        body = str(src.get("body") or src.get("description") or "")
        rolled.append({
            "id": fb_id,
            "raiser": raiser,
            "origin_sha": origin_sha or "",
            "body_digest": body[:_FEEDBACK_DIGEST_MAX],
            "disposition": "open",
            "dispute_reason": "",
            "re_raised": re_raised,
            "round_status": "current",
        })
        out = dict(src)
        out["id"] = fb_id
        out["raiser"] = raiser
        out["re_raised"] = re_raised
        # Prefix the id into the item's text field so the backend prompt
        # builders surface it and the implementer can echo it back in
        # feedback_dispositions. Review comments carry ``body``; security
        # findings carry ``description`` — prefix whichever this item uses
        # (prefer body). Items with neither keep their exact shape.
        if src.get("body"):
            out["body"] = f"[{fb_id}] {src['body']}"
        elif src.get("description"):
            out["description"] = f"[{fb_id}] {src['description']}"
        enriched.append(out)

    state["feedback_ledger"] = rolled  # type: ignore[typeddict-unknown-key]
    prior_origin = str(state.get("feedback_origin_sha") or "")
    state["feedback_origin_sha"] = origin_sha or None  # type: ignore[typeddict-unknown-key]
    # Reset the no-op retry budget only when the origin head actually changed
    # (contract L2). A different raiser bouncing at the SAME head is not
    # progress — resetting would let a no-op completion escape the F5 hold by
    # riding a cross-raiser bounce. ``!=`` already covers a new/absent origin
    # (e.g. "abc" -> "" or "" -> "abc"); an unchanged empty origin ("" -> "")
    # leaves the (already-unarmed) counter untouched.
    if origin_sha != prior_origin:
        state["noop_success_retries"] = 0  # type: ignore[typeddict-unknown-key]
    logger.info(
        "monitor_performer.feedback_round_stamped",
        card_id=str((state.get("current_card") or {}).get("id", "")),
        raiser=raiser,
        origin_sha=(origin_sha or "")[:12],
        item_count=len(enriched),
        re_raised=re_raised,
    )
    return enriched


def _settled_head(status: dict[str, Any]) -> str:
    """The performer-reported settled head for a terminal response ('' if none)."""
    raw = status.get("head_after") or status.get("head_sha")
    return raw.strip() if isinstance(raw, str) else ""


def _apply_feedback_dispositions(
    state: CoordinareState,
    dispositions: list[dict[str, Any]] | None,
    *,
    allow_addressed: bool = True,
) -> list[dict[str, Any]]:
    """126 (contract F2/F3, I4): apply per-item completion dispositions.

    Returns the newly-disputed ledger records (copies).  Unknown ids, repeat
    dispositions, and non-current-round targets are ignored with a log.  With
    ``allow_addressed=False`` (unmoved head, F3) an ``addressed`` claim does
    not close the item — only disputes count.
    """
    ledger = [dict(r) for r in (state.get("feedback_ledger") or []) if isinstance(r, dict)]
    by_id = {str(r.get("id", "")): r for r in ledger}
    disputed: list[dict[str, Any]] = []
    for d in dispositions or []:
        if not isinstance(d, dict):
            continue
        fb_id = str(d.get("id") or "")
        rec = by_id.get(fb_id)
        if rec is None or rec.get("round_status") != "current":
            logger.info("monitor_performer.disposition_unknown_id", id=fb_id)
            continue
        if rec.get("disposition") != "open":
            logger.info("monitor_performer.disposition_repeat_ignored", id=fb_id)
            continue
        disp = str(d.get("disposition") or "")
        if disp == "addressed":
            if allow_addressed:
                rec["disposition"] = "addressed"
            else:
                logger.info(
                    "monitor_performer.disposition_addressed_unverified",
                    id=fb_id,
                    reason="head unmoved — item stays open",
                )
        elif disp == "disputed":
            rec["disposition"] = "disputed"
            rec["dispute_reason"] = str(d.get("reason") or "")[:_FEEDBACK_DIGEST_MAX]
            disputed.append(dict(rec))
    state["feedback_ledger"] = ledger  # type: ignore[typeddict-unknown-key]
    return disputed


def _evaluate_success_floor(
    state: CoordinareState, status: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """126 (contract F1-F6, D4/D5): the implementer terminal-success floor.

    Returns ``(updates, stop)``. ``stop=True`` means the completion was NOT
    accepted (strengthened re-dispatch or operator hold); the caller applies
    the updates and returns without advancing.  Never touches the content or
    transient budgets (I2).  Fail-open: no stamped round or no resolvable
    completion head accepts as today (FR-011).
    """
    origin = str(state.get("feedback_origin_sha") or "")
    dispositions = status.get("feedback_dispositions")
    if not isinstance(dispositions, list):
        dispositions = []
    card_id = str((state.get("current_card") or {}).get("id", ""))
    # F1: no stamped feedback round — the floor is unarmed.
    if not origin:
        return {}, False
    head = _settled_head(status)
    # F6: cannot resolve the completion head — accept (fail-open).
    if not head:
        _apply_feedback_dispositions(state, dispositions)
        return {}, False
    # F2: real progress — accept, apply dispositions, disarm the round.
    if head != origin:
        _apply_feedback_dispositions(state, dispositions)
        state["feedback_origin_sha"] = None  # type: ignore[typeddict-unknown-key]
        state["noop_success_retries"] = 0  # type: ignore[typeddict-unknown-key]
        return {}, False
    # Head unmoved: only disputes carry weight (F3); addressed claims on an
    # unverifiable completion stay open.
    disputed = _apply_feedback_dispositions(state, dispositions, allow_addressed=False)
    if disputed:
        # D5: CI-raised items cannot be adjudicated by a stage — hold.
        ci_disputes = [d for d in disputed if d.get("raiser") == "ci"]
        # D4: a second dispute against a re-raised round gets no more laps.
        re_raised_disputes = [d for d in disputed if d.get("re_raised")]
        if ci_disputes or re_raised_disputes:
            reason = (
                "disputes a red CI check (CI cannot adjudicate)"
                if ci_disputes
                else "re-disputes feedback its raiser already re-raised"
            )
            logger.warning(
                "monitor_performer.success_floor_hold",
                card_id=card_id,
                head=origin[:12],
                cause=reason,
                disputed_ids=[str(d.get("id")) for d in disputed],
            )
            return (
                {
                    "phase": "blocked",
                    "open_questions": [
                        f"The implementer completed without moving the head "
                        f"({origin}) and {reason}: "
                        f"{', '.join(str(d.get('id')) for d in disputed)} — "
                        f"operator adjudication required."
                    ],
                    "agent_dispatch": {},
                    "agent_dispatch_at": None,
                },
                True,
            )
        # F3: legitimate dispute path — accept; the raiser adjudicates (D1).
        logger.info(
            "monitor_performer.dispute_queued",
            card_id=card_id,
            disputed_ids=[str(d.get("id")) for d in disputed],
        )
        return {}, False
    # F4/F5: no progress and nothing disputed.
    open_items = [
        r for r in (state.get("feedback_ledger") or [])
        if isinstance(r, dict)
        and r.get("round_status") == "current"
        and r.get("disposition") == "open"
    ]
    item_lines = "\n".join(
        f"- {r.get('id')}: {r.get('body_digest')}" for r in open_items
    )
    retries = int(state.get("noop_success_retries") or 0)
    if retries < 1:
        state["noop_success_retries"] = retries + 1  # type: ignore[typeddict-unknown-key]
        logger.warning(
            "monitor_performer.success_floor_retry",
            card_id=card_id,
            head=origin[:12],
            open_item_count=len(open_items),
        )
        directive = (
            "Your previous completion changed nothing: the branch head still "
            f"matches the commit this feedback was raised against ({origin}). "
            "Address each item below with commits, or mark it disputed with a "
            "reason in feedback_dispositions. Do not report done without one "
            f"or the other.\n{item_lines}"
        )
        return (
            {
                "relay_feedback": [{"body": directive, "author_login": "coordinare"}],
                "performer_stage": "implementing",
                "phase": "dispatching",
                "agent_dispatch": {},
                "agent_dispatch_at": None,
            },
            True,
        )
    logger.warning(
        "monitor_performer.success_floor_hold",
        card_id=card_id,
        head=origin[:12],
        cause="no progress after strengthened re-dispatch",
        open_item_count=len(open_items),
    )
    return (
        {
            "phase": "blocked",
            "open_questions": [
                f"The implementer reported done twice without moving the head "
                f"({origin}) and without disputing the outstanding feedback:\n"
                f"{item_lines}\nOperator triage required."
            ],
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        },
        True,
    )


def _resolve_dispute_round(
    state: CoordinareState, raiser_stage: str, *, passed: bool
) -> None:
    """126 (contract D2/D3): the raiser's next verdict adjudicates its disputes.

    Pass ⇒ ``dispute_accepted`` (demand withdrawn); bounce ⇒
    ``dispute_rejected`` — the subsequent stamped round is then marked
    ``re_raised`` (see _stamp_feedback_bounce).
    """
    ledger = [dict(r) for r in (state.get("feedback_ledger") or []) if isinstance(r, dict)]
    changed = False
    for r in ledger:
        # Scope to the CURRENT round only — a stale/corrupted previous-round
        # ``disputed`` entry must not be re-adjudicated by a later verdict.
        if (
            r.get("raiser") == raiser_stage
            and r.get("disposition") == "disputed"
            and r.get("round_status") == "current"
        ):
            r["disposition"] = "dispute_accepted" if passed else "dispute_rejected"
            changed = True
    if changed:
        state["feedback_ledger"] = ledger  # type: ignore[typeddict-unknown-key]
        logger.info(
            "monitor_performer.dispute_round_resolved",
            card_id=str((state.get("current_card") or {}).get("id", "")),
            raiser=raiser_stage,
            accepted=passed,
        )


def _record_stage_verdict(
    state: CoordinareState, marker: str, status: dict[str, Any]
) -> None:
    """125 (contract R1-R4): record a passing verdict slot for the stage.

    One slot per verdict stage, overwritten by each new passing verdict.  The
    head is the performer-reported settled head (``head_after``, the 072
    audit-trail field) falling back to ``head_sha``; with no resolvable head
    nothing is recorded — a missing slot simply dispatches next time (the safe
    direction).  Never records for implementing/assessing, marker/stage
    mismatches, or failure markers.
    """
    stage = str(state.get("performer_stage") or "")
    if stage not in VERDICT_STAGES or EXPECTED_STAGE_MARKER.get(stage) != marker:
        return
    head_raw = status.get("head_after") or status.get("head_sha")
    head = head_raw.strip() if isinstance(head_raw, str) else ""
    if not head:
        logger.debug(
            "monitor_performer.stage_verdict_no_head",
            performer_stage=stage,
            marker=marker,
        )
        return
    verdicts = dict(state.get("stage_verdicts") or {})
    verdicts[stage] = {
        "head_sha": head,
        "verdict": marker,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    state["stage_verdicts"] = verdicts
    logger.info(
        "monitor_performer.stage_verdict_recorded",
        performer_stage=stage,
        head_sha=head,
        verdict=marker,
        card_id=str((state.get("current_card") or {}).get("id", "")),
    )


def _apply_pending_override(state: CoordinareState) -> CoordinareState | None:
    """Check for and apply a pending human override (031-human-override-controls).

    Returns the updated state if an override was applied, or None if no
    override was pending.  The override is always cleared from state (FR-008).
    """
    override = state.get("pending_override")
    if override is None:
        return None

    action = override.get("action")
    state["pending_override"] = None  # FR-008: clear immediately

    if action == "skip":
        logger.info("override.skip", performer_stage=state.get("performer_stage"))
        updates = _advance_stage(state)
        for k, v in updates.items():
            state[k] = v  # type: ignore[literal-required]
        return state

    if action == "restart":
        target = override.get("target_stage", "")
        lifecycle = list(state.get("lifecycle_sequence") or [])
        if target in lifecycle:
            logger.info("override.restart", target_stage=target)
            state["performer_stage"] = target
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            # 125 (V1): an operator-requested restart must always dispatch —
            # this one-shot flag vetoes the verdict-cache skip for the target
            # stage and is consumed (cleared) by the cache check.
            state["override_forced_dispatch"] = target
        else:
            logger.warning("override.restart_invalid_role", target_stage=target)
        return state

    if action == "veto":
        logger.info("override.veto", card_id=(state.get("current_card") or {}).get("id"))
        state["phase"] = "blocked"
        state["open_questions"] = ["Lifecycle vetoed by human override."]
        return state

    logger.warning("override.unknown_action", action=action)
    return state


def _feedback_cycle_budget(state: CoordinareState) -> int:
    """Return ``config.max_feedback_cycles`` or the default (5).

    Helper so the monitor_performer callers don't each have to re-implement
    the config-may-be-None / attribute-may-be-missing guards.
    """
    config = state.get("config")
    raw = getattr(config, "max_feedback_cycles", 5) if config else 5
    return raw if isinstance(raw, int) else 5


def _summarise_feedback_items(
    items: list[dict[str, Any]], *, max_items: int = 5, max_chars_each: int = 220,
) -> list[str]:
    """Render the most recent feedback list (review comments, security
    findings, or QA failures) as a truncated bullet list for the block
    message.  Different roles stuff different fields into their items,
    so we try ``body`` (reviewer), ``description`` (security), ``actual``
    (QA), and finally ``str(item)``.  Empty strings are skipped.
    """
    out: list[str] = []
    for item in items[:max_items]:
        if not isinstance(item, dict):
            out.append(str(item)[:max_chars_each])
            continue
        parts: list[str] = []
        path = item.get("file") or item.get("path")
        line = item.get("line")
        if path:
            parts.append(f"`{path}{':' + str(line) if line else ''}`")
        body = (
            item.get("body")
            or item.get("description")
            or item.get("actual")
            or item.get("message")
            or ""
        )
        if not isinstance(body, str):
            body = str(body)
        body = body.strip().replace("\n", " ")
        if body:
            parts.append(body[:max_chars_each])
        if parts:
            out.append(" — ".join(parts))
    remaining = len(items) - max_items
    if remaining > 0:
        out.append(f"_…and {remaining} more_")
    return out


def _feedback_cycle_exhausted(
    state: CoordinareState,
    card_id: str,
    source_stage: str,
    reason_label: str,
    feedback_items: list[dict[str, Any]],
) -> CoordinareState | None:
    """Increment the card's feedback-cycle counter and block the card if
    we've exceeded the budget.

    Returns an updated ``state`` dict (to be returned by the caller) when
    the cycle limit has been reached, or ``None`` when the caller should
    proceed with the normal re-dispatch.  (045)

    The block message is Markdown with PR link, cycle count, the last
    round of feedback that wasn't getting addressed, and suggested
    actions — so the @-mentioned reviewer has everything they need to
    triage without digging through logs or cross-referencing the PR
    timeline.
    """
    # Mark content feedback even when the cycle budget is disabled.
    if state.get("last_attempt_id") and reason_label in {"changes_requested", "qa_failed", "security_failed"}:
        state["last_attempt_failure_source"] = (
            "qa_role" if source_stage in {"qa", "security"} else "human"
        )
    max_cycles = _feedback_cycle_budget(state)
    if max_cycles <= 0:
        return None  # 0 disables the bound
    # 123 US3 (FR-006/FR-007/FR-009): content_feedback_cycles is the operative
    # content-driven budget — reviewer/QA/security ``changes_requested`` rounds.
    # It is the persisted source of truth (feedback_cycle_count is NOT persisted
    # and resets to 0 on restart), so the count survives a daemon restart. The
    # legacy feedback_cycle_count is kept in lock-step for the dashboard and any
    # legacy readers. Infra/transient failures use transient_error_cycles
    # instead (see _transient_error_exhausted) and never touch this counter.
    # Read the MAX of the two counters so the budget is robust to either source
    # being the live one: content_feedback_cycles is the persisted survivor
    # across a restart (feedback_cycle_count is not persisted and resets to 0),
    # while legacy in-memory state may have only feedback_cycle_count set. Both
    # are written back in lock-step below.
    current = max(
        int(state.get("content_feedback_cycles") or 0),
        int(state.get("feedback_cycle_count") or 0),
    ) + 1
    state["content_feedback_cycles"] = current  # type: ignore[typeddict-unknown-key]
    state["feedback_cycle_count"] = current  # type: ignore[typeddict-unknown-key]
    # 065 US4 — monotonic lifetime counter; never resets on un-block.
    state["total_feedback_cycles"] = int(state.get("total_feedback_cycles") or 0) + 1  # type: ignore[typeddict-unknown-key]
    if current <= max_cycles:
        return None
    # 065 US4 — exhaustion path: bump triage_blocks (monotonic).
    state["triage_blocks"] = int(state.get("triage_blocks") or 0) + 1  # type: ignore[typeddict-unknown-key]
    logger.warning(
        "monitor_performer.feedback_cycle_exhausted",
        card_id=card_id,
        source_stage=source_stage,
        cycle_count=current,
        max_feedback_cycles=max_cycles,
        feedback_item_count=len(feedback_items),
    )

    card = state.get("current_card") or {}
    raw_reviewers = state.get("human_reviewers") or []
    mentions = " ".join(
        f"@{login}" for login in raw_reviewers if isinstance(login, str) and login.strip()
    )
    card_title = str(card.get("title") or "").strip()
    issue_number = card.get("issue_number") or 0
    pr_url = str(card.get("pr_url") or "").strip()

    header_bits: list[str] = []
    if mentions:
        header_bits.append(mentions)
    header_bits.append(
        f"**Triage needed — feedback loop hit {max_cycles}-cycle limit.**"
    )
    lines: list[str] = [" ".join(header_bits), ""]

    if card_title:
        label = f"#{issue_number} {card_title}" if issue_number else card_title
        lines.append(f"- **Card**: {label}")
    if pr_url:
        lines.append(f"- **PR**: {pr_url}")
    lines.append(f"- **Last stage requesting changes**: `{source_stage}` ({reason_label})")
    lines.append(f"- **Cycles used**: {current} (limit {max_cycles})")

    summary = _summarise_feedback_items(feedback_items)
    if summary:
        lines.append("")
        lines.append(f"**Latest {source_stage} feedback the implementer didn't resolve:**")
        lines.extend(f"- {s}" for s in summary)

    lines.extend([
        "",
        "The review → implement loop isn't converging. Options:",
        "1. Review the PR, accept as-is, and merge (if the remaining complaints are bogus).",
        "2. Leave a concrete comment on the PR telling the implementer exactly what to change, then move the card back to `IN_PROGRESS` to resume.",
        "3. Move the card to `BACKLOG` / `DONE` to abandon this attempt.",
    ])

    state["phase"] = "blocked"
    state["open_questions"] = ["\n".join(lines)]
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state


_TRANSIENT_BACKEND_ERROR_MARKERS = (
    "subprocess_exit",                 # backend CLI crashed / exited non-zero
    "server disconnected",             # container HTTP server died mid-request
    "readiness timeout",               # container slow/failed to come up
    "unreachable",
    "connection reset", "connectionreseterror", "econnreset",
    "connection refused",
    "transport error", "transporterror",
    "container start failed",
    "cannot write to closing transport",
)


def _is_transient_backend_error(reason: str) -> bool:
    """True for SYSTEM/infrastructure failures (backend CLI crash, container
    death, transport/readiness) that warrant an auto-retry rather than parking
    the card in Blocked. These are NOT content verdicts — a flaky CLI or a
    container hiccup shouldn't permanently block a card. The system_error path
    (backoff + budget) still blocks after N consecutive failures."""
    low = reason.lower()
    return any(m in low for m in _TRANSIENT_BACKEND_ERROR_MARKERS)


# 119: backend output-format-contract failures. A JSON-only role (tech_writer,
# assessor, ...) on a stochastic local reasoning model (gpt-oss:120b via hermes)
# occasionally emits output with no parseable JSON object — the backend reports
# `malformed_output`. This is NOT a content verdict and NOT deterministic: a
# re-dispatch almost always parses. Route it through the same bounded retry
# (handle_system_error: backoff + budget, blocks after N consecutive fails) that
# spec-098 gave assessor-shape / `BACKEND_FORMAT_ERROR:` failures, instead of
# terminal-blocking the card on the first bad roll. (Truncation — a genuinely
# too-large doc — is the deterministic case; it exhausts the budget and blocks,
# which is the right floor.)
_FORMAT_CONTRACT_ERROR_MARKERS = ("malformed_output",)


def _is_format_contract_error(reason: str) -> bool:
    """True for a backend output-format-contract failure (e.g. ``malformed_output``)
    that warrants a bounded retry rather than an immediate terminal block."""
    low = reason.lower()
    return any(m in low for m in _FORMAT_CONTRACT_ERROR_MARKERS)


# 138: backend event type → activity type. `output` is the backend's fallback
# for an unrecognised line, so it reads as plain progress in the feed.
_ACTIVITY_TYPE_BY_EVENT = {
    "progress": "progress",
    "tool_use": "tool_use",
    "thinking": "thinking",
    "cost": "cost",
    "error": "error",
    "output": "progress",
    "completed": "completed",
    "completion": "completed",
    "blocked": "blocked",
    "failed": "error",
    "failure": "error",
}


def _activity_attribution(state: CoordinareState, card_id: str, stage: str) -> dict[str, Any]:
    """138: the four attribution fields every feed entry carries (FR-005).

    ``card_id``/``stage`` are locals at the push sites; title and number are
    not — they come off ``current_card``.
    """
    card = state.get("current_card") or {}
    return {
        "card_id": card_id,
        "card_number": card.get("issue_number"),
        "card_title": card.get("title", ""),
        "stage": stage,
        "session_id": str((state.get("agent_dispatch") or {}).get("session_id") or ""),
        "performer_id": str((state.get("agent_dispatch") or {}).get("performer_id") or stage),
    }


def _record_activity(
    state: CoordinareState,
    activity_type: str,
    text: str,
    *,
    card_id: str,
    stage: str,
) -> None:
    """138: push one feed entry. No-ops without a log (tests, no dashboard)."""
    log = state.get("activity_log")
    if log is None:
        return
    with contextlib.suppress(Exception):
        log.record(
            activity_type=activity_type,
            text=text,
            **_activity_attribution(state, card_id, stage),
        )


def _record_activity_batch(
    state: CoordinareState,
    events: list[Any],
    *,
    card_id: str,
    stage: str,
) -> None:
    """138 T023: push a poll's worth of backend events as one batch."""
    log = state.get("activity_log")
    if log is None:
        return
    attribution = _activity_attribution(state, card_id, stage)
    with contextlib.suppress(Exception):
        log.record_many([
            {
                # 343: a step boundary is its own kind of entry, not generic
                # progress. Classified from the same predicate the latch uses,
                # so the feed and the trail can never disagree about what
                # counts as a step.
                "activity_type": (
                    "workflow_step"
                    if is_step_event(ev)
                    else _ACTIVITY_TYPE_BY_EVENT.get(str(ev.get("type", "")), "progress")
                ),
                "text": ev.get("text") or ev.get("detail") or "",
                "is_delta": ev.get("is_delta") is True,
                "stream_id": ev.get("stream_id", ""),
                "source_event_id": str(ev.get("timestamp") or ""),
                **attribution,
            }
            for ev in events
            if isinstance(ev, dict)
        ])


def _is_workflow_push_permission_error(reason: str) -> bool:
    """True when git push was rejected because workflow writes are disallowed."""
    lowered = reason.lower()
    return (
        "git push failed" in lowered
        and any(marker in lowered for marker in _WORKFLOW_PUSH_REJECTION_MARKERS)
    )


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


def _advance_stage(state: CoordinareState, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute the state update to advance the lifecycle to the next role.

    If more roles remain in ``lifecycle_sequence``, returns a dict that sets
    ``performer_stage`` to the next stage and resets dispatch state so the
    graph re-enters ``dispatching``.

    If no more roles remain, transitions to ``monitoring_pr`` and copies
    ``pr_url`` / ``pr_node_id`` from the terminal status into the card.
    """
    sequence: list[str] = list(state.get("lifecycle_sequence") or ["implementing"])
    current: str = state.get("performer_stage", "implementing")

    try:
        idx = sequence.index(current)
    except ValueError:
        # Unknown stage — treat as last so we fall through to monitoring_pr.
        idx = len(sequence)

    if idx + 1 < len(sequence):
        # More roles remain — advance to the next stage.
        # Persist PR identifiers from the current role's status so they're
        # available to subsequent roles (e.g. reviewer needs the PR URL).
        # 076 (T073, FR-015 + FR-016): _record_pr_artefacts also mirrors
        # to active_sessions[card_id] so a daemon restart loads the new
        # PR identifiers from snapshot rather than the stale ones.
        updates: dict[str, Any] = {
            "performer_stage": sequence[idx + 1],
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        }
        artefact_updates = _record_pr_artefacts(state, status)
        if "current_card" in artefact_updates:
            updates["current_card"] = artefact_updates["current_card"]
        return updates

    # All roles complete — transition to human review.
    # 076 (T073): write through any new PR identifiers BEFORE
    # transitioning so monitoring_pr sees the correct head SHA.
    artefact_updates = _record_pr_artefacts(state, status)
    card: dict[str, Any] = (
        artefact_updates.get("current_card")
        if isinstance(artefact_updates.get("current_card"), dict)
        else dict(state.get("current_card") or {})
    )

    # 043: CI lint gate — defence-in-depth before transitioning to
    # monitoring_pr.  Run the detected lint command on the workspace if
    # it's still available (performer may have already torn it down).
    # If lint fails, route back to the implementer with the failure output.
    # Note: _advance_stage is a sync function, so we use subprocess.run.
    workspace_path = state.get("workspace_path")
    if workspace_path is not None:
        from pathlib import Path as _Path

        ws = _Path(workspace_path) if not isinstance(workspace_path, _Path) else workspace_path
        if ws.is_dir():
            from coordinare.services.ci_detection import detect

            ci_result = detect(ws)
            if ci_result.lint_command:
                import shlex
                import subprocess

                try:
                    proc = subprocess.run(
                        shlex.split(ci_result.lint_command),
                        cwd=str(ws),
                        capture_output=True,
                        timeout=60,
                    )
                    if proc.returncode != 0:
                        lint_output = ((proc.stderr or b"") + (proc.stdout or b"")).decode("utf-8", errors="replace")[:2000]
                        # 065 US5 FR-021 — structured ci-failed log with the
                        # full {card_id, performer_stage, ci_command,
                        # exit_code, output_excerpt} field set.
                        logger.warning(
                            "performer.ci_failed",
                            card_id=str((state.get("current_card") or {}).get("id", "")),
                            performer_stage=str(state.get("performer_stage", "")),
                            ci_command=ci_result.lint_command,
                            exit_code=proc.returncode,
                            output_excerpt=lint_output[:500],
                        )
                        return {
                            "performer_stage": "implementing",
                            "phase": "dispatching",
                            "agent_dispatch": {},
                            "agent_dispatch_at": None,
                            "current_card": card,
                            "relay_feedback": [{"body": f"CI lint gate failed:\n```\n{lint_output}\n```", "author_login": "coordinare"}],
                        }
                    logger.info("performer.ci_passed", ci_command=ci_result.lint_command)
                except (subprocess.TimeoutExpired, OSError) as exc:
                    logger.warning("ci_gate.lint_error", command=ci_result.lint_command, error=str(exc))
                    # Don't block on gate execution errors — proceed to monitoring_pr
            else:
                logger.info("ci_gate.no_lint_detected", stack=ci_result.stack)
        else:
            logger.info("ci_gate.workspace_gone", workspace_path=str(workspace_path))
    else:
        logger.info("ci_gate.no_workspace_path")

    card["previous_status"] = card.get("status", "IN_PROGRESS")
    card["status"] = "IN_REVIEW"

    return {
        "phase": "monitoring_pr",
        "current_card": card,
        "system_error_count": 0,
        "system_error_last_at": None,
        "system_error_notified": False,
        "system_error_reason": None,
        # Record when the lifecycle completed so monitor_pr can ignore
        # reviews submitted before this point (they were already addressed
        # by the lifecycle roles).
        "lifecycle_completed_at": datetime.now(UTC),
    }


_STATS_POLL_INTERVAL_SECONDS = 30


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


def _get_closer_pr_checks_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's closer_pr_checks config (064).

    Returns the symphony's CloserPrChecksConfig if available, else None.

    Legacy single-symphony mode (no symphony_configs entry) intentionally
    returns None so the gate stays *off* until an operator opts in by
    configuring a symphony — gating remote checks is a behavior change that
    must not silently activate on upgrade.
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    return getattr(sym_cfg, "closer_pr_checks", None)


async def _evaluate_pr_checks_gate(
    state: CoordinareState,
    card_id: str,
    pr_url: str,
) -> tuple[dict[str, Any], bool]:
    """Run the closer PR-checks gate (spec 064).

    Returns a (state_updates, stop) tuple. `stop=True` means HOLD/BOUNCE — caller
    should apply updates and return without running handoff side-effects.
    `stop=False` means FORWARD (or gate disabled) — caller should apply updates
    then continue with the normal monitoring_pr transition.
    """
    cfg = _get_closer_pr_checks_config(state)
    if cfg is None or not getattr(cfg, "enabled", False):
        return {}, False

    parts = _pr_url_parts(pr_url)
    if parts is None:
        logger.warning("pr_checks_gate.unparseable_pr_url", pr_url=pr_url)
        return {}, False
    owner, repo, pr_num = parts

    github = state.get("github_service")
    if github is None:
        return {}, False

    # T029: Tick fast-path — if last poll is within poll_interval_seconds and the
    # prior decision was HOLD, skip the GraphQL query and re-HOLD.
    prior = (state.get("card_checks_state") or {}).get(card_id)
    # getattr fallback guards against legacy/duck-typed configs in tests.
    poll_interval = getattr(cfg, "poll_interval_seconds", 30)
    if prior and prior.get("last_decision") == "HOLD":
        try:
            last_polled = datetime.fromisoformat(prior["last_polled_at"])
            age = (datetime.now(UTC) - last_polled).total_seconds()
            if age < poll_interval:
                logger.debug(
                    "pr_checks_gate.fast_path_reuse", pr=pr_num, age=round(age, 1)
                )
                return {"phase": "monitoring_performer"}, True
        except (KeyError, ValueError, TypeError):
            pass  # fall through and re-query

    from coordinare.services.pr_checks_policy import decide
    from coordinare.services.pr_checks_service import PrChecksService

    svc = PrChecksService(github, owner, repo)
    try:
        rollup = await svc.get_pr_check_rollup(pr_num)
    except Exception as exc:
        if getattr(cfg, "fail_open_on_error", True):
            logger.warning(
                "pr_checks_gate.fail_open_on_error", pr=pr_num, error=str(exc)
            )
            return {}, False
        logger.warning("pr_checks_gate.error_blocking", pr=pr_num, error=str(exc))
        return (
            {
                "performer_stage": "implementing",
                "phase": "dispatching",
                "agent_dispatch": {},
                "agent_dispatch_at": None,
                "relay_feedback": [
                    {
                        "body": f"PR checks gate failed to query GitHub: {exc}",
                        "author_login": "coordinare",
                    }
                ],
            },
            True,
        )

    decision = decide(
        rollup,
        pending_timeout_seconds=getattr(cfg, "pending_timeout_seconds", 900),
        treat_unknown_required_as=getattr(cfg, "treat_unknown_required_as", "pass"),
    )

    # GraphQL `first: 100` cap: log once per HEAD so a long-context PR doesn't
    # spam the warning on every poll.
    if rollup.at_context_cap and (prior or {}).get("cap_hit_sha") != rollup.head_sha:
        logger.warning(
            "pr_checks.context_cap_hit",
            pr=pr_num,
            head=rollup.head_sha[:7],
        )

    # Cache for tick fast-path (US3).
    checks_state = dict(state.get("card_checks_state") or {})
    checks_state[card_id] = {
        "head_sha": rollup.head_sha,
        "last_polled_at": datetime.now(UTC).isoformat(),
        "last_decision": decision.action,
        "cap_hit_sha": rollup.head_sha if rollup.at_context_cap else (prior or {}).get("cap_hit_sha"),
    }

    if decision.action == "FORWARD":
        logger.info(
            "closer.pr_checks.decision",
            action="FORWARD",
            pr=pr_num,
            head=rollup.head_sha[:7],
            elapsed=round(decision.elapsed_seconds, 1),
        )
        return {"card_checks_state": checks_state}, False

    if decision.action == "HOLD":
        logger.info(
            "closer.pr_checks.decision",
            action="HOLD",
            pr=pr_num,
            head=rollup.head_sha[:7],
            pending=decision.pending,
            elapsed=round(decision.elapsed_seconds, 1),
        )
        # Stay in monitoring_performer; do not advance to monitoring_pr.
        return (
            {
                "phase": "monitoring_performer",
                "card_checks_state": checks_state,
            },
            True,
        )

    # BOUNCE
    if decision.reason == "pending_timeout":
        body = (
            f"PR checks gate: required checks still pending after "
            f"{int(decision.elapsed_seconds)}s. Pending: "
            f"{', '.join(decision.pending) or '(none)'}."
        )
    else:
        # Build a name → details_url map so the implementer can jump straight
        # to the failing job log without re-querying GitHub.
        url_by_name = {c.name: (c.details_url or "") for c in rollup.checks}
        lines = [
            "PR checks gate: required check(s) failed.",
            "",
            "Failing job(s):",
        ]
        for name in decision.failed:
            url = url_by_name.get(name, "")
            lines.append(f"- {name}: {url}" if url else f"- {name}")
        lines.extend([
            "",
            f"Fetch the failing log via `gh pr checks {pr_num}` to list the runs, "
            f"then `gh run view --log-failed <run-id>` for each failure. Read the "
            f"actual error, push a fix, and verify `gh pr checks {pr_num}` is green "
            f"before returning.",
        ])
        # 071 FR-004: inline log tails for each failing check so the implementer
        # gets the actual error in the first relay. Fair-share per-check budget
        # with an 800-char floor; fetch failures degrade silently to name-only.
        notification_service = state.get("notification_service")
        max_total_chars = (
            getattr(notification_service, "pr_checks_bounce_log_max_chars", 6000)
            if notification_service is not None
            else 6000
        )
        if max_total_chars > 0 and decision.failed:
            log_blocks: list[str] = []
            remaining = max_total_chars
            failed_names = list(decision.failed)
            for i, name in enumerate(failed_names):
                job_id = _parse_job_id_from_details_url(url_by_name.get(name, ""))
                if job_id is None:
                    continue
                slots_left = len(failed_names) - i
                slice_size = max(800, remaining // slots_left)
                try:
                    tail = await github.fetch_failed_job_log(
                        owner, repo, job_id, max_chars=slice_size
                    )
                except Exception as exc:
                    logger.warning(
                        "pr_checks.bounce_log_fetch_failed",
                        pr=pr_num,
                        job_id=job_id,
                        error=str(exc),
                    )
                    tail = ""
                if not tail:
                    continue
                log_blocks.append(
                    f"**Log tail (job {job_id}, check {name})**:\n```\n{tail}\n```"
                )
                remaining = max(0, remaining - len(tail))
                if remaining <= 0:
                    break
            if log_blocks:
                lines.append("")
                lines.extend(log_blocks)
        body = "\n".join(lines)
    logger.warning(
        "closer.pr_checks.decision",
        action="BOUNCE",
        pr=pr_num,
        head=rollup.head_sha[:7],
        reason=decision.reason,
        failed=decision.failed,
        pending=decision.pending,
    )
    return (
        {
            "performer_stage": "implementing",
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
            "card_checks_state": checks_state,
            "relay_feedback": [{"body": body, "author_login": "coordinare"}],
        },
        True,
    )


def _get_ci_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.ci_gate config (spec 075).

    Returns the ``CIGateConfig`` if available, else None.  Legacy
    single-symphony mode (no symphony_configs entry) returns None so the
    implementer CI gate stays off until an operator opts in.
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "ci_gate", None)


def _get_local_test_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.local_test_gate config (spec 089).

    Returns the ``LocalTestGateConfig`` if available, else None.  Used on the
    coordinare side to read the coordinare-only ``max_fix_attempts`` budget for
    the bounded local-test self-fix loop (US3).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "local_test_gate", None)


def _get_baseline_prevention_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.baseline_prevention_gate config.

    Spec 090 L1 (US1). Returns the ``BaselinePreventionGateConfig`` if available,
    else None. Legacy single-symphony mode (no symphony_configs entry) returns
    None so the L1 base-precondition gate stays off until an operator opts in —
    keeping merge decisions byte-identical to the pre-feature baseline (SC-006).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "baseline_prevention_gate", None)


def _get_baseline_classification_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.baseline_classification_gate config.

    Spec 090 L2 (US2). Returns the ``BaselineClassificationGateConfig`` if
    available, else None. Legacy single-symphony mode (no symphony_configs entry)
    returns None so the L2 observe-only classifier stays off until an operator
    opts in — keeping CI-gate decisions byte-identical to the pre-feature
    baseline (SC-006).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "baseline_classification_gate", None)


def _get_env_blocked_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.env_blocked_gate config (095).

    Returns the ``EnvBlockedGateConfig`` if available, else None — keeping
    ENV_BLOCKED classification off until an operator opts in (default-off,
    SC-006-equivalent).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "env_blocked_gate", None)


def _get_inherited_repair_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.inherited_repair_gate config.

    Spec 090 L3 (US3). Returns the ``InheritedRepairGateConfig`` if available,
    else None. Legacy single-symphony mode (no symphony_configs entry) returns
    None so autonomous baseline repair stays off until an operator opts in —
    keeping dispatch byte-identical to the pre-feature baseline (SC-006).
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    return getattr(persona_scope_cfg, "inherited_repair_gate", None)


# 090-L3 (US3): the durable do-not-weaken instruction carried on every repair
# mandate (contracts/repair-dispatch.md). It is a static coordinare-owned
# template — never agent-authored — so the prohibition cannot be paraphrased
# away across a long run (FR-016, FR-020).
_REPAIR_INSTRUCTION = (
    "A check that is already red on the base branch is failing on this PR for "
    "the same reason. Fix the underlying code or configuration so the check "
    "passes. You MUST NOT weaken, skip, xfail, delete, comment-out, mock-away, "
    "or loosen any test or assertion to make the check pass. If the only way to "
    "make it pass is to change a test's strictness, stop and leave the work for "
    "a human."
)


def _build_repair_mandate(
    *,
    state: CoordinareState,
    rollup: CheckRollup,
    inherited: list[FailedCheckWithSignature],
    head_sha: str,
    inheritance_repair_counter: dict[str, int],
) -> dict[str, Any] | None:
    """Build the 090-L3 ``repair_mandate`` for the INHERITED head failures.

    Returns ``None`` (and mutates nothing) unless the inherited-repair gate is
    enabled, at least one INHERITED failure is present, and the per-head repair
    budget has remaining attempts — keeping dispatch byte-identical to the
    pre-feature baseline at all flag defaults (SC-006). When it does build a
    mandate it increments the per-head budget AT dispatch (FR-018) so a crash
    after dispatch still consumes the attempt, and stamps ``attempt`` with the
    1-based post-increment value (always ``<= max_attempts``).

    Each ``normalized_reason`` is recomputed from the head ``CheckEntry`` via the
    SAME title/summary selection the classifier used, so it is byte-equal to the
    canonical string the classifier hashed (reason-fidelity; FR-016). The
    budget-exhausted escalation path is handled by the caller (T034).
    """
    cfg = _get_inherited_repair_gate_config(state)
    if cfg is None or not getattr(cfg, "enabled", False):
        return None
    if not inherited:
        return None
    max_attempts = getattr(cfg, "max_repair_attempts_per_head", 1)
    attempts_so_far = inheritance_repair_counter.get(head_sha, 0)
    if attempts_so_far >= max_attempts:
        # Budget exhausted — the caller escalates rather than dispatching.
        return None
    inheritance_repair_counter[head_sha] = attempts_so_far + 1
    attempt = inheritance_repair_counter[head_sha]

    head_by_name = {c.name: c for c in rollup.checks}
    inherited_checks: list[dict[str, Any]] = []
    for fc in inherited:
        entry = head_by_name.get(fc.name)
        if entry is not None and entry.conclusion is not None:
            title, summary = entry.title, entry.summary
        else:
            title, summary = None, None
        inherited_checks.append(
            {
                "name": fc.name,
                "conclusion": fc.conclusion,
                "normalized_reason": normalize_reason(title, summary),
                "html_url": fc.html_url,
            }
        )
    return {
        "type": "baseline_repair",
        "inherited_checks": inherited_checks,
        "attempt": attempt,
        "max_attempts": max_attempts,
        "instruction": _REPAIR_INSTRUCTION,
    }


def _l3_budget_exhausted(
    *,
    state: CoordinareState,
    inherited: list[FailedCheckWithSignature],
    head_sha: str,
    inheritance_repair_counter: dict[str, int],
) -> bool:
    """True iff an autonomous repair was warranted but the per-head budget is
    already fully consumed — the caller must escalate rather than dispatch.

    ``_build_repair_mandate`` returns ``None`` for BOTH genuine exhaustion AND a
    configured budget of zero (``max_repair_attempts_per_head: 0`` =
    classify-but-never-dispatch), so the bounce path can't tell them apart from
    the ``None`` alone. This helper isolates *genuine* exhaustion: L3 enabled, at
    least one INHERITED failure, a budget of ``>= 1``, and that budget already
    consumed for this head. A zero budget returns ``False`` here (fall through to
    a plain bounce, never escalate), and L3-disabled returns ``False`` so the
    bounce path is byte-identical to the pre-spec-090 baseline (SC-006).
    """
    cfg = _get_inherited_repair_gate_config(state)
    if cfg is None or not getattr(cfg, "enabled", False):
        return False
    if not inherited:
        return False
    max_attempts = getattr(cfg, "max_repair_attempts_per_head", 1)
    if max_attempts < 1:
        return False
    attempts_so_far = inheritance_repair_counter.get(head_sha, 0)
    return attempts_so_far >= max_attempts


# 090-L3 (US3, T032): the dual test-integrity guard on the candidate repair diff.
# ---------------------------------------------------------------------------
# Comment template restating the no-auto-merge / stale-approval assumption
# (Decision 7, FR-021, SC-005). The coordinare NEVER auto-merges a repair; it
# lands the fix as a candidate and relies on GitHub-native "Dismiss stale
# approvals on new commits" so a prior human approval cannot carry over onto the
# repaired head. The lowercase substrings "approval" and "merge" are asserted by
# the contract tests — keep them present.
_REPAIR_CANDIDATE_COMMENT = (
    "🤖 **Baseline repair landed as a candidate.** The dual test-integrity guard "
    "(static analysis + an independent adversarial reviewer) cleared this fix for "
    "an inherited base-branch failure.\n\n"
    "This is **not** auto-merged. It awaits fresh human review and approval. Any "
    "prior approval on an earlier head is dismissed by GitHub's "
    "\"Dismiss stale approvals on new commits\" setting, so re-approval on this "
    "head is required before merge."
)


def _pending_repair_dispatch(state: CoordinareState) -> dict[str, Any] | None:
    """Return the most recent ``repair_audit`` record iff it is a pending
    ``dispatch`` awaiting guard adjudication, else ``None``.

    Gating the guard purely on the presence of a trailing ``dispatch`` record
    keeps the whole L3 guard a no-op at all flag defaults: dispatch records are
    only ever appended when the inherited-repair gate is enabled, so behavior is
    byte-identical to pre-spec-090 when L3 is off (SC-006). Reading the last
    record (not a scan) also makes the guard fail-safe: a flag toggled off
    mid-flight still adjudicates the one pending repair before anything lands.
    """
    audit = state.get("repair_audit") or []
    if not audit:
        return None
    last = audit[-1]
    if isinstance(last, dict) and last.get("kind") == "dispatch":
        return last
    return None


def _repair_record(
    *,
    head_sha: str,
    attempt: int,
    kind: str,
    now_iso: str,
    is_safe: bool | None = None,
    flagged_patterns: list[str] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serialized ``RepairDecisionRecord`` for ``repair_audit``."""
    from coordinare.state_store import RepairDecisionRecord

    return RepairDecisionRecord(
        head_sha=head_sha,
        attempt=attempt,
        kind=kind,  # type: ignore[arg-type]
        is_safe=is_safe,
        flagged_patterns=flagged_patterns or [],
        detail=detail,
        decided_at=now_iso,
    ).model_dump(mode="json")


async def _dispatch_repair_reviewer(
    *,
    state: CoordinareState,
    card_id: str,
    diff: str,
    pending: dict[str, Any],
) -> tuple[bool, str]:
    """Dispatch the independent ``diagnostic``-role adversarial reviewer with
    fresh context to adjudicate the candidate repair diff (FR-019).

    Returns ``(is_safe, detail)``. **Fails SAFE**: if the diagnostic performer is
    not wired, the reviewer is treated as a veto (``is_safe=False``) so an
    un-reviewed repair is never allowed to land. The real adversarial probe is
    not yet wired end-to-end; until then the guard refuses to land any candidate
    that reaches this half, which is the conservative default the contract
    requires (the unit suite monkeypatches this function to exercise the
    clear/veto/uncertainty branches).
    """
    services = state.get("performer_services") or {}
    reviewer = services.get("diagnostic") if isinstance(services, dict) else None
    if reviewer is None:
        return (
            False,
            "No diagnostic-role reviewer is configured, so the candidate repair "
            "could not be independently adversarially reviewed; refusing to land "
            "it (guard fails safe).",
        )
    # A wired diagnostic reviewer would be dispatched here with fresh context and
    # the do-not-weaken mandate. Until that transport exists, treat a configured
    # but unexercised reviewer the same way the unit suite does via monkeypatch.
    return (
        False,
        "Adversarial repair review is not yet implemented; refusing to land the "
        "candidate (guard fails safe).",
    )


async def _post_repair_comment(state: CoordinareState, body: str) -> None:
    """Best-effort: post a guard escalation/acceptance comment on the PR.

    Reads the PR node id from ``current_card``; no-ops silently if the GitHub
    service, the ``add_comment`` capability, or the subject id is missing.
    Prefixed with the coordinare attribution header. Never raises — a comment
    failure must not change the guard verdict.
    """
    github = state.get("github_service")
    if github is None or not hasattr(github, "add_comment"):
        return
    card = state.get("current_card")
    subject_id = card.get("pr_node_id") if isinstance(card, dict) else None
    if not subject_id:
        return
    header = coordinare_attribution(state.get("config"), "implementing")
    try:
        await github.add_comment(str(subject_id), f"{header}\n\n{body}")
    except Exception as exc:
        logger.warning(
            "repair_guard.comment_failed",
            error=str(exc),
            exc_type=type(exc).__name__,
        )


async def _evaluate_repair_guard(
    state: CoordinareState,
    card_id: str,
    pr_url: str | None,
) -> tuple[dict[str, Any], bool]:
    """Adjudicate the landed candidate repair diff via the dual guard (T032).

    Runs ONLY when a pending ``dispatch`` record is present. Returns a
    ``(state_updates, stop)`` tuple. ``stop=True`` means REJECT — the candidate
    is blocked, escalated, and never advanced (no push). ``stop=False`` means
    both halves cleared — the candidate lands and the caller advances normally.

    The guard **fails SAFE**: any uncertainty (diff cannot be fetched, reviewer
    errors) rejects, unlike the fail-open L1/L2/CI gates. Adjudication order:
    (1) static ``analyze_diff`` hot path, then (2) the independent adversarial
    reviewer — consulted ONLY when the static half clears. Either veto rejects.
    Each step appends a ``RepairDecisionRecord`` to ``repair_audit`` (FR-023).
    """
    pending = _pending_repair_dispatch(state)
    if pending is None:
        return {}, False

    head_sha = str(pending.get("head_sha", ""))
    attempt = int(pending.get("attempt", 0))
    now_iso = datetime.now(UTC).isoformat()
    audit: list[dict[str, Any]] = list(state.get("repair_audit") or [])

    async def _reject(detail: str, *, kind_seq: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        records = list(kind_seq)
        records.append(
            _repair_record(
                head_sha=head_sha,
                attempt=attempt,
                kind="rejection",
                now_iso=now_iso,
                detail=detail,
            )
        )
        open_qs = list(state.get("open_questions") or [])
        open_qs.append(
            f"Autonomous baseline repair (attempt {attempt}) on head {head_sha[:12]} "
            f"was rejected by the test-integrity guard and NOT landed: {detail} "
            "A human must review and resolve the inherited base-branch failure."
        )
        await _post_repair_comment(
            state,
            "🚫 **Baseline repair rejected by the test-integrity guard — not "
            f"landed.** {detail}\n\nThe coordinare never weakens tests and never "
            "auto-merges; this requires human review.",
        )
        return (
            {
                "phase": "blocked",
                "agent_dispatch": {},
                "agent_dispatch_at": None,
                "repair_audit": audit + records,
                "open_questions": open_qs,
            },
            True,
        )

    # (1) Fetch the candidate diff. A failure here is uncertainty → fail safe,
    # and we reject BEFORE recording a static_guard record (analyze_diff never
    # ran).
    github = state.get("github_service")
    if github is None or not hasattr(github, "get_pr_diff") or not pr_url:
        return await _reject(
            "Could not fetch the candidate repair diff to adjudicate it.",
            kind_seq=[],
        )
    try:
        diff, _changed = await github.get_pr_diff(pr_url)
    except Exception as exc:
        logger.warning(
            "repair_guard.diff_fetch_failed",
            card_id=card_id,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        return await _reject(
            "Could not fetch the candidate repair diff to adjudicate it.",
            kind_seq=[],
        )

    # (2) Static guard (hot path). FR-011: never log the raw diff.
    is_safe, flagged = analyze_diff(diff or "")
    static_record = _repair_record(
        head_sha=head_sha,
        attempt=attempt,
        kind="static_guard",
        now_iso=now_iso,
        is_safe=is_safe,
        flagged_patterns=flagged,
    )
    if not is_safe:
        return await _reject(
            "The static test-integrity check flagged the diff as weakening tests "
            f"({', '.join(flagged)}).",
            kind_seq=[static_record],
        )

    # (3) Independent adversarial reviewer — only when the static half cleared.
    try:
        reviewer_safe, reviewer_detail = await _dispatch_repair_reviewer(
            state=state, card_id=card_id, diff=diff or "", pending=pending
        )
    except Exception as exc:
        logger.warning(
            "repair_guard.reviewer_failed",
            card_id=card_id,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        reviewer_safe, reviewer_detail = (
            False,
            "The adversarial reviewer could not reach a verdict "
            f"({type(exc).__name__}).",
        )
    reviewer_record = _repair_record(
        head_sha=head_sha,
        attempt=attempt,
        kind="reviewer",
        now_iso=now_iso,
        is_safe=reviewer_safe,
        detail=None if reviewer_safe else reviewer_detail,
    )
    if not reviewer_safe:
        return await _reject(
            reviewer_detail,
            kind_seq=[static_record, reviewer_record],
        )

    # Both halves cleared → land as a candidate (never auto-merged).
    acceptance_record = _repair_record(
        head_sha=head_sha,
        attempt=attempt,
        kind="acceptance",
        now_iso=now_iso,
        is_safe=True,
    )
    await _post_repair_comment(state, _REPAIR_CANDIDATE_COMMENT)
    logger.info(
        "repair_guard.candidate_accepted",
        card_id=card_id,
        attempt=attempt,
        head_sha=head_sha,
    )
    return {
        "repair_audit": [*audit, static_record, reviewer_record, acceptance_record]
    }, False


def _build_baseline_index(
    base_rollup: CheckRollup | None,
) -> dict[str, BaselineFailure] | None:
    """Index the merge-base baseline's *failing* checks by name (090-L2).

    Returns ``None`` when the base rollup is unavailable (unfetchable /
    indeterminate) so ``classify_failure_origin`` routes every HEAD failure to
    UNKNOWN rather than INHERITED (FR-012).  A fetched base with zero failures
    yields an **empty dict** — a distinct sentinel that lets same-name HEAD
    failures classify INTRODUCED.  The two must never be conflated.
    """
    if base_rollup is None:
        return None
    index: dict[str, BaselineFailure] = {}
    for entry in base_rollup.checks:
        if not _is_failure(entry):
            continue
        conclusion = entry.conclusion or "failure"
        signature, normalized = make_failure_signature(
            entry.name, conclusion, entry.title, entry.summary
        )
        index[entry.name] = BaselineFailure(
            name=entry.name,
            conclusion=conclusion,
            signature=signature,
            normalized_reason=normalized,
        )
    return index


def _plain(fc: FailedCheckWithSignature) -> FailedCheck:
    """Drop the signature fields — FLAKE/UNKNOWN lists carry plain failures."""
    return FailedCheck(
        name=fc.name,
        conclusion=fc.conclusion,
        html_url=fc.html_url,
        last_log_line=fc.last_log_line,
    )


async def _classify_head_failures(
    *,
    state: CoordinareState,
    card_id: str,
    svc: PrChecksService,
    rollup: CheckRollup,
    failed_names: list[str],
    failed_conclusion: str,
    url_by_name: dict[str, str],
) -> dict[str, Any]:
    """Observe-only 090-L2 classification of each failing HEAD check.

    Returns the four classification lists as ``CIGateDecision`` kwargs, or an
    empty dict when the gate is disabled / unconfigured.  The entire body is
    wrapped in its own ``try/except`` returning ``{}`` so a classification
    failure can NEVER reach ``_evaluate_ci_gate``'s outer fail-open ``except``
    (which would turn a BOUNCE into a PASS).  Classification is strictly
    additive — it never influences the verdict or routing (FR-013, FR-014,
    SC-006).
    """
    cfg = _get_baseline_classification_gate_config(state)
    l2_on = cfg is not None and getattr(cfg, "enabled", False)
    # 095: ENV_BLOCKED classification is its own gate, independent of L2. Run the
    # classifier when EITHER gate is on; ``env_patterns is None`` keeps Row 0 off
    # (byte-identical) when the env gate is disabled.
    env_cfg = _get_env_blocked_gate_config(state)
    env_on = env_cfg is not None and getattr(env_cfg, "enabled", False)
    if not (l2_on or env_on):
        return {}
    env_patterns = list(getattr(env_cfg, "patterns", []) or []) if env_on else None
    try:
        base_rollup = await svc.get_base_branch_check_rollup(rollup.base_ref or "main")
        if env_on and base_rollup is not None:
            base_rollup = await svc.enrich_failure_evidence(base_rollup)
        baseline_index = _build_baseline_index(base_rollup)
        if env_on and hasattr(svc, "refresh_peer_rollups"):
            await svc.refresh_peer_rollups(rollup.pr_number)
        structural_causes: dict[str, Any] = {}
        head_by_name = {c.name: c for c in rollup.checks}

        inherited: list[FailedCheckWithSignature] = []
        introduced: list[FailedCheckWithSignature] = []
        flake: list[FailedCheck] = []
        unknown: list[FailedCheck] = []
        env_blocked: list[FailedCheckWithSignature] = []

        for name in failed_names:
            entry = head_by_name.get(name)
            if entry is not None and entry.conclusion is not None:
                # Real conclusion/output: a transient conclusion classifies
                # FLAKE (FR-010), so we must NOT substitute the gate's
                # overridden failed_conclusion here.
                conclusion = entry.conclusion
                title = entry.title
                summary = entry.summary
            else:
                # pending_timeout / missing entry: no real output to read, so
                # synthesize the gate's failed_conclusion ("timed_out" for a
                # pending timeout → FLAKE; "failure" otherwise).
                conclusion = failed_conclusion
                title = None
                summary = None
            head_sig, head_reason = make_failure_signature(
                name, conclusion, title, summary
            )
            base_failure = baseline_index.get(name) if baseline_index else None
            fc = FailedCheckWithSignature(
                name=name,
                conclusion=conclusion,
                html_url=url_by_name.get(name) or None,
                head_signature=head_sig,
                baseline_signature=base_failure.signature if base_failure else None,
            )
            origin = classify_failure_origin(
                fc, head_reason, baseline_index, env_patterns=env_patterns
            )
            if env_on:
                from coordinare.services.env_signature import EnvCause

                if entry is not None and getattr(entry, "setup_failure", False):
                    structural_causes[name] = EnvCause("ci_setup_failure", "CI failed during runner/platform setup",
                                                       "Repair runner setup or platform credentials, then rerun the failed job")
                elif head_reason and hasattr(svc, "unrelated_failure_seen") and svc.unrelated_failure_seen(rollup, name, head_sig) is True:
                    structural_causes[name] = EnvCause("ci_shared_failure", "The same CI failure occurs on an unrelated head",
                                                       "Inspect the shared runner/service or base-branch failure, then rerun CI")
                if name in structural_causes:
                    origin = "env_blocked"
            if origin == "env_blocked":
                env_blocked.append(fc)
            elif l2_on:
                # Only collect the L2 lists when the L2 gate is on. With env-only
                # (l2_on=False), a non-env failure stays unclassified so the
                # decision is byte-identical to the pre-L2 baseline (SC-006).
                if origin == "inherited":
                    inherited.append(fc)
                elif origin == "introduced":
                    introduced.append(fc)
                elif origin == "flake":
                    flake.append(_plain(fc))
                else:
                    unknown.append(_plain(fc))

        logger.info(
            "ci_gate.classified",
            card_id=card_id,
            pr=rollup.pr_number,
            head=rollup.head_sha[:7],
            inherited=[c.name for c in inherited],
            introduced=[c.name for c in introduced],
            flake=[c.name for c in flake],
            unknown=[c.name for c in unknown],
            env_blocked=[c.name for c in env_blocked],
            base_fetched=baseline_index is not None,
        )
        # L2 lists only when L2 is on; env_blocked_checks only when env gate is on.
        result: dict[str, Any] = {}
        if l2_on:
            result["inherited_checks"] = inherited
            result["introduced_checks"] = introduced
            result["flake_checks"] = flake
            result["unknown_checks"] = unknown
        if env_on:
            result["env_blocked_checks"] = env_blocked
            result["env_causes"] = structural_causes
        return result
    except Exception as exc:
        logger.warning(
            "ci_gate.classification_failed", card_id=card_id, error=str(exc)
        )
        return {}


def _get_persona_check_map(state: CoordinareState) -> dict | None:
    """Pull the configured persona_check_map for the active symphony (075 US3).

    ``PersonaCheckMapConfig`` is a Pydantic ``RootModel`` wrapping
    ``dict[str, PersonaCheckMapPerDepth]``; flatten to the plain dict the
    resolver expects.
    """
    sym_name = state.get("current_symphony")
    sym_configs = state.get("symphony_configs") or {}
    sym_cfg = sym_configs.get(sym_name) if sym_name else None
    if sym_cfg is None:
        return None
    persona_scope_cfg = getattr(sym_cfg, "persona_scope", None)
    if persona_scope_cfg is None:
        return None
    cm = getattr(persona_scope_cfg, "persona_check_map", None)
    if cm is None:
        return None
    root = getattr(cm, "root", None)
    if not root:
        return None
    out: dict[str, dict[str, list[str]]] = {}
    for persona, per_depth in root.items():
        out[persona] = {
            # 077 FR-013: `any` is the depth-agnostic list the resolver uses
            # when no 074 scope/depth is present. Must be flattened through here
            # or the decoupled gate scoping silently no-ops (falls to layer 3).
            "any": list(getattr(per_depth, "any", []) or []),
            "skim": list(getattr(per_depth, "skim", []) or []),
            "normal": list(getattr(per_depth, "normal", []) or []),
            "full": list(getattr(per_depth, "full", []) or []),
        }
    return out


def _get_session_persona_scope(state: CoordinareState, card_id: str) -> dict | None:
    sessions = state.get("active_sessions") or {}
    sess = sessions.get(card_id)
    if not isinstance(sess, dict):
        return None
    scope = sess.get("persona_scope")
    return scope if isinstance(scope, dict) else None


# FR-011 rate-limited warning: keyed by error class so different failure modes
# don't suppress each other.  Mirrors `persona_classifier._LAST_WARN_AT`.
_CI_GATE_API_ERROR_LAST_WARN_AT: dict[str, float] = {}  # mutable; cleared by test hook
_CI_GATE_API_ERROR_COOLDOWN_SECONDS: float = 600.0  # constant


def _reset_ci_gate_api_error_cooldown() -> None:
    """Test hook: clear the api-error warning rate-limit window."""
    _CI_GATE_API_ERROR_LAST_WARN_AT.clear()


def _warn_ci_gate_api_error(
    *,
    pr: int | None,
    card_id: str,
    exc: BaseException,
) -> None:
    reason = type(exc).__name__
    now = time.monotonic()
    last = _CI_GATE_API_ERROR_LAST_WARN_AT.get(reason)
    if last is not None and (now - last) < _CI_GATE_API_ERROR_COOLDOWN_SECONDS:
        logger.debug(
            "ci_gate.api_error_warning_suppressed",
            card_id=card_id,
            pr=pr,
            reason=reason,
            cooldown_seconds=_CI_GATE_API_ERROR_COOLDOWN_SECONDS,
        )
        return
    _CI_GATE_API_ERROR_LAST_WARN_AT[reason] = now
    logger.warning(
        "ci_gate.api_error",
        card_id=card_id,
        pr=pr,
        reason=reason,
        error=str(exc),
        fail_open=True,
    )


def _implementer_session_gone(state: CoordinareState) -> bool:
    """077: True when the implementer's performer session can no longer be polled.

    Ephemeral performers tear down their one-shot container the moment a job
    reaches a terminal state, so ``has_live_session`` returns False and re-polling
    on the next cycle is impossible (it lookup-misses → false transport error).
    Persistent performers keep a shared endpoint, so ``has_live_session`` stays
    True and the existing re-poll-to-re-gate HOLD loop is preserved.

    Conservative: returns True only when the session is demonstrably gone (no
    session_id, or ``has_live_session`` explicitly False); on any ambiguity
    (missing service / accessor / error) it returns False so existing behaviour
    is unchanged.
    """
    session_id = (state.get("agent_dispatch") or {}).get("session_id")
    if not session_id:
        return True
    services = state.get("performer_services") or {}
    svc = services.get("implementing") if isinstance(services, dict) else None
    check = getattr(svc, "has_live_session", None) if svc is not None else None
    if check is None:
        return False
    try:
        return not bool(check(str(session_id)))
    except Exception:
        return False


async def _maybe_env_blocked_hold(
    *,
    state: CoordinareState,
    classification: dict[str, Any],
    rollup: Any,
    required_names: Any,
    failed_names: list[str],
    head_sha: str,
    pr_num: Any,
    resolved: dict[str, Any],
    now_iso: str,
    stash: Any,
    evidence_service: Any = None,
) -> tuple[dict[str, Any], bool] | None:
    """095 (US1/US2): HOLD the card when a required failure is an infra/environment
    block — no code change can fix it, so do NOT bounce or re-dispatch. Surface
    the cause + suggested action to the operator once per condition (deduped via
    the per-card ``env_blocked`` state), and let a later cycle auto-resume when
    the signature clears. Returns the ``(updates, stop)`` tuple, or ``None`` to
    fall through to the normal bounce path.
    """
    env_blocked = classification.get("env_blocked_checks") or []
    if not env_blocked:
        return None

    # Re-derive the operator-facing cause/action across ALL env-blocked checks.
    # A card can be blocked by more than one DISTINCT infra pattern at once (e.g.
    # an artifact-quota failure on one check and an offline-runner failure on
    # another). Aggregate every matched pattern so the notification names all of
    # them, and build the dedup signature from the full sorted set of pattern ids
    # — that way the operator is re-notified when the SET of infra causes changes
    # (one clears while another persists), not silently deduped on the first.
    head_by_name = {c.name: c for c in rollup.checks}
    env_cfg = _get_env_blocked_gate_config(state)
    patterns = list(getattr(env_cfg, "patterns", []) or []) if env_cfg else []
    matched: dict[str, tuple[str, str]] = {}  # pattern_id -> (cause, action), de-duped, insertion-ordered
    for fc in env_blocked:
        entry = head_by_name.get(fc.name)
        reason = normalize_reason(entry.title, entry.summary) if entry else ""
        ec = (classification.get("env_causes") or {}).get(fc.name) or match_env_signature(reason, patterns)
        if ec is not None and ec.pattern_id not in matched:
            matched[ec.pattern_id] = (ec.cause, ec.action)
    if matched:
        # Stable dedup key over the SET of distinct patterns.
        pattern_id = "+".join(sorted(matched))
        cause = "; ".join(c for c, _ in matched.values())
        action = "; ".join(a for _, a in matched.values())
    else:
        # Defensive: env_blocked is non-empty so a pattern matched at classification
        # time; if re-derivation can't reproduce the cause (e.g. reason source
        # drift), still surface an actionable generic message rather than None.
        pattern_id = "env_blocked"
        cause = "Infrastructure/environment CI failure"
        action = "Operator action required — inspect the failing required check"

    # Carry ONLY env_blocked_checks on the hold. A `hold` verdict forbids
    # failed_checks, and the validator requires every inherited/introduced/flake/
    # unknown name to appear in failed_checks — so spreading the full
    # classification onto a hold raises for a MIXED card (env_blocked + an
    # inherited/introduced check). The other classifications aren't lost: when the
    # infra block clears, the next evaluation re-classifies and surfaces them.
    decision_obj = CIGateDecision(
        verdict="hold",
        head_sha=head_sha,
        required_checks=sorted(required_names),
        failed_checks=[],
        resolver_source=resolved["source"],
        bounce_count_after=0,
        decided_at=now_iso,
        env_blocked_checks=env_blocked,
    )
    dump = decision_obj.model_dump(mode="json")
    stash(dump)

    # Dedup: announce once per (head, pattern). Persisted on the session's
    # env_blocked state so a re-eval of the same block does not re-notify.
    prior = state.get("env_blocked") or {}
    already_notified = (
        prior.get("head_sha") == head_sha and prior.get("pattern_id") == pattern_id
    )
    if evidence_service is not None and prior:
        try:
            retried = await evidence_service.retry_recovered_infrastructure(rollup, prior)
            if retried:
                logger.info("ci_gate.infrastructure_recovered_rechecking", pr=pr_num, jobs=retried)
        except Exception as exc:
            logger.warning("ci_gate.infrastructure_recovery_probe_failed", error_type=type(exc).__name__)
    signature = "|".join(sorted(c.head_signature for c in env_blocked))
    notification_service = state.get("notification_service")
    if evidence_service is not None and notification_service is not None:
        cooldown = getattr(notification_service, "card_blocked_reminder_cooldown_seconds", 3600)
        if not isinstance(cooldown, (int, float)):
            cooldown = 3600
        already_notified = not evidence_service.claim_env_notification(signature, cooldown)
    if not already_notified:
        # FR-009: the card is held on the infra block, but the operator signal
        # must still distinguish any OTHER failing checks so a code defect
        # alongside the infra block is not masked. Derive these from the actual
        # failing names minus the env-blocked ones — robust whether or not the L2
        # classifier is on (in env-only mode the L2 lists are empty).
        env_names = {c.name for c in env_blocked}
        other_failed = sorted(n for n in failed_names if n not in env_names)
        logger.warning(
            "ci_gate.env_blocked_hold",
            pr=pr_num,
            head=head_sha[:7],
            checks=[c.name for c in env_blocked],
            other_failed=other_failed,
            pattern_id=pattern_id,
            cause=cause,
            action=action,
        )
        # FR-003: surface the infra cause + action to the operator through the
        # existing Slack / GitHub-comment notification channel — a DISTINCT
        # env-block event, not a generic "tests failed". Deduped at the channel
        # too via dedup_key so a re-eval of the same block stays quiet. A notify
        # failure must never break the gate, so this is best-effort.
        notification_service = state.get("notification_service")
        if notification_service is not None:
            try:
                from coordinare.models.notification import (
                    EventType,
                    NotificationEvent,
                    NotificationSeverity,
                )
                card_id = state.get("active_card_id") or (
                    (state.get("current_card") or {}).get("id") or ""
                )
                await notification_service.dispatch(NotificationEvent(
                    event_type=EventType.env_blocked,
                    severity=NotificationSeverity.warning,
                    source="monitor_performer",
                    dedup_key=f"env_blocked:{signature}",
                    payload={
                        "event_type": EventType.env_blocked.value,
                        "severity": NotificationSeverity.warning.value,
                        "source": "monitor_performer",
                        "card_id": str(card_id),
                        "pr": str(pr_num) if pr_num is not None else "",
                        "head_sha": head_sha[:7],
                        "pattern_id": pattern_id,
                        "cause": cause or "",
                        "action": action or "",
                        "checks": ", ".join(c.name for c in env_blocked),
                        "other_failed": ", ".join(other_failed),
                    },
                ))
            except Exception:
                if evidence_service is not None:
                    evidence_service.release_env_notification(signature)
                logger.warning(
                    "ci_gate.env_blocked_notify_failed",
                    pr=pr_num,
                    head=head_sha[:7],
                    pattern_id=pattern_id,
                    exc_info=True,
                )
    # Hold via monitoring_performer (the gate-re-evaluation phase) so a later
    # cycle re-runs the CI gate and AUTO-RESUMES once the infra signature clears
    # (FR-008). Clear the dispatch reference UNCONDITIONALLY — for both ephemeral
    # AND persistent performers — because an env block must never re-poll or
    # re-dispatch a performer (no code change can fix it, FR-004); the card
    # re-gates instead of polling a stale/persistent session.
    updates: dict[str, Any] = {
        "phase": "monitoring_performer",
        "latest_ci_gate_decision": dump,
        "ci_gate_advisory_failures": [],
        "agent_dispatch": {},
        "agent_dispatch_at": None,
        "env_blocked": {
            "head_sha": head_sha,
            "pattern_id": pattern_id,
            "cause": cause,
            "action": action,
            "check_names": sorted(c.name for c in env_blocked),
            "retried_jobs": prior.get("retried_jobs", []) if prior.get("head_sha") == head_sha else [],
            "retried_checks": prior.get("retried_checks", []) if prior.get("head_sha") == head_sha else [],
            "blocked_at": (prior.get("blocked_at") or now_iso) if prior.get("head_sha") == head_sha else now_iso,
        },
    }
    return (updates, True)


def _retain_infrastructure_hold(state: CoordinareState, updates: dict[str, Any]) -> bool:
    """A pending rerun on the same head retains the outage's retry budget."""
    prior = state.get("env_blocked") or {}
    decision = updates.get("latest_ci_gate_decision") or {}
    return bool(prior.get("check_names") and decision.get("verdict") == "hold"
                and prior.get("head_sha") == decision.get("head_sha"))


async def _evaluate_ci_gate(
    state: CoordinareState,
    card_id: str,
    pr_url: str | None,
) -> tuple[dict[str, Any], bool]:
    """Run the implementer CI gate (spec 075) at implementer→reviewer boundary.

    Returns a (state_updates, stop) tuple.  ``stop=True`` means BOUNCE/HOLD/
    ESCALATE — caller should apply updates and skip ``_advance_stage``.
    ``stop=False`` means PASS (or gate disabled / fail-open) — caller advances
    normally.  Fails open on any exception per FR-011.
    """
    cfg = _get_ci_gate_config(state)
    if cfg is None or not getattr(cfg, "enabled", False):
        return {}, False

    # FR-013: no PR yet → nothing to gate on.
    if not pr_url:
        return {}, False

    parts = _pr_url_parts(pr_url)
    if parts is None:
        logger.warning("ci_gate.unparseable_pr_url", pr_url=pr_url)
        return {}, False
    owner, repo, pr_num = parts

    github = state.get("github_service")
    if github is None:
        return {}, False

    try:
        # Cache PrChecksService on the github service object so the
        # _bpr_forbidden flag survives between poll cycles.  Without caching a
        # fresh instance is built every call and the flag resets to False,
        # causing a repeated FORBIDDEN → fallback → WARNING on every poll for
        # tokens that lack admin:read on branchProtectionRules.
        _svc_cache: dict[tuple[str, str], PrChecksService] = getattr(
            github, "_pr_checks_service_cache", None
        ) or {}
        if not hasattr(github, "_pr_checks_service_cache"):
            github._pr_checks_service_cache = _svc_cache
        cache_key = (owner, repo)
        if cache_key not in _svc_cache:
            _svc_cache[cache_key] = PrChecksService(github, owner, repo)
        svc = _svc_cache[cache_key]
        rollup = await svc.get_pr_check_rollup(pr_num)
        env_cfg = _get_env_blocked_gate_config(state)
        if env_cfg is not None and getattr(env_cfg, "enabled", False):
            rollup = await svc.enrich_failure_evidence(rollup)

        # Warn once per HEAD when the GraphQL context cap is reached so
        # operators know the required-checks set may be incomplete (>100
        # contexts).  Dedup by tracking the last warned SHA on the session to
        # avoid log spam across repeated gate evaluations on the same HEAD.
        if rollup.at_context_cap:
            sessions_snap = state.get("active_sessions") or {}
            sess_snap = sessions_snap.get(card_id) if isinstance(sessions_snap, dict) else None
            cap_warned_sha = (sess_snap or {}).get("_ci_gate_cap_warned_sha")
            if cap_warned_sha != rollup.head_sha:
                logger.warning(
                    "ci_gate.context_cap_hit",
                    pr=pr_num,
                    head=rollup.head_sha[:7],
                    note="PR has >100 check contexts; required-checks list may be incomplete",
                )
                if isinstance(sess_snap, dict):
                    sess_snap["_ci_gate_cap_warned_sha"] = rollup.head_sha

        all_head = [c.name for c in rollup.checks]
        scope = _get_session_persona_scope(state, card_id)
        persona_check_map = _get_persona_check_map(state)
        branch_protection_set: set[str] | None = None
        get_bp = getattr(github, "get_required_status_checks", None)
        if callable(get_bp):
            try:
                # PR's base ref is the default branch we gate on.
                # rollup.base_ref is populated from baseRefName in the GraphQL
                # response; fall back to "main" only when the field is empty
                # (e.g. parse_rollup received a malformed/stub payload).
                base_ref = rollup.base_ref or "main"
                if not rollup.base_ref:
                    logger.warning(
                        "ci_gate.base_ref_unknown",
                        pr=pr_num,
                        fallback=base_ref,
                        note="branch_protection lookup may query wrong branch",
                    )
                bp = await get_bp(owner, repo, base_ref)
                if bp is not None:
                    branch_protection_set = set(bp)
            except Exception as bp_exc:
                logger.debug("ci_gate.branch_protection_lookup_failed", error=str(bp_exc))
        resolved = resolve(
            scope=scope,
            persona_check_map=persona_check_map,
            branch_protection_set=branch_protection_set,
            all_head_checks=all_head,
        )
        logger.debug(
            "ci_gate.resolver",
            card_id=card_id,
            pr=pr_num,
            source=resolved["source"],
            required_count=len(resolved["names"]),
        )
        required_names = set(resolved["names"])

        decision = decide(
            rollup,
            pending_timeout_seconds=getattr(cfg, "pending_timeout_seconds", 900),
            required_check_names=required_names,
        )

        head_sha = rollup.head_sha
        bounce_counter = dict(state.get("bounce_counter") or {})
        # 090-L3 (US3): per-head repair-dispatch budget. Copied (not mutated in
        # place) so a fail-open exit leaves the persisted counter untouched; the
        # mandate builder increments this copy at dispatch (see
        # _build_repair_mandate). Empty when L3 is disabled (SC-006).
        inheritance_repair_counter = dict(state.get("inheritance_repair_counter") or {})
        max_bounces = getattr(cfg, "max_bounces_per_head", 3)
        now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        # T058: stash the latest decision on the live session so notify.py
        # can render a deduped PR rollup comment on the next cycle.
        sessions_for_stash = state.get("active_sessions") or {}
        session_for_stash = (
            sessions_for_stash.get(card_id)
            if isinstance(sessions_for_stash, dict) else None
        )

        def _stash(decision_dump: dict[str, Any]) -> None:
            if isinstance(session_for_stash, dict):
                session_for_stash["latest_ci_gate_decision"] = decision_dump

        if decision.action == "FORWARD":
            decision_obj = CIGateDecision(
                verdict="pass",
                head_sha=head_sha,
                required_checks=sorted(required_names),
                failed_checks=[],
                resolver_source=resolved["source"],
                bounce_count_after=0,
                decided_at=now_iso,
            )
            # FR-014: surface non-required failures as advisory on PASS.
            advisory_failures = [
                {"name": c.name, "conclusion": c.conclusion or "failure"}
                for c in rollup.checks
                if c.conclusion == "failure" and c.name not in required_names
            ]
            logger.info(
                "ci_gate.decided",
                verdict="pass",
                pr=pr_num,
                head=head_sha[:7],
                resolver_source=resolved["source"],
                advisory_count=len(advisory_failures),
            )
            dump = decision_obj.model_dump(mode="json")
            _stash(dump)
            return (
                {
                    "latest_ci_gate_decision": dump,
                    "ci_gate_advisory_failures": advisory_failures,
                },
                False,
            )

        if decision.action == "HOLD":
            decision_obj = CIGateDecision(
                verdict="hold",
                head_sha=head_sha,
                required_checks=sorted(required_names),
                failed_checks=[],
                pending_checks=sorted(decision.pending),
                resolver_source=resolved["source"],
                bounce_count_after=0,
                decided_at=now_iso,
            )
            logger.info(
                "ci_gate.decided",
                verdict="hold",
                pr=pr_num,
                head=head_sha[:7],
                pending=decision.pending,
            )
            dump = decision_obj.model_dump(mode="json")
            _stash(dump)
            hold_updates: dict[str, Any] = {
                "phase": "monitoring_performer",
                "latest_ci_gate_decision": dump,
                # Clear any advisory failures from a prior PASS so stale
                # data is not misread by a future consumer.
                "ci_gate_advisory_failures": [],
            }
            # 077: an ephemeral implementer's one-shot container is already torn
            # down at this point (terminal success), so it cannot be re-polled
            # next cycle. Clear the stale session reference (mirrors BOUNCE/
            # ESCALATE) so (a) check_board._is_stale does not misclassify the
            # completed session as restart-orphaned and re-dispatch, and (b)
            # monitor_performer routes through the gate-only re-evaluation branch
            # instead of polling a dead container (which lookup-misses → false
            # transport-error block). Persistent performers keep agent_dispatch so
            # their existing re-poll-to-re-gate HOLD loop is preserved untouched.
            if _implementer_session_gone(state):
                hold_updates["agent_dispatch"] = {}
                hold_updates["agent_dispatch_at"] = None
            return (hold_updates, True)

        # 095: classify (incl. ENV_BLOCKED) BEFORE counting a bounce, so an
        # infrastructure HOLD never consumes the card's bounce budget.
        url_by_name = {c.name: (c.details_url or "") for c in rollup.checks}
        if decision.reason == "pending_timeout":
            # Pending checks exceeded the timeout — represent them as failed
            # entries with conclusion="timed_out" so the bounce decision satisfies
            # the contract's non-empty failed_checks invariant.
            failed_names = sorted(decision.pending)
            failed_conclusion = "timed_out"
        else:
            failed_names = sorted(decision.failed)
            failed_conclusion = "failure"
        failed_objs = [
            FailedCheck(
                name=name,
                conclusion=failed_conclusion,
                html_url=url_by_name.get(name) or None,
            )
            for name in failed_names
        ]

        # 090-L2 (US2): observe-only failure-origin classification. Computed once
        # (the base rollup is fetched at most once) and spread onto the BOUNCE
        # and ESCALATE decisions only — never PASS/HOLD, whose failed_checks is
        # empty. Returns {} (no lists) when the classification gate is disabled,
        # keeping the decision byte-identical to the pre-spec-090 baseline
        # (SC-006). It is wrapped in its own try/except so it can never trip the
        # outer fail-open path.
        classification = await _classify_head_failures(
            state=state,
            card_id=card_id,
            svc=svc,
            rollup=rollup,
            failed_names=failed_names,
            failed_conclusion=failed_conclusion,
            url_by_name=url_by_name,
        )

        # 095: an infrastructure/environment block HOLDs (no bounce, no
        # re-dispatch) and surfaces to the operator — returned before the bounce
        # counter is touched.
        env_hold = await _maybe_env_blocked_hold(
            state=state,
            classification=classification,
            rollup=rollup,
            required_names=required_names,
            failed_names=failed_names,
            head_sha=head_sha,
            pr_num=pr_num,
            resolved=resolved,
            now_iso=now_iso,
            stash=_stash,
            evidence_service=svc,
        )
        if env_hold is not None:
            return env_hold

        classification.pop("env_causes", None)

        # BOUNCE — count this attempt and decide bounce vs escalate.
        bounce_counter[head_sha] = bounce_counter.get(head_sha, 0) + 1
        count = bounce_counter[head_sha]

        METRICS.bounces_total.labels(
            symphony=state.get("symphony_name", "__default__"),
            role=state.get("performer_stage", "unknown"),
        ).inc()

        if count >= max_bounces:
            decision_obj = CIGateDecision(
                verdict="escalate",
                head_sha=head_sha,
                required_checks=sorted(required_names),
                failed_checks=failed_objs,
                resolver_source=resolved["source"],
                bounce_count_after=count,
                max_bounces_per_head=max_bounces,
                decided_at=now_iso,
                **classification,
            )
            logger.warning(
                "ci_gate.decided",
                verdict="escalate",
                pr=pr_num,
                head=head_sha[:7],
                failed=decision.failed,
                bounce_count=count,
            )
            dump = decision_obj.model_dump(mode="json")
            _stash(dump)
            return (
                {
                    "phase": "blocked",
                    # Reset dispatch so resume-from-blocked doesn't inherit a
                    # stale performer session reference (mirrors BOUNCE path).
                    "agent_dispatch": {},
                    "agent_dispatch_at": None,
                    "bounce_counter": bounce_counter,
                    # Round-trip the (unchanged) repair budget alongside the CI
                    # bounce counter so a restart from blocked sees the same L3
                    # budget it had pre-escalation.
                    "inheritance_repair_counter": inheritance_repair_counter,
                    "latest_ci_gate_decision": dump,
                    "ci_gate_advisory_failures": [],
                },
                True,
            )

        # 090-L3 (US3): genuine repair-budget exhaustion (distinct from the CI
        # bounce-budget exhaustion above) — at least one INHERITED failure is
        # still red but the per-head autonomous-repair budget is fully consumed.
        # Escalate for human repair (phase=blocked + open_questions + a PR
        # comment + an ``escalation`` audit record) instead of looping a fresh
        # autonomous attempt (FR-022, FR-024, SC-007). A zero budget
        # (max_repair_attempts_per_head: 0) and L3-disabled both fall through to a
        # plain bounce, keeping SC-006 byte-identical.
        inherited_for_l3 = classification.get("inherited_checks") or []
        repair_audit: list[dict[str, Any]] = list(state.get("repair_audit") or [])
        if _l3_budget_exhausted(
            state=state,
            inherited=inherited_for_l3,
            head_sha=head_sha,
            inheritance_repair_counter=inheritance_repair_counter,
        ):
            attempts_used = inheritance_repair_counter.get(head_sha, 0)
            reason = (
                f"Autonomous baseline-repair budget exhausted for head "
                f"{head_sha[:7]}: {attempts_used} attempt(s) used and "
                f"{len(inherited_for_l3)} inherited base-branch failure(s) remain "
                f"red. Escalating for human repair instead of dispatching another "
                f"autonomous attempt."
            )
            decision_obj = CIGateDecision(
                verdict="escalate",
                head_sha=head_sha,
                required_checks=sorted(required_names),
                failed_checks=failed_objs,
                resolver_source=resolved["source"],
                bounce_count_after=count,
                max_bounces_per_head=max_bounces,
                decided_at=now_iso,
                **classification,
            )
            dump = decision_obj.model_dump(mode="json")
            _stash(dump)
            repair_audit.append(
                _repair_record(
                    head_sha=head_sha,
                    attempt=attempts_used,
                    kind="escalation",
                    now_iso=now_iso,
                    detail=reason,
                )
            )
            open_questions = list(state.get("open_questions") or [])
            open_questions.append(reason)
            await _post_repair_comment(
                state, f"🤖 **Baseline repair budget exhausted.** {reason}"
            )
            logger.warning(
                "ci_gate.repair_budget_exhausted",
                pr=pr_num,
                head=head_sha[:7],
                attempts=attempts_used,
            )
            return (
                {
                    "phase": "blocked",
                    "agent_dispatch": {},
                    "agent_dispatch_at": None,
                    "bounce_counter": bounce_counter,
                    "inheritance_repair_counter": inheritance_repair_counter,
                    "repair_audit": repair_audit,
                    "open_questions": open_questions,
                    "latest_ci_gate_decision": dump,
                    "ci_gate_advisory_failures": [],
                },
                True,
            )

        decision_obj = CIGateDecision(
            verdict="bounce",
            head_sha=head_sha,
            required_checks=sorted(required_names),
            failed_checks=failed_objs,
            resolver_source=resolved["source"],
            bounce_count_after=count,
            max_bounces_per_head=max_bounces,
            decided_at=now_iso,
            **classification,
        )
        if decision.reason == "pending_timeout":
            body = (
                f"CI gate: {len(failed_names)} required check(s) still pending "
                f"past timeout on this HEAD ({', '.join(failed_names)}). "
                f"Re-run or fix before re-handing off to reviewer."
            )
        else:
            body = (
                f"CI gate: {len(failed_names)} required check(s) failing on this HEAD "
                f"({', '.join(failed_names)}). Fix and push before re-handing off "
                f"to reviewer."
            )
        logger.warning(
            "ci_gate.decided",
            verdict="bounce",
            pr=pr_num,
            head=head_sha[:7],
            failed=decision.failed,
            bounce_count=count,
        )
        dump = decision_obj.model_dump(mode="json")
        _stash(dump)
        existing_rf = list(state.get("relay_feedback") or [])
        # 126 (L1): CI-gate items are raiser="ci" — disputes of a red check
        # route to the operator hold, never to stage adjudication (D5).
        ci_items = _stamp_feedback_bounce(
            state,
            [{"body": body, "author_login": "coordinare"}],
            raiser="ci",
            origin_sha=head_sha,
        )
        existing_rf.extend(ci_items)
        # 090-L3 (US3): when L3 is enabled and at least one INHERITED failure
        # remains within the per-head repair budget, build the repair mandate to
        # thread into the re-dispatched implementer's JobInitPayload.metadata
        # (the dispatch node reads result["repair_mandate"]). The builder mutates
        # inheritance_repair_counter at dispatch and returns None when L3 is off,
        # nothing is INHERITED, or the budget is exhausted — so the mandate key is
        # absent and the counter is byte-identical to baseline when L3 is off
        # (SC-006).
        repair_mandate = _build_repair_mandate(
            state=state,
            rollup=rollup,
            inherited=classification.get("inherited_checks") or [],
            head_sha=head_sha,
            inheritance_repair_counter=inheritance_repair_counter,
        )
        bounce_updates: dict[str, Any] = {
            "performer_stage": "implementing",
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
            "bounce_counter": bounce_counter,
            "inheritance_repair_counter": inheritance_repair_counter,
            "latest_ci_gate_decision": dump,
            "relay_feedback": existing_rf,
            "ci_gate_advisory_failures": [],
        }
        if repair_mandate is not None:
            bounce_updates["repair_mandate"] = repair_mandate
            # FR-023: record the dispatch decision in the append-only audit trail
            # (the guard appends static_guard/reviewer/outcome on the next cycle).
            repair_audit.append(
                _repair_record(
                    head_sha=head_sha,
                    attempt=repair_mandate["attempt"],
                    kind="dispatch",
                    now_iso=now_iso,
                    detail=(
                        f"Dispatched autonomous baseline-repair attempt "
                        f"{repair_mandate['attempt']}/{repair_mandate['max_attempts']} "
                        f"for {len(repair_mandate['inherited_checks'])} inherited "
                        f"failure(s)."
                    ),
                )
            )
            bounce_updates["repair_audit"] = repair_audit
        return (bounce_updates, True)
    except Exception as exc:
        # FR-011: fail-open on any error so a broken gate never blocks flow.
        _warn_ci_gate_api_error(pr=pr_num, card_id=card_id, exc=exc)
        return {}, False


async def _evaluate_baseline_prevention_gate(
    state: CoordinareState,
    card_id: str,
    pr_url: str | None,
) -> tuple[dict[str, Any], bool]:
    """L1 base-precondition gate (spec 090, US1) at the merge transition.

    Refuses to advance an approved, head-green PR to ``merging`` while a
    *required* check on its **base** branch is red (FR-001/FR-002).  Returns a
    (state_updates, stop) tuple:

    * ``stop=True`` — the base is RED: caller holds in ``monitoring_pr`` and
      re-evaluates next cycle.  The gate is pure and holds no state, so a base
      that turns green proceeds on the very next cycle (no latch, FR-006).
    * ``stop=False`` — PROCEED: base all-green, only *non-required* base
      failures (FR-004), a still-*pending* required base check, an
      INDETERMINATE (unfetchable) base (FR-005), or the gate disabled.

    The disabled / no-PR / unparseable-URL / no-github short-circuits and the
    fail-open ``except`` keep merge decisions byte-identical to the pre-feature
    baseline whenever L1 is off or its I/O breaks (SC-006).  The offending
    *base* check(s) are named in the ``monitor_pr.base_not_green_hold`` record
    distinctly from any head failure (FR-003).
    """
    cfg = _get_baseline_prevention_gate_config(state)
    if cfg is None or not getattr(cfg, "enabled", False):
        return {}, False

    # No PR yet → nothing to gate on.
    if not pr_url:
        return {}, False

    parts = _pr_url_parts(pr_url)
    if parts is None:
        logger.warning("monitor_pr.base_gate_unparseable_pr_url", pr_url=pr_url)
        return {}, False
    owner, repo, pr_num = parts

    github = state.get("github_service")
    if github is None:
        return {}, False

    try:
        # Reuse the PrChecksService cached on the github service object (see
        # _evaluate_ci_gate) so the branch-protection FORBIDDEN flag survives
        # between poll cycles instead of resetting on every evaluation.
        _svc_cache: dict[tuple[str, str], PrChecksService] = getattr(
            github, "_pr_checks_service_cache", None
        ) or {}
        if not hasattr(github, "_pr_checks_service_cache"):
            github._pr_checks_service_cache = _svc_cache
        cache_key = (owner, repo)
        if cache_key not in _svc_cache:
            _svc_cache[cache_key] = PrChecksService(github, owner, repo)
        svc = _svc_cache[cache_key]

        # The PR's base ref is the branch we gate on; fall back to "main" only
        # when baseRefName came back empty (malformed/stub payload).
        rollup = await svc.get_pr_check_rollup(pr_num)
        base_ref = rollup.base_ref or "main"
        base_rollup = await svc.get_base_branch_check_rollup(base_ref)

        scope = _get_session_persona_scope(state, card_id)
        persona_check_map = _get_persona_check_map(state)
        decision = evaluate_base_gate(
            base_rollup, scope, persona_check_map=persona_check_map
        )

        if decision.decision == "BLOCK":
            base_failing = [
                {"name": c.name, "url": c.html_url, "conclusion": c.conclusion}
                for c in decision.failing_checks
            ]
            logger.warning(
                "monitor_pr.base_not_green_hold",
                card_id=card_id,
                pr=pr_num,
                base_ref=base_ref,
                base_failing_checks=base_failing,
            )
            return {"phase": "monitoring_pr"}, True

        # PROCEED / INDETERMINATE → fall through to head-only behavior.
        return {}, False
    except Exception as exc:
        # Fail-open: a broken L1 gate must never hard-block the merge (SC-006).
        logger.warning(
            "monitor_pr.base_gate_error",
            card_id=card_id,
            pr=pr_num,
            reason=type(exc).__name__,
            error=str(exc),
            fail_open=True,
        )
        return {}, False


async def _refresh_backend_ui(
    state: dict[str, Any],
    service: Any,
    card_id: str,
) -> None:
    """Discover the backend UI URL from stderr logs and optionally poll session stats.

    Best-effort: any error is swallowed and logged at DEBUG so the performer
    session is never interrupted.  Stats polling is throttled to at most once
    per _STATS_POLL_INTERVAL_SECONDS to avoid hammering the local HTTP server.
    """
    from coordinare.transport.subprocess_transport import (
        _discover_backend_ui_url,
        _fetch_session_stats,
    )

    try:
        getter = getattr(service, "get_agent_logs", None)
        if not callable(getter):
            return
        logs = getter()
        if not isinstance(logs, list):
            return

        # Prefer fresh discovery; fall back to stored URL so stats keep updating
        # even after the opencode server line scrolls out of the log buffer.
        url = _discover_backend_ui_url(logs) or state.get("backend_ui_url")
        if not url:
            return

        state["backend_ui_url"] = url

        # Throttle stats polling to at most once per 30 seconds.
        last_fetched = state.get("_backend_stats_fetched_at")
        now = datetime.now(UTC)
        if last_fetched is not None:
            elapsed = (now - last_fetched).total_seconds()
            if elapsed < _STATS_POLL_INTERVAL_SECONDS:
                return

        # Record the attempt time before fetching so failed polls are also throttled.
        state["_backend_stats_fetched_at"] = now  # type: ignore[typeddict-unknown-key]
        stats = await _fetch_session_stats(url)
        if stats is not None:
            state["session_stats"] = stats
    except Exception as exc:
        logger.debug("monitor_performer.backend_ui_refresh_failed", card_id=card_id, error=str(exc))


async def monitor_performer(state: CoordinareState) -> CoordinareState:
    """Poll the active performer, then surface any terminal outcome in the feed.

    138 T041: the body has ~15 separate terminal-error / blocked returns. One
    wrapper covers every one of them, where patching individual sites would
    leave the siblings silent.
    """
    result = await _monitor_performer_body(state)
    if isinstance(result, dict) and result.get("phase") in {"blocked", "system_error"}:
        reason = result.get("system_error_reason") or "no reason reported"
        _record_activity(
            result,
            "error",
            f"{result['phase']}: {reason}",
            card_id=str((result.get("current_card") or {}).get("id", "")),
            stage=str(result.get("performer_stage") or ""),
        )
    return result


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

    stage: str = state.get("performer_stage", "implementing")
    performer_services: dict[str, Any] = state.get("performer_services") or {}

    # card_id must be extracted before SlotManager lookup.
    # Also guards against missing/invalid current_card — if card is not a
    # dict we can't acquire a meaningful slot and should bail early.
    if not isinstance(card, dict):
        state["phase"] = "idle"
        return state
    card_id = str(card.get("id", ""))

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
                state["env_blocked"] = None  # type: ignore[typeddict-unknown-key]
            if ci_stop:
                return state
            advance_updates = _advance_stage(state, None)
            for _k, _v in advance_updates.items():
                state[_k] = _v  # type: ignore[literal-required]
            return state
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

    # 048: Resolve the card's specific service instance via SlotManager so
    # status polls go to the correct transport (not just the primary).
    slot_manager = state.get("slot_manager")
    service = None
    if slot_manager is not None and hasattr(slot_manager, "acquire") and card_id:
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
    _teardown_on_exit = True
    try:
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
        # 027: Check session timeout before polling status
        dispatch_at = state.get("agent_dispatch_at")
        if timeout_secs > 0 and dispatch_at is not None:
            elapsed = (datetime.now(UTC) - dispatch_at).total_seconds()
            if elapsed > timeout_secs:
                logger.warning(
                    "monitor_performer.session_timeout",
                    performer_stage=stage,
                    card_id=card_id,
                    elapsed_seconds=round(elapsed),
                    timeout_seconds=timeout_secs,
                )
                # 048: release slot on timeout
                _sm = state.get("slot_manager")
                if _sm is not None and hasattr(_sm, "release"):
                    _sm.release(stage, card_id)
                state["phase"] = "blocked"
                state["open_questions"] = [
                    f"Performer ({stage}) timed out after {round(elapsed)}s "
                    f"(limit: {timeout_secs}s)"
                ]
                return state

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
        try:
            status = await service.check_status(str(session_id), payload=status_payload)
        except (TransportError, ConnectionError, TimeoutError) as exc:
            # 088 US6 (FR-012): an auth-class failure on a session whose
            # secret refresh already degraded is attributable — the performer
            # is rejecting stale credentials, not flaking. Route to blocked
            # with the structured reason instead of burning the generic
            # system-error retry budget.
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
                                "move_card_to_blocked_failed", card_id=card_id
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
        except PermanentGitHubError as exc:
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

        # Accumulate backend events (capped at 100 entries).
        new_events = status.get("events")
        if isinstance(new_events, list) and new_events:
            existing = list(state.get("performer_events") or [])
            # 358: merge, don't concatenate — the reported list is itself
            # cumulative, so appending it whole stored the same events again
            # on every poll until the buffer held nothing else.
            state["performer_events"] = merge_performer_events(existing, new_events)
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
                                {str(f.get("severity")) for f in gating}
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
                if not isinstance(state.get("documenting_side"), dict) or (state["documenting_side"].get("status") != "running" and not state["documenting_side"].get("writer_active")):
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

        # 166: lift the assessor workflow's assessment into the session. It is
        # the structured product reading of the card: goal, expected behaviour,
        # out-of-scope items, questions, assumptions, criteria with their source,
        # and carried clarifications. A new assessment replaces the old one.
        # The prose assessor reports no assessment and leaves state untouched.
        if stage == "assessing" and marker == "assessment_complete":
            _assess_report = status.get("report") if isinstance(status.get("report"), dict) else {}
            _assess = _assess_report.get("assessment")
            if isinstance(_assess, dict) and _assess.get("goal"):
                state["assessment"] = dict(_assess)
                if "recorded_at" not in state["assessment"]:
                    state["assessment"]["recorded_at"] = datetime.now(UTC).isoformat()
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

        # 164: stash the QA repair brief for the next implementer dispatch,
        # mirroring how scanner_findings already travels. Reports what failed
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

        # 077 stall watchdog: a still-"working" turn that has made NO forward
        # progress (no new events this poll, no token growth) for
        # ``stall_timeout_seconds`` is wedged — e.g. a hung upstream model read
        # that the backend reports as "busy" (the 2-hour qwen/Ollama hang).
        # Neither role_timeouts (opt-in, blocks) nor the backend's own
        # idle_timeout fires for this case. Kill the wedged turn and route
        # through the idle-timeout retry budget (retry → re-dispatch; budget
        # exhausted → BLOCK for an operator). Disabled when stall_timeout_seconds
        # is 0 (default).
        if marker == "working":
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
                        f"upstream model. Operator intervention required."
                    ]
                    state["agent_dispatch"] = {}
                    state["agent_dispatch_at"] = None
                    return state

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
            state["env_health_hold_reason"] = f"qa_env_blocked: {_reason}"  # type: ignore[typeddict-unknown-key]
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

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

        # --- 089 (US2): implementer local-test gate env-blocked — role-agnostic ---
        # The local test run failed coinciding with a spec-088 env-cache signal,
        # so there is no code defect to fix. Per spec.md (US2, FR-005, SC-003) the
        # card bails immediately to the BLOCKED column carrying the env-cache
        # reason — it is not changes_requested and consumes zero self-fix attempts
        # (local_fix_counter is left untouched). We still invalidate the env cache
        # via mark_runtime_health_failed so it rebuilds once an operator unblocks.
        if marker == "env_blocked":
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
            if _env_cache_svc is not None and _sym_name:
                try:
                    _env_cache_svc.mark_runtime_health_failed(_sym_name, state)
                except Exception as _exc:
                    logger.warning(
                        "monitor_performer.env_blocked_mark_failed",
                        card_id=card_id,
                        symphony=_sym_name,
                        error=str(_exc),
                    )
            state["env_health_hold_reason"] = f"env_blocked: {_reason}"  # type: ignore[typeddict-unknown-key]
            state["phase"] = "blocked"
            # 123 FR-006: env_blocked is an infrastructure/transient failure — count
            # it toward the per-card transient budget (accumulates across the card's
            # lifetime; reset only on un-block) so it stays off the content budget
            # and the accounting is consistent with the system_error/unknown path.
            state["transient_error_cycles"] = int(  # type: ignore[typeddict-unknown-key]
                state.get("transient_error_cycles") or 0
            ) + 1
            if stage in ("reviewing", "closing_review"):
                # 169: the reviewer workflow holds when it could not post its one
                # review or could not read every changed file; neither is a test
                # environment problem and no code change fixes it.
                state["system_error_reason"] = f"the review could not complete (no code defect):\n{_reason}"
                state["open_questions"] = [
                    f"The reviewer hit an environment blocker ({_reason}). The card is parked in the "
                    "blocked column: check GitHub API access from the performer and the size of the "
                    "injected diff, then re-run the review stage."
                ]
            elif stage == "documenting":
                # 171: the documenter workflow holds when the tree was dirty at
                # start or the docs commit or push failed. No code change fixes it.
                state["system_error_reason"] = f"the documentation update could not complete (no code defect):\n{_reason}"
                state["open_questions"] = [
                    f"The documenter hit an environment blocker ({_reason}). The card is parked in the "
                    "blocked column: check the workspace for leftover changes and the push path for the "
                    "branch, then re-run the documenting stage."
                ]
            elif stage == "closing_review":
                # 172: the closer workflow holds when it could not read the review
                # threads, could not post its one review, or could not resolve a
                # thread it judged addressed. None of these is a code defect.
                state["system_error_reason"] = f"the closing review could not complete (no code defect):\n{_reason}"
                state["open_questions"] = [
                    f"The closer hit an environment blocker ({_reason}). The card is parked in the blocked "
                    "column: check GitHub API access from the performer, then re-run the closing review."
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
                    "the size of the injected diff, then re-run the security stage."
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
                    "performer environment / env cache before the stage can re-run."
                ]
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        from coordinare.services.documentation_findings import (
            collect as collect_documentation_findings,
        )
        collect_documentation_findings(state, stage, marker, status)

        # --- Terminal success states ---
        if marker in TERMINAL_SUCCESS_STATES:
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
                state["env_health_hold_reason"] = "terminal_success_env_health_failed"  # type: ignore[typeddict-unknown-key]
                state["phase"] = "dispatching"
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                return state
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
                    state, status if isinstance(status, dict) else {}
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
                        state, card_id, pr_url_for_gate
                    )
                    for key, value in guard_updates.items():
                        state[key] = value  # type: ignore[literal-required]
                    if guard_stop:
                        return state
                ci_updates, ci_stop = await _evaluate_ci_gate(
                    state, card_id, pr_url_for_gate
                )
                for key, value in ci_updates.items():
                    state[key] = value  # type: ignore[literal-required]
                # 095 (FR-008): clear stale env-hold dedup state on any non-env
                # verdict so a recurrence re-notifies (auto-resume / flapping).
                if "env_blocked" not in ci_updates and not _retain_infrastructure_hold(state, ci_updates):
                    state["env_blocked"] = None  # type: ignore[typeddict-unknown-key]
                if ci_stop:
                    return state

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
            if stage in VERDICT_STAGES and marker == EXPECTED_STAGE_MARKER.get(stage):
                _resolve_dispute_round(state, stage, passed=True)

            updates = _advance_stage(state, status)

            # When advancing to monitoring_pr (final role complete), move the
            # card on the GitHub board and validate required PR fields.
            if updates.get("phase") == "monitoring_pr":
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
                    state, card_id, pr_url
                )
                for key, value in gate_updates.items():
                    state[key] = value  # type: ignore[literal-required]
                if gate_stop:
                    # HOLD or BOUNCE — skip the move_card / reviewer / notification
                    # side-effects.
                    return state

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
                            )
                        )
                    except Exception as exc:
                        logger.warning("ready_for_review_notification_failed", error=str(exc))

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

        # --- Changes requested (021-reviewer-performer) ---
        # Non-terminal outcome: relay reviewer comments back to implementer.
        if marker == "changes_requested":
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
                state["local_fix_counter"] = _counter  # type: ignore[typeddict-unknown-key]
                if _count <= _max_attempts:
                    logger.info(
                        "monitor_performer.local_test_failed_redispatch",
                        performer_stage=stage,
                        card_id=card_id,
                        head=_head,
                        attempt=_count,
                        max_fix_attempts=_max_attempts,
                    )
                    state["relay_feedback"] = comments  # type: ignore[typeddict-unknown-key]
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
                    f"{_fail_blob}"
                ]
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                return state
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
                    state["review_empty_retry_count"] = _empty_retries + 1  # type: ignore[typeddict-unknown-key]
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
                state["review_empty_retry_count"] = 0  # type: ignore[typeddict-unknown-key]
                state["phase"] = "blocked"
                state["system_error_reason"] = (
                    f"performer reported changes_requested with no actionable "
                    f"feedback after {_empty_retries} re-review(s) (stage={stage})"
                )
                state["open_questions"] = [
                    f"The `{stage}` performer rejected this card "
                    f"(`status=changes_requested`) but returned no structured "
                    f"comments and no prose body, even after a re-review. "
                    f"Nothing to relay to the implementer. Operator triage required."
                ]
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                return state
            # Actionable feedback present — reset the empty-review retry counter.
            state["review_empty_retry_count"] = 0  # type: ignore[typeddict-unknown-key]
            # 065 Fix 4b: synthesise a comment from the prose body when the
            # performer rejected with explanation but no structured comments.
            if not comments and body:
                comments = [{"body": body, "author_login": "coordinare"}]
            exhausted = _feedback_cycle_exhausted(state, card_id, stage, "changes_requested", comments)
            if exhausted is not None:
                return exhausted
            # 126 (D3): a raiser bouncing again while its disputes were
            # pending rejects them — the new round below stamps re_raised.
            _resolve_dispute_round(state, stage, passed=False)
            # 126 (L1): stamp the round — ids/raiser/origin head — so the
            # implementer success floor and the disposition contract can hold
            # this completion to account.
            comments = _stamp_feedback_bounce(
                state, comments, raiser=stage, origin_sha=_settled_head(status)
            )
            state["relay_feedback"] = comments  # type: ignore[typeddict-unknown-key]
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
                        [{"body": body, "comments": comments}]
                    )
                    if len(set(categories)) >= 2:
                        next_stage = "assessing"
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

        # --- Security failed (022-security-performer) ---
        # Non-terminal: route findings to implementer or architect based on routing field.
        if marker == "security_failed":
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
                state["relay_feedback"] = findings  # type: ignore[typeddict-unknown-key]
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
            state["relay_feedback"] = relevant_findings  # type: ignore[typeddict-unknown-key]
            state["performer_stage"] = target_stage
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

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
                state, failures, raiser="qa", origin_sha=_settled_head(status)
            )
            state["relay_feedback"] = failures  # type: ignore[typeddict-unknown-key]
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
            if current_max is None:
                current_max = state.get("card_context", {}).get("max_tokens")
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
                "Operator intervention required."
            ]
            return state

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
                "role or splitting the card. Operator intervention required."
            ]
            return state

        # --- Error status (FR-006) ---
        if marker == "error":
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
                state["relay_feedback"] = []  # type: ignore[typeddict-unknown-key]
                state["phase"] = "system_error"
                return state
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
                    state["relay_feedback"] = feedback  # type: ignore[typeddict-unknown-key]
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
                else f"Performer ({stage}) encountered an error."
            ]
            return state

        # --- Session expired ---
        if marker == "session_expired":
            # Preserve any unanswered questions so the next performer receives them.
            open_qs = [str(q) for q in (state.get("open_questions") or [])]
            if open_qs:
                existing_clarifications = list(state.get("card_clarifications") or [])
                state["card_clarifications"] = [
                    *existing_clarifications,
                    {"questions": open_qs, "answer": ""},
                ]
            state["open_questions"] = []

            # If a PR was already opened in a prior cycle, resume monitoring it
            # rather than re-queuing the card to TODO (which would trigger a
            # duplicate dispatch and a GitHub 422 error).
            if card.get("pr_node_id"):
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
                            "infinite monitoring loop on stale board state."
                        ]
            else:
                # Transient failure with no open PR — auto-requeue to TODO.
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
            return state

        # --- Partial progress (070) ---
        # Non-terminal escape hatch for implementer turns that committed/pushed
        # work but did not finish. Relay the next_focus hint and re-dispatch
        # the implementing stage for another turn.
        if marker == "partial_progress" and stage in SENTINEL_STAGES:
            next_focus = status.get("next_focus")
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
            state["relay_feedback"] = [  # type: ignore[typeddict-unknown-key]
                {"body": relay_body, "author_login": "coordinare"}
            ]
            # 072: preserve the originating stage rather than coercing to
            # "implementing" — a reviewer that checkpointed should resume
            # as a reviewer, not be demoted into the implementer role.
            state["performer_stage"] = stage
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        # --- Blocked ---
        if marker == "blocked":
            # 070: guardrail. If an implementing-stage turn reports blocked
            # but produced zero new commits, the model punted with a status
            # report instead of doing the work. Route back to dispatching
            # with a stronger directive instead of honoring the verdict.
            head_before = status.get("head_before")
            head_after = status.get("head_after")
            if (
                stage == "implementing"
                and isinstance(head_before, str)
                and isinstance(head_after, str)
                and head_before
                and head_before == head_after
            ):
                logger.warning(
                    "monitor_performer.blocked_no_commits_retry",
                    performer_stage=stage,
                    card_id=card_id,
                    head=head_before,
                )
                state["relay_feedback"] = [  # type: ignore[typeddict-unknown-key]
                    {
                        "body": (
                            "Your previous turn ended without pushing any new "
                            "commits. Resume the work; do not stop until your "
                            "changes are committed and pushed, or emit the "
                            "`partial_progress` JSON sentinel if you need to "
                            "checkpoint mid-task."
                        ),
                        "author_login": "coordinare",
                    }
                ]
                state["performer_stage"] = "implementing"
                state["phase"] = "dispatching"
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                return state

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
                logger.warning(
                    "monitor_performer.blocked_zero_progress_retry",
                    performer_stage=stage,
                    card_id=card_id,
                    head=head_before,
                    bot_comment_delta=bot_comment_delta,
                    clarifications_delta=_clar_now - _clar_at_dispatch,
                )
                state["relay_feedback"] = [  # type: ignore[typeddict-unknown-key]
                    {"body": resume_directive, "author_login": "coordinare"}
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
            questions = status.get("questions")
            if isinstance(questions, list) and questions:
                state["open_questions"] = [str(item) for item in questions]
                # 123 US4 (FR-010): persist the assessor's open questions so a
                # later assessor re-dispatch carries them forward as
                # prior_clarifications (FR-011) and does not re-ask them.
                # Distinct from open_questions above (blocked-card diagnostic
                # surface): stored as {"question","answer"} carry-forward Q&A.
                if stage == "assessing":
                    state["assessor_open_questions"] = [  # type: ignore[typeddict-unknown-key]
                        {"question": str(item), "answer": ""} for item in questions
                    ]
            else:
                # Blocked with no questions — assessment backend will generate them.
                state["open_questions"] = []
            return state

        # --- In-progress (working) ---
        _teardown_on_exit = False  # still working — workspace stays active
        state["phase"] = "monitoring_performer"

        # 052: Backend transparency — discover UI URL and poll session stats.
        await _refresh_backend_ui(state, service, card_id)

        return state
    finally:
        if _teardown_on_exit:
            await _teardown_workspace(state)
