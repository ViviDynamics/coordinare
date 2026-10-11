"""Stage verdict recording, advancement, and feedback cycles (435)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

from coordinare.graph.nodes.monitor.artefacts import _record_pr_artefacts
from coordinare.graph.nodes.monitor.constants import (
    EXPECTED_STAGE_MARKER,
    VERDICT_STAGES,
)
from coordinare.services.observer import observer_enabled

logger = structlog.get_logger(__name__)


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

    state["feedback_ledger"] = rolled
    prior_origin = str(state.get("feedback_origin_sha") or "")
    state["feedback_origin_sha"] = origin_sha or None
    # Reset the no-op retry budget only when the origin head actually changed
    # (contract L2). A different raiser bouncing at the SAME head is not
    # progress — resetting would let a no-op completion escape the F5 hold by
    # riding a cross-raiser bounce. ``!=`` already covers a new/absent origin
    # (e.g. "abc" -> "" or "" -> "abc"); an unchanged empty origin ("" -> "")
    # leaves the (already-unarmed) counter untouched.
    if origin_sha != prior_origin:
        state["noop_success_retries"] = 0
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
    state["feedback_ledger"] = ledger
    return disputed


def _evaluate_success_floor(
    state: CoordinareState, status: dict[str, Any],
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
        state["feedback_origin_sha"] = None
        state["noop_success_retries"] = 0
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
                        f"operator adjudication required.",
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
    return _success_floor_noop_hold(state, card_id, origin)


def _success_floor_noop_hold(
    state: CoordinareState, card_id: str, origin: str,
) -> tuple[dict[str, Any], bool]:
    """F4/F5: no progress and nothing disputed — strengthen once, then hold."""
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
        state["noop_success_retries"] = retries + 1
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
                f"{item_lines}\nOperator triage required.",
            ],
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        },
        True,
    )


def _resolve_dispute_round(
    state: CoordinareState, raiser_stage: str, *, passed: bool,
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
        state["feedback_ledger"] = ledger
        logger.info(
            "monitor_performer.dispute_round_resolved",
            card_id=str((state.get("current_card") or {}).get("id", "")),
            raiser=raiser_stage,
            accepted=passed,
        )


def _record_stage_verdict(
    state: CoordinareState, marker: str, status: dict[str, Any],
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
    if stage not in VERDICT_STAGES or marker not in EXPECTED_STAGE_MARKER.get(stage, frozenset()):
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
    override was pending. Skip/veto commands are consumed immediately (FR-008).
    A valid restart retains an applied receipt until its target dispatch is
    accepted, so a deferred dispatch survives restart without replaying effects.
    """
    override = state.get("pending_override")
    if override is None:
        return None

    action = override.get("action")
    if action == "restart" and override.get("applied"):
        return None
    control_id = override.get("control_id")
    state["consumed_control_id"] = control_id if isinstance(control_id, str) else None
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
            state["lifecycle_continuation"] = []
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            # 125 (V1): an operator-requested restart must always dispatch —
            # this one-shot flag vetoes the verdict-cache skip for the target
            # stage and is consumed (cleared) by the cache check.
            state["override_forced_dispatch"] = target
            state["pending_override"] = {**override, "applied": True}
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
    state["content_feedback_cycles"] = current
    state["feedback_cycle_count"] = current
    # 065 US4 — monotonic lifetime counter; never resets on un-block.
    state["total_feedback_cycles"] = int(state.get("total_feedback_cycles") or 0) + 1
    if current <= max_cycles:
        return None
    # 065 US4 — exhaustion path: bump triage_blocks (monotonic).
    state["triage_blocks"] = int(state.get("triage_blocks") or 0) + 1
    logger.warning(
        "monitor_performer.feedback_cycle_exhausted",
        card_id=card_id,
        source_stage=source_stage,
        cycle_count=current,
        max_feedback_cycles=max_cycles,
        feedback_item_count=len(feedback_items),
    )

    message = _feedback_block_message(
        state, card_id, source_stage, reason_label, feedback_items, current, max_cycles,
    )

    state["phase"] = "blocked"
    state["open_questions"] = [message]
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None
    return state








def _feedback_block_message(
    state: CoordinareState,
    card_id: str,
    source_stage: str,
    reason_label: str,
    feedback_items: list[dict[str, Any]],
    current: int,
    max_cycles: int,
) -> str:
    """Build the Markdown triage message for an exhausted feedback cycle."""
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
        f"**Triage needed — feedback loop hit {max_cycles}-cycle limit.**",
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
    return "\n".join(lines)


def _ci_lint_gate(
    state: CoordinareState, card: dict[str, Any],
) -> dict[str, Any] | None:
    """043: CI lint gate — defence-in-depth before transitioning to
    monitoring_pr.

    Runs the detected lint command on the workspace if it's still available
    (performer may have already torn it down).  If lint fails, returns the
    re-dispatch updates routing back to the implementer; on pass or if the
    gate cannot run, returns None to proceed to monitoring_pr.  Note:
    _advance_stage is a sync function, so we use subprocess.run.
    """
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
    return None


