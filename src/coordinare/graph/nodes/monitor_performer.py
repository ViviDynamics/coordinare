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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.state import _retire_active_session, _set_current_card
from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
from coordinare.services.github import PermanentGitHubError
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

TERMINAL_SUCCESS_STATES: frozenset[str] = frozenset({
    "pr_opened",
    "plan_committed",
    "approved",
    "security_passed",
    "qa_passed",
    "docs_committed",
    "assessment_complete",
})
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
    "changes_requested", "security_failed", "qa_failed",
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
        _retire_active_session(state)
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
        _retire_active_session(state)
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
        _retire_active_session(state)
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
    max_cycles = _feedback_cycle_budget(state)
    if max_cycles <= 0:
        return None  # 0 disables the bound
    current = int(state.get("feedback_cycle_count") or 0) + 1
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


def _is_workflow_push_permission_error(reason: str) -> bool:
    """True when git push was rejected because workflow writes are disallowed."""
    lowered = reason.lower()
    return (
        "git push failed" in lowered
        and any(marker in lowered for marker in _WORKFLOW_PUSH_REJECTION_MARKERS)
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
        updates: dict[str, Any] = {
            "performer_stage": sequence[idx + 1],
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        }
        if status is not None:
            card = dict(state.get("current_card") or {})
            changed = False
            pr_url = status.get("pr_url")
            pr_node_id = status.get("pr_node_id")
            plan_path = status.get("plan_path")
            if pr_url:
                card["pr_url"] = pr_url
                changed = True
            if pr_node_id:
                card["pr_node_id"] = pr_node_id
                changed = True
            if plan_path:
                card["plan_path"] = plan_path
                changed = True
            if changed:
                updates["current_card"] = card
        return updates

    # All roles complete — transition to human review.
    card: dict[str, Any] = dict(state.get("current_card") or {})
    if status is not None:
        pr_url = status.get("pr_url")
        pr_node_id = status.get("pr_node_id")
        plan_path = status.get("plan_path")
        if pr_url:
            card["pr_url"] = pr_url
        if pr_node_id:
            card["pr_node_id"] = pr_node_id
        if plan_path:
            card["plan_path"] = plan_path

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
                    await github.move_card(card_id, "IN_REVIEW")
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
                    await github.move_card(card_id, "BLOCKED")
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
            state["performer_events"] = (existing + new_events)[-100:]

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
                    fresh = await github.get_issue_details(issue_id)
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

        # 048: Release the performer slot on ANY terminal marker (success,
        # changes_requested, failed, error, blocked, session_expired) so
        # the next queued card can use the freed slot.  Must happen before
        # any branching because non-success paths (changes_requested,
        # security_failed, qa_failed, error) return early.
        _terminal_markers = TERMINAL_SUCCESS_STATES | {
            "changes_requested", "security_failed", "qa_failed",
            "error", "blocked", "session_expired", "token_limit",
            "partial_progress",
        }
        if marker in _terminal_markers:
            _slot_mgr = state.get("slot_manager")
            if _slot_mgr is not None and hasattr(_slot_mgr, "release"):
                _slot_mgr.release(stage, card_id)

        # --- Terminal success states ---
        if marker in TERMINAL_SUCCESS_STATES:
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
                        await github.move_card(card_id, "IN_REVIEW")
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
            # 065 Fix 4c: safety net — if performer reported changes_requested
            # but supplied neither structured comments nor a prose body, the
            # implementer would have no information to act on. Block the card
            # for operator triage instead of looping.
            if not comments and not body:
                logger.warning(
                    "monitor_performer.changes_requested_no_actionable_feedback",
                    performer_stage=stage,
                    card_id=card_id,
                )
                state["phase"] = "blocked"
                state["system_error_reason"] = (
                    f"performer reported changes_requested with no actionable "
                    f"feedback (stage={stage})"
                )
                state["open_questions"] = [
                    f"The `{stage}` performer rejected this card "
                    f"(`status=changes_requested`) but returned no structured "
                    f"comments and no prose body. Nothing to relay to the "
                    f"implementer. Operator triage required."
                ]
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None
                return state
            # 065 Fix 4b: synthesise a comment from the prose body when the
            # performer rejected with explanation but no structured comments.
            if not comments and body:
                comments = [{"body": body, "author_login": "coordinare"}]
            exhausted = _feedback_cycle_exhausted(state, card_id, stage, "changes_requested", comments)
            if exhausted is not None:
                return exhausted
            state["relay_feedback"] = comments  # type: ignore[typeddict-unknown-key]
            state["performer_stage"] = "implementing"
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        # --- Security failed (022-security-performer) ---
        # Non-terminal: route findings to implementer or architect based on routing field.
        if marker == "security_failed":
            raw_findings = status.get("findings", [])
            findings = raw_findings if isinstance(raw_findings, list) else []
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
            state["relay_feedback"] = failures  # type: ignore[typeddict-unknown-key]
            state["performer_stage"] = "implementing"
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
            return state

        # --- Token-cap exhaustion (055) ---
        if marker == "token_limit":
            reason = str(status.get("reason", ""))
            current_max = state.get("card_context", {}).get("max_tokens")
            if current_max:
                advice = (
                    f"The {stage} performer hit its output token cap ({current_max:,} tokens). "
                    f"Raise `performers.{stage}.max_tokens` in your config, or set it to `0` "
                    f"(unlimited) to remove the cap."
                )
            else:
                advice = (
                    f"The {stage} performer hit the backend's output token cap. "
                    f"Set `performers.{stage}.max_tokens` to a higher value, or `0` for unlimited."
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
                    issue_number = card.get("issue_number") or state.get("issue_number")
                    if issue_number:
                        await github.post_comment(
                            int(issue_number),
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

        # --- Error status (FR-006) ---
        if marker == "error":
            reason = str(status.get("reason", ""))
            if reason.startswith(_FORMAT_ERROR_PREFIX):
                # Treat backend format-contract failures as retryable system
                # errors first; handle_system_error controls backoff + budget.
                if state.get("system_error_notified"):
                    state["system_error_count"] = 0
                    state["system_error_notified"] = False
                state["system_error_count"] = state.get("system_error_count", 0) + 1
                state["system_error_last_at"] = datetime.now(UTC)
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
                        await github.move_card(card_id, "IN_REVIEW")
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
                        await github.move_card(card_id, "TODO")
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

            state["phase"] = "blocked"
            questions = status.get("questions")
            if isinstance(questions, list) and questions:
                state["open_questions"] = [str(item) for item in questions]
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