def _carry_dispatched_feedback(state: CoordinareState, updates: dict[str, Any]) -> None:
    """Retarget unfinished requests with a gate bounce's new repair feedback."""
    batch = state.get("dispatched_feedback") or {}
    items: list[dict[str, Any]] = []
    for item in [*(batch.get("items") or []), *(state.get("relay_feedback") or []),
                 *(updates.get("relay_feedback") or [])]:
        if item not in items:
            items.append(item)
    updates["relay_feedback"] = items


def _advance_stage(
    state: CoordinareState, status: dict[str, Any] | None = None,
    *, acknowledge_final_feedback: bool = True,
) -> dict[str, Any]:
    """Compute the state update to advance the lifecycle to the next role.

    If more roles remain in ``lifecycle_sequence``, returns a dict that sets
    ``performer_stage`` to the next stage and resets dispatch state so the
    graph re-enters ``dispatching``.

    If no more roles remain, transitions to ``monitoring_pr`` and copies
    ``pr_url`` / ``pr_node_id`` from the terminal status into the card.
    """
    sequence: list[str] = list(
        state.get("lifecycle_continuation") or state.get("lifecycle_sequence") or ["implementing"],
    )
    current: str = state.get("performer_stage", "implementing")

    try:
        idx = sequence.index(current)
    except ValueError:
        # Unknown stage — treat as last so we fall through to monitoring_pr.
        idx = len(sequence)

    if idx + 1 < len(sequence):
        if status is not None:
            # An assessor detour must not acknowledge a reviewer correction
            # that is still awaiting implementation. Ordinary stage feedback
            # and batches owned by another stage retain their isolation.
            batch = state.get("dispatched_feedback") or {}
            if batch.get("stage") == current:
                queued = list(state.get("relay_feedback") or [])
                for item in batch.get("items") or []:
                    if (
                        item.get("raiser") == "reviewing"
                        and item.get("delivery_stage") == "implementing"
                        and "implementing" in sequence[idx + 1:]
                        and item not in queued
                    ):
                        queued.append(deepcopy(item))
                state["relay_feedback"] = queued
            state["dispatched_feedback"] = {}
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
            # 380: the production clock belongs to ONE performer run and must
            # die with it, exactly as agent_dispatch_at does. Left behind, the
            # next stage is measured from the previous stage's last command and
            # can be killed on its first poll. Worse, dispatch clears
            # performer_events, so the inherited fingerprint (say 9 commands)
            # sits above a freshly empty stream: the new stage would need ten
            # commands of its own before its clock could reset at all.
            "last_production_at": None,
            "last_production_fingerprint": None,
            "last_production_cursor": None,
            "convergence_reprieves": 0,
            # 425: the observer's repetition streak and verdict belong to ONE
            # performer run, exactly like the production clock above them.
            # Guarded so the default-off path stays byte-identical: an
            # ordinary stage advance must not write observer keys at all.
            **(
                {
                    "observer_repetition_count": 0,
                    "observer_verdict": None,
                }
                if observer_enabled(state)
                else {}
            ),
            # 426: a pending correction belongs to the role it was observed
            # for. The stage advancing here must not deliver it to the next
            # role unacted-on. NOT gated on observer_enabled: a config reload
            # can disable the observer after a correction was pended, and the
            # stale directive must still not reach the next role. Written
            # only when actually pending so the absent-key contract holds.
            **(
                {"observer_correction": None}
                if state.get("observer_correction") is not None
                else {}
            ),
        }
        artefact_updates = _record_pr_artefacts(state, status)
        if "current_card" in artefact_updates:
            updates["current_card"] = artefact_updates["current_card"]
        return updates

    # All roles complete — transition to human review.
    # 076 (T073): write through any new PR identifiers BEFORE
    # transitioning so monitoring_pr sees the correct head SHA.
    artefact_updates = _record_pr_artefacts(state, status)
    updated_card = artefact_updates.get("current_card")
    card: dict[str, Any] = (
        updated_card
        if isinstance(updated_card, dict)
        else dict(state.get("current_card") or {})
    )

    # 043: CI lint gate — route back to the implementer on lint failure,
    # else fall through to the monitoring_pr transition.
    lint_updates = _ci_lint_gate(state, card)
    if lint_updates is not None:
        # A final reviewer can bounce to implementation. Carry the original
        # request across that stage change until the replacement accepts it.
        _carry_dispatched_feedback(state, lint_updates)
        return lint_updates

    if status is not None and acknowledge_final_feedback:
        state["dispatched_feedback"] = {}
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
        "lifecycle_continuation": [],
    }


