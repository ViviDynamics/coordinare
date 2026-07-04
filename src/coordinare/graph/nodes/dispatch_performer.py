"""Generic, role-agnostic performer dispatch node (019-performer-lifecycle).

Replaces dispatch_card with a stage-driven dispatch that resolves the
performer service from ``performer_services[performer_stage]`` and injects
the matching persona instructions.  Contains ZERO role-specific logic
(FR-003) -- the stage-to-persona-role mapping is the only bridge between
the lifecycle pipeline and the persona system.
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.state import _set_current_card
from coordinare.services.env_cache import verify_env_cache_clean
from coordinare.services.github import PermanentGitHubError
from coordinare.services.persona_service import get_effective_instructions, load_personas_hot
from coordinare.services.security_scanner import ScannerError, scan_diff
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError
from coordinare.workspace import WorkspaceSetupError

if TYPE_CHECKING:
    from coordinare.graph.state import AgentServiceProtocol, CoordinareState

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Stage -> persona role mapping
# ---------------------------------------------------------------------------
# The performer pipeline uses stage names (verbs / gerunds) while the persona
# system uses role names (nouns).  This mapping is the *only* place where
# stage identifiers are coupled to persona roles.
_STAGE_TO_ROLE: dict[str, str] = {
    "implementing": "implementer",
    "reviewing": "reviewer",
    "security": "security",
    "qa": "qa",
    "documenting": "tech_writer",
    "architecting": "architect",
    "assessing": "assessor",
    "advocate": "advocate",
    # 042: Closing pass — same persona shape as reviewer but distinct stage
    # so _advance_stage doesn't loop back to the substantive reviewer.
    "closing_review": "closer",
}

_PR_REQUIRED_STAGES: set[str] = {
    "reviewing",
    "security",
    "qa",
    "documenting",
    "closing_review",
}


def _persona_role_for_stage(stage: str) -> str | None:
    """Return the persona role name for a pipeline stage, or None if unmapped."""
    return _STAGE_TO_ROLE.get(stage)


def _get_local_test_gate_config(state: CoordinareState) -> Any:
    """Resolve the active symphony's persona_scope.local_test_gate (spec 089).

    Mirrors ``monitor_performer._get_ci_gate_config``. Returns the
    ``LocalTestGateConfig`` if available, else None (legacy single-symphony
    mode), so the implementer local-test gate stays dormant until opt-in.
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


def _resolve_persona_slice_and_behavior(
    state: CoordinareState,
    card_id: str,
    role: str,
) -> tuple[dict[str, Any] | None, Any]:
    """074 — Resolve the per-card persona slice and matching scope_behavior config.

    Returns ``(slice, scope_behavior)``.  Either may be None: ``slice`` is None
    when no scope was classified for this card/persona; ``scope_behavior`` is
    None when the persona has not opted into scope tiering (FR-010 shadow mode).
    Both lookups walk the same chain (state → active_sessions[card_id] →
    persona_scope → personas[role] / config.personas[role].scope_behavior), so
    factoring them here keeps the skip-routing and tier-injection sites in sync.
    """
    active_sessions = state.get("active_sessions") or {}
    session = active_sessions.get(card_id) if isinstance(active_sessions, dict) else None
    persona_scope = session.get("persona_scope") if isinstance(session, dict) else None
    slice_dict: dict[str, Any] | None = None
    if isinstance(persona_scope, dict):
        raw_slice = (persona_scope.get("personas") or {}).get(role)
        if isinstance(raw_slice, dict):
            slice_dict = raw_slice

    config = state.get("config")
    personas_cfg = getattr(config, "personas", None) if config is not None else None
    persona_cfg = getattr(personas_cfg, role, None) if personas_cfg is not None else None
    scope_behavior = (
        getattr(persona_cfg, "scope_behavior", None) if persona_cfg is not None else None
    )
    return slice_dict, scope_behavior


def _scanner_unavailable_finding(reason: str) -> dict[str, Any]:
    """083 — Synthetic fail-closed finding for a broken/unavailable scanner.

    ``reason`` is a short, payload-free phrase (no diff text, no token, no raw
    tool output) — it only describes *which* stage failed, never *what* the
    scanner saw.
    """
    return {
        "severity": "critical",
        "category": "scanner_unavailable",
        "description": f"security scanner unavailable: {reason}",
        "file": "",
        "line": 0,
        "routing": "halt",
    }


async def _run_security_floor(
    state: CoordinareState, card: dict[str, Any]
) -> list[dict[str, Any]]:
    """083 US1 — Fetch the PR diff and run the scanner once (Contract 3).

    Returns the normalized findings list. On *any* diff-fetch or scanner error
    returns a single synthetic ``scanner_unavailable`` critical finding so the
    gate fails closed (FR-008). INFO logging is summary-only (counts), never raw
    diff text or finding messages (FR-011).
    """
    github = state.get("github_service")
    pr_url = str(card.get("pr_url") or "").strip()
    card_id = str(card.get("id", ""))

    if github is None or not hasattr(github, "get_pr_diff") or not pr_url:
        logger.error(
            "security_floor.diff_unavailable",
            card_id=card_id,
            has_github=github is not None,
            has_pr_url=bool(pr_url),
        )
        return [_scanner_unavailable_finding("PR diff source unavailable")]

    try:
        _raw_diff, changed_files = await github.get_pr_diff(pr_url)
    except Exception as exc:
        logger.error(
            "security_floor.diff_fetch_failed",
            card_id=card_id,
            error_type=type(exc).__name__,
        )
        return [_scanner_unavailable_finding("diff fetch failed")]

    repo_root = state.get("workspace_path")
    try:
        findings = scan_diff(changed_files, repo_root)
    except ScannerError as exc:
        logger.error(
            "security_floor.scan_failed",
            card_id=card_id,
            error_type=type(exc).__name__,
        )
        return [_scanner_unavailable_finding("scanner failed")]

    gating = sum(1 for f in findings if f.get("severity") in ("critical", "high"))
    logger.info(
        "security_floor.complete",
        card_id=card_id,
        file_count=len(changed_files),
        finding_count=len(findings),
        gating_count=gating,
    )
    return findings


# Persona roles that issue a verdict on an *existing* PR's code and therefore
# need the raw diff to assess it. The diff is injected into card_context so a
# model that does not proactively fetch it (observed: gpt-oss:120b rejecting a
# PR with "no code changes were supplied for review") still sees the changes.
# ``security`` is excluded: it runs its own diff fetch via the scanner floor.
_DIFF_REVIEW_ROLES: frozenset[str] = frozenset(
    {"reviewer", "closer", "qa", "tech_writer"}
)

# Cap on the inline PR diff injected into review prompts. A diff bloated by
# committed agent-tooling artifacts (.codex/, .tmp/), vendored trees, or binary
# files can blow past the model's context window — observed live: a 573 KB diff
# (99% committed .codex/ Codex-CLI junk) produced a ~169k-token QA prompt that
# tripped LiteLLM's ContextWindowExceededError on a 40,960-token model → HTTP
# 400 → QA returned zero evidence and looped. We FILTER obvious noise per-file
# sections first, THEN cap the remainder — filtering (not a head-truncation)
# because noise paths like `.codex/` sort BEFORE the real change, so a blind
# head-cut would keep the junk and drop the actual diff.
_DIFF_INJECT_MAX_CHARS = 60_000
_DIFF_NOISE_PATH_MARKERS: tuple[str, ...] = (
    ".codex/", ".tmp/", "node_modules/", "vendor/bundle/",
    ".venv/", "__pycache__/", ".git/",
)


def _diff_section_path(section: str) -> str:
    """Best-effort a/-side path from a ``diff --git a/PATH b/PATH`` header line."""
    m = re.match(r"diff --git a/(.+?) b/", section.split("\n", 1)[0])
    return m.group(1) if m else ""


def _sanitize_pr_diff(raw_diff: str, *, max_chars: int = _DIFF_INJECT_MAX_CHARS) -> str:
    """Drop agent-tooling/vendor/binary per-file sections, then cap total size.

    Keeps any preamble before the first ``diff --git`` header. Appends a short
    ``[coordinare: …]`` note when anything was omitted/truncated so the model
    knows the diff is partial (the persona already instructs fetching the full
    diff via ``gh pr diff`` when needed).
    """
    sections = re.split(r"(?m)^(?=diff --git )", raw_diff)
    kept: list[str] = []
    dropped_noise = 0
    dropped_binary = 0
    for sec in sections:
        if not sec.strip():
            continue
        if not sec.startswith("diff --git "):
            kept.append(sec)  # preamble before the first file header
            continue
        path = _diff_section_path(sec)
        if path and any(mk in path for mk in _DIFF_NOISE_PATH_MARKERS):
            dropped_noise += 1
            continue
        if "\nBinary files " in sec:
            dropped_binary += 1
            continue
        kept.append(sec)
    filtered = "".join(kept)
    truncated = len(filtered) > max_chars
    if truncated:
        filtered = filtered[:max_chars]
    notes: list[str] = []
    if dropped_noise or dropped_binary:
        notes.append(
            f"omitted {dropped_noise} tooling/vendor and {dropped_binary} "
            f"binary file section(s)"
        )
    if truncated:
        notes.append(
            f"diff truncated to {max_chars} chars — run `gh pr diff <pr_url>` "
            f"for the full changes"
        )
    if notes:
        filtered = filtered.rstrip() + "\n\n[coordinare: " + "; ".join(notes) + "]\n"
    return filtered


async def _fetch_pr_diff_text(
    state: CoordinareState, card: dict[str, Any]
) -> str | None:
    """Fetch the raw unified PR diff for a review role, or None on any failure.

    Best-effort: a missing github service, missing ``pr_url``, or a fetch error
    returns ``None`` so the caller simply omits the inline diff — the hardened
    review persona instructs the model to fetch the diff itself as a fallback,
    so dispatch must never be blocked by this.

    FR-011: the raw diff text is NEVER logged (only a length summary on
    success and an error type on failure).
    """
    github = state.get("github_service")
    pr_url = str(card.get("pr_url") or "").strip()
    card_id = str(card.get("id", ""))

    if github is None or not hasattr(github, "get_pr_diff") or not pr_url:
        return None

    try:
        raw_diff, _changed_files = await github.get_pr_diff(pr_url)
    except Exception as exc:
        logger.warning(
            "dispatch_performer.review_diff_fetch_failed",
            card_id=card_id,
            error_type=type(exc).__name__,
        )
        return None

    raw_diff = raw_diff or ""
    if not raw_diff.strip():
        return None

    # Filter noise + cap so a bloated diff can't overflow the model context.
    sanitized = _sanitize_pr_diff(raw_diff)
    if not sanitized.strip():
        return None

    logger.info(
        "dispatch_performer.review_diff_injected",
        card_id=card_id,
        diff_length=len(sanitized),
        raw_diff_length=len(raw_diff),
    )
    return sanitized


async def _pre_dispatch_rebase_guard(state: CoordinareState, card_id: str) -> bool:
    """097: rebase a conflicting/behind in-flight branch before a performer starts.

    Returns ``True`` to proceed with dispatch, ``False`` to skip this cycle.

    A performer is dispatched only when the base is current — either already, or
    after a clean rebase here. A branch known to be CONFLICTING/BEHIND that cannot
    be made current is NOT dispatched onto (returns False → defer): a fresh
    performer cannot fix a stale-base conflict (the 096 livelock). On a genuine
    conflict the implementer is routed via 047 conflict-resolution feedback and
    re-dispatched on a LATER cycle (so ``check_inflight`` re-validates the mutated
    stage — FR-004 stays intact). A dispatch already carrying pending feedback
    (incl. that conflict-resolution feedback) is let through unchanged so the
    resolution path is never starved.

    Fail-open: when the guard cannot act (no open PR / missing deps / mergeability
    unknown-but-not-yet-conflicting / any error) it returns ``True`` — it never
    blocks a dispatch it has no positive reason to hold (FR-006/FR-009). It only
    returns ``False`` once it has CONFIRMED the branch is conflicting/behind.
    Reuses spec-096/047 machinery; writes 096's anti-thrash marker.
    """
    import contextlib

    # A feedback-driven dispatch (review feedback, or 047 conflict-resolution
    # feedback) carries explicit work for the performer — let it through so the
    # guard never starves the conflict-resolution path it set up on a prior cycle.
    # This is BOUNDED, not an unbounded dispatch-onto-conflict: relay_feedback is
    # consumed (cleared) by _dispatch_performer_body once delivered, and a
    # resolution attempt that fails to move the branch head trips the anti-thrash
    # marker (blocked_thrash) on a later cycle. So a conflict resolves in at most
    # one performer attempt per head before the card is held for an operator.
    if state.get("relay_feedback"):
        return True

    card = state.get("current_card") or {}
    pr_url = card.get("pr_url") if isinstance(card, dict) else None
    pr_node_id = str(card.get("pr_node_id") or "").strip() if isinstance(card, dict) else ""
    github = state.get("github_service")
    config = state.get("config")
    # Guard N/A → dispatch as today: no open published PR (first run that will
    # create the branch), or missing deps (FR-006).
    if not (pr_url and pr_node_id and github is not None and config is not None):
        return True

    try:
        from coordinare.models.rebase import RebaseOutcome
        from coordinare.services.rebase import (
            classify_pre_dispatch,
            fetch_main_sha,
            prepare_conflict_resolution,
            repo_url_from_config,
            run_rebase_round,
        )

        repo_url = repo_url_from_config(config)
        token = ""
        if hasattr(github, "_current_token"):
            with contextlib.suppress(Exception):
                token = await github._current_token()
        if not (repo_url and token):
            return True  # auto-rebase not available (same gate as 047/096)

        current_main = state.get("last_known_main_sha")
        if not current_main:
            with contextlib.suppress(Exception):
                current_main = await fetch_main_sha(repo_url, token)
        if not current_main:
            return True

        mc = await github.check_mergeability(pr_node_id)
        raw = mc.get("mergeable_raw") or ""
        mss = mc.get("merge_state_status") or ""
        head = mc.get("head_ref_oid") or ""
        decision = classify_pre_dispatch(raw, mss, head, state, current_main)

        if decision == "proceed":
            return True
        if decision == "defer":
            logger.info(
                "dispatch_performer.pre_dispatch_defer",
                card_id=card_id, mergeable=raw or "?",
            )
            return False
        if decision == "blocked_thrash":
            # Confirmed CONFLICTING and already BLOCKED/FAILED against this same
            # (main, head): do NOT dispatch a fresh performer onto it.
            logger.warning(
                "dispatch_performer.pre_dispatch_held_on_conflict",
                card_id=card_id, branch=str(mc.get("head_ref_name") or ""),
            )
            return False

        # decision == "rebase". Rebase the branch directly — source it from the PR
        # (head_ref_name), NOT workspace_branch (which may be unset pre-dispatch).
        branch = str(mc.get("head_ref_name") or state.get("workspace_branch") or "")
        if not branch.startswith("coordinare/"):
            # Confirmed conflicting/behind but we cannot identify the branch to
            # rebase — DO NOT dispatch onto the conflicting base (FR-002). Record a
            # marker so we don't re-attempt this every cycle (FR-008), then defer;
            # 096's check_board sweep (no active performer now) can still heal it.
            if head:
                state["last_rebase_attempt"] = {  # type: ignore[typeddict-unknown-key]
                    "main_sha": current_main, "head_sha": head, "outcome": "failed",
                }
            logger.warning(
                "dispatch_performer.pre_dispatch_branch_unknown",
                card_id=card_id, mergeable=raw or "?",
            )
            return False

        # Build an explicit session carrying the branch so detect_stale_branches
        # (inside run_rebase_round) includes it regardless of workspace_branch.
        rebase_session = {
            "workspace_branch": branch,
            "phase": "dispatching",
            "current_card": card,
            "last_rebase_attempt": state.get("last_rebase_attempt"),
        }
        rr = await run_rebase_round(
            {card_id: rebase_session}, current_main, repo_url, token,
            notification_service=state.get("notification_service"),
            github=github, human_reviewers=state.get("human_reviewers"),
        )
        if not rr.jobs:
            # A confirmed-conflicting branch we could not rebase — do NOT dispatch
            # onto it (this is the bug 097 exists to prevent). Record a marker so
            # the guard doesn't re-rebase it every cycle (FR-008), then defer.
            if head:
                state["last_rebase_attempt"] = {  # type: ignore[typeddict-unknown-key]
                    "main_sha": current_main, "head_sha": head, "outcome": "failed",
                }
            logger.warning(
                "dispatch_performer.pre_dispatch_rebase_no_op",
                card_id=card_id, branch=branch,
            )
            return False

        prev_main = current_main
        proceed = True
        for job in rr.jobs:
            # Marker head: after a clean rebase the branch head is the new
            # post-rebase commit (what the next cycle's check_mergeability will
            # report); on a non-pushing outcome (BLOCKED/FAILED) it is the head we
            # just checked.
            if job.outcome in (RebaseOutcome.CLEAN, RebaseOutcome.PERFORMER_RESOLVED) and job.post_rebase_sha:
                marker_head = job.post_rebase_sha
            else:
                marker_head = head or job.pre_rebase_sha or ""
            state["last_rebase_attempt"] = {  # type: ignore[typeddict-unknown-key]
                "main_sha": current_main,
                "head_sha": marker_head,
                "outcome": job.outcome.value,
            }
            logger.info(
                "rebase.triggered", reason="pre_dispatch",
                card_id=card_id, branch=job.branch,
                prev_main_sha=prev_main, current_main_sha=current_main,
                outcome=job.outcome.value,
            )
            if job.outcome == RebaseOutcome.BLOCKED:
                # 047 US2: set up performer-driven conflict resolution (relay
                # feedback + implementer stage), then DEFER. Next cycle re-enters
                # dispatch_performer where check_inflight re-validates the new
                # stage (FR-004) and this guard lets the feedback-bearing dispatch
                # through to resolve the conflict.
                prepare_conflict_resolution(job, state, human_reviewers=state.get("human_reviewers"))
                proceed = False
            elif job.outcome == RebaseOutcome.FAILED:
                proceed = False  # do not dispatch onto a failed rebase; retry next cycle
        # Record the main we reconciled against (mirror check_board), so the
        # marker comparison stays consistent across cycles.
        state["last_known_main_sha"] = current_main  # type: ignore[typeddict-unknown-key]
        return proceed
    except Exception as exc:  # FR-009: a guard bug must never block dispatch
        logger.warning(
            "dispatch_performer.pre_dispatch_guard_failed",
            card_id=card_id, error=str(exc),
        )
        return True


async def dispatch_performer(state: CoordinareState) -> CoordinareState:
    """Public entry point for the dispatch graph node.

    076 (FR-001, FR-006, T034): acquire the per-``(card_id, performer_stage)``
    mutex and check the in-flight guard BEFORE delegating to the underlying
    dispatch body.  The mutex prevents two concurrent graph invocations
    from both passing the guard.  The guard prevents a second dispatch when
    a session is already in flight for the same ``(card, stage)``.

    If either identifier is unavailable (e.g. cold start with no current_card),
    the dispatch falls through to the existing body unguarded — the
    body's own missing-prerequisites check will handle it.

    The pending-override path (031) runs before the mutex on purpose:
    operator skip/restart signals must not be blocked by a stuck mutex,
    and they explicitly mutate ``performer_stage`` / ``current_card`` so
    their identifiers are not stable until they have been applied.
    """
    from coordinare.graph.nodes.monitor_performer import _apply_pending_override
    from coordinare.services.dispatch_guard import check_inflight, dispatch_mutex

    # Apply any operator override OUTSIDE the mutex.  Override return values
    # are honoured directly (some terminate dispatch entirely; the
    # `dispatching` branch falls through into the guarded body below).
    override_result = _apply_pending_override(state)
    if override_result is not None:
        new_phase = override_result.get("phase")
        if new_phase != "dispatching":
            # Override produced a terminal-for-this-cycle state (blocked /
            # monitoring_pr); honour it directly.  The body in
            # _dispatch_performer_body will not run.
            github_for_override = state.get("github_service")
            return await _apply_override_terminal(override_result, state.get("current_card"), github_for_override)
        # Override kept us in dispatching with mutated state — proceed.
        state = override_result

    card = state.get("current_card")
    performer_stage = str(state.get("performer_stage") or "")
    card_id = str(card.get("id", "")) if isinstance(card, dict) else ""

    if not card_id or not performer_stage:
        # No identifiers → cannot acquire a per-card mutex.  The body's
        # missing-prerequisites branch handles this safely (sets phase=idle).
        return await _dispatch_performer_body(state)

    async with dispatch_mutex(card_id, performer_stage):
        guard = await check_inflight(state, card_id, performer_stage)
        if guard.advice == "refuse":
            # Another in-flight session for this (card, stage) already
            # exists and is live.  Return state UNMODIFIED so the graph
            # loops back through monitor_performer for the existing
            # session.  The structured log event is emitted from
            # check_inflight (`dispatch_performer.in_flight_guard_tripped`).
            return state

        # 076 (T112, FR-024): multi-PR detection at the DISPATCH trigger.
        # If we'd be about to launch a performer for a card with > 1
        # open PR on the canonical prefix, refuse with a structured
        # event so the operator can resolve the divergence manually.
        try:
            from coordinare.services.dispatch_guard import detect_multi_pr_divergence

            github = state.get("github_service")
            current_card = state.get("current_card") or {}
            pr_url = current_card.get("pr_url") if isinstance(current_card, dict) else None
            owner, repo = _owner_repo_from_pr_url(pr_url) if pr_url else (None, None)
            if github is not None and owner and repo:
                divergence = await detect_multi_pr_divergence(
                    state,
                    card_id,
                    github_service=github,
                    owner=owner,
                    repo=repo,
                    trigger="dispatch",
                )
                if divergence is not None:
                    logger.warning(
                        "dispatch_performer.multi_pr_divergence_refused",
                        card_id=card_id,
                        pr_numbers=divergence.get("pr_numbers"),
                    )
                    return state
        except Exception as _exc:  # pragma: no cover — exercised via test_multi_pr_check_crash
            # Detection failure MUST NOT block dispatch — that path
            # historically produced today's bug.  Log and proceed.
            logger.warning(
                "dispatch_performer.multi_pr_divergence_check_crashed",
                card_id=card_id,
                error=str(_exc),
            )

        # 097: pre-dispatch rebase guard. We are past check_inflight (no performer
        # is running for this (card, stage) — FR-004 holds by construction) and
        # about to start one. If this in-flight card's open-PR branch is
        # CONFLICTING/BEHIND main, rebase it FIRST so the performer never starts on
        # a stale base it cannot fix (the 096 active-performer livelock). Reuses
        # 096/047 machinery; degrades safely (proceeds with a normal dispatch) on
        # any error or when it cannot act (FR-009 isolation).
        if not await _pre_dispatch_rebase_guard(state, card_id):
            # Mergeability unknown / rebase failed / held on an unresolvable
            # conflict — do NOT dispatch a performer this cycle; phase stays
            # "dispatching" so the next cycle re-evaluates (FR-003/005).
            return state

        return await _dispatch_performer_body(state)


def _owner_repo_from_pr_url(pr_url: str | None) -> tuple[str | None, str | None]:
    """Extract (owner, repo) from a github.com PR URL.  None on failure."""
    if not isinstance(pr_url, str):
        return None, None
    import re
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/\d+", pr_url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


async def _apply_override_terminal(
    override_result: CoordinareState,
    card: dict[str, Any] | None,
    github: Any,
) -> CoordinareState:
    """Handle the non-`dispatching` outcomes of `_apply_pending_override`.

    Extracted from the original dispatch_performer body so the guarded
    body itself never sees an override-finalising state.  Mirrors the
    pre-076 behaviour exactly.
    """
    new_phase = override_result.get("phase")
    if new_phase == "monitoring_pr" and github is not None:
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
            override_result["phase"] = "system_error"
            override_result["system_error_count"] = override_result.get("system_error_count", 0) + 1
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
    # Blocked or any other non-dispatching terminal — return as-is.
    return override_result


async def _dispatch_performer_body(state: CoordinareState) -> CoordinareState:
    """Original dispatch_performer body (pre-076 logic).

    Reads ``performer_stage`` from state, resolves the service from
    ``performer_services[performer_stage]``, performs a health check,
    prepares the workspace, and dispatches the card.  On success the phase
    transitions to ``monitoring_performer``.

    If the resolved service is ``None`` the stage is skipped by calling
    ``_advance_stage`` from ``monitor_performer``.

    Called by the public ``dispatch_performer`` only after the in-flight
    guard + per-card mutex have authorised the dispatch (T034).  The
    pending-override handling that used to live here has been hoisted
    into ``dispatch_performer`` so the override path runs outside the
    mutex (overrides MUST NOT block on a stuck dispatch).
    """
    # Lazy import to avoid circular dependency -- monitor_performer is created
    # in parallel and will exist by the time this node is actually invoked.
    from coordinare.graph.nodes.monitor_performer import _advance_stage

    card: dict[str, Any] | None = state.get("current_card")
    github = state.get("github_service")

    # 076 (T034): The pending-override handler used to live here.  It has
    # been hoisted into the public ``dispatch_performer`` wrapper so the
    # override path runs OUTSIDE the per-card mutex.  By the time this
    # body runs, the override (if any) has already been applied to state.
    performer_stage: str = state.get("performer_stage", "")  # type: ignore[assignment]
    performer_services: dict[str, Any] = state.get("performer_services", {})  # type: ignore[assignment]

    if not isinstance(card, dict) or github is None or not performer_stage:
        logger.warning(
            "dispatch_performer.missing_prerequisites",
            has_card=isinstance(card, dict),
            has_github=github is not None,
            performer_stage=performer_stage,
        )
        state["phase"] = "idle"
        return state

    # PR-dependent stages cannot run without PR identifiers. If they're missing,
    # recover from the linked issue's open PR when possible; otherwise route
    # back to implementing so a fresh PR can be created.
    card_id = str(card.get("id", ""))
    issue_id = str(card.get("issue_id") or "").strip()

    # Persona scope skip routing (FR-008, FR-010): if the persona has opted
    # into scope tiering and the classifier set depth=skip, advance past it.
    # Closer is scope-invariant (FR-009) so applying uniformly is safe.
    scope_role = _persona_role_for_stage(performer_stage)
    if scope_role is not None and card_id:
        slice_dict, scope_behavior = _resolve_persona_slice_and_behavior(
            state, card_id, scope_role
        )
        if (
            slice_dict is not None
            and scope_behavior is not None
            and slice_dict.get("depth") == "skip"
        ):
            logger.info(
                "persona_scope.persona_skipped",
                card_id=card_id,
                persona=scope_role,
                performer_stage=performer_stage,
                focus=slice_dict.get("focus"),
                overrides=slice_dict.get("overrides") or [],
            )
            updates = _advance_stage(state)
            for key, value in updates.items():
                state[key] = value  # type: ignore[literal-required]
            return state

    # 124 (FR-006): the documenter maintains the living docs/wiki from the card's
    # CODE changes, not just its docs/ paths — so it runs on every card and
    # no-ops (empty {files}) when there is nothing to document. The 123 docs-path
    # skip is therefore removed for the wiki-maintaining documenter.

    # 053: Guard against repeated PR churn for the same issue.
    config = state.get("config")
    raw_closed_pr_limit = getattr(config, "max_closed_pr_attempts_per_issue", 0) if config else 0
    closed_pr_limit = raw_closed_pr_limit if isinstance(raw_closed_pr_limit, int) else 0
    raw_transport_timeout = getattr(config, "transport_timeout_seconds", 30) if config else 30
    closed_pr_count_timeout_seconds = (
        float(raw_transport_timeout)
        if isinstance(raw_transport_timeout, int | float) and raw_transport_timeout > 0
        else 30.0
    )
    if performer_stage == "implementing" and issue_id and closed_pr_limit > 0:
        pr_url = str(card.get("pr_url") or "").strip()
        pr_node_id = str(card.get("pr_node_id") or "").strip()
        has_open_pr = bool(pr_url and pr_node_id)
        if not has_open_pr and hasattr(github, "find_pr_for_issue"):
            recovered = None
            try:
                recovered = await github.find_pr_for_issue(issue_id)
            except Exception as exc:
                logger.warning(
                    "dispatch_performer.pr_recovery_check_failed",
                    card_id=card_id,
                    issue_id=issue_id,
                    error=str(exc),
                )
            if recovered and recovered.get("pr_url") and recovered.get("pr_node_id"):
                card["pr_url"] = recovered["pr_url"]
                card["pr_node_id"] = recovered["pr_node_id"]
                _set_current_card(state, card)
                has_open_pr = True
        if not has_open_pr and hasattr(github, "count_closed_prs_for_issue"):
            closed_count = 0
            try:
                closed_count = await asyncio.wait_for(
                    github.count_closed_prs_for_issue(issue_id),
                    timeout=closed_pr_count_timeout_seconds,
                )
            except TimeoutError:
                logger.warning(
                    "dispatch_performer.closed_pr_count_timeout",
                    card_id=card_id,
                    issue_id=issue_id,
                    timeout_seconds=closed_pr_count_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "dispatch_performer.closed_pr_count_failed",
                    card_id=card_id,
                    issue_id=issue_id,
                    error=str(exc),
                )
            if closed_count >= closed_pr_limit:
                logger.warning(
                    "dispatch_performer.closed_pr_limit_reached",
                    card_id=card_id,
                    issue_id=issue_id,
                    closed_pr_count=closed_count,
                    limit=closed_pr_limit,
                )
                try:
                    await github.move_card(card_id, "BLOCKED")
                except Exception as exc:
                    logger.warning(
                        "dispatch_performer.move_card_to_blocked_failed",
                        card_id=card_id,
                        error=str(exc),
                    )
                state["phase"] = "blocked"
                state["open_questions"] = [
                    (
                        f"Card has {closed_count} closed PR attempts (limit: {closed_pr_limit}). "
                        "Blocking new PR creation. Please review prior PRs and decide whether to "
                        "resume manually, reset branch strategy, or close the card."
                    ),
                ]
                return state

    if performer_stage in _PR_REQUIRED_STAGES and issue_id:
        pr_url = str(card.get("pr_url") or "").strip()
        pr_node_id = str(card.get("pr_node_id") or "").strip()
        if not (pr_url and pr_node_id):
            recovered: dict[str, str] | None = None
            if hasattr(github, "find_pr_for_issue"):
                try:
                    recovered = await github.find_pr_for_issue(issue_id)
                except Exception as exc:
                    logger.warning(
                        "dispatch_performer.pr_recovery_failed",
                        card_id=str(card.get("id", "")),
                        performer_stage=performer_stage,
                        issue_id=issue_id,
                        error=str(exc),
                    )
            if recovered and recovered.get("pr_url") and recovered.get("pr_node_id"):
                card["pr_url"] = recovered["pr_url"]
                card["pr_node_id"] = recovered["pr_node_id"]
                _set_current_card(state, card)
            else:
                lifecycle: list[str] = list(state.get("lifecycle_sequence") or [])
                fallback_stage = "implementing" if "implementing" in lifecycle else ""
                if fallback_stage and fallback_stage != performer_stage:
                    logger.warning(
                        "dispatch_performer.missing_pr_context_fallback",
                        card_id=str(card.get("id", "")),
                        from_stage=performer_stage,
                        to_stage=fallback_stage,
                    )
                    state["performer_stage"] = fallback_stage
                    state["phase"] = "dispatching"
                    state["agent_dispatch"] = {}
                    state["agent_dispatch_at"] = None
                    return state
                logger.error(
                    "dispatch_performer.missing_pr_context_blocked",
                    card_id=str(card.get("id", "")),
                    performer_stage=performer_stage,
                )
                state["phase"] = "blocked"
                state["open_questions"] = [
                    (
                        f"Cannot run stage '{performer_stage}' because PR context is missing "
                        "(`pr_url`/`pr_node_id`) and no open PR could be recovered from "
                        "the linked issue."
                    ),
                ]
                return state

    # 048: Resolve the service for this stage via SlotManager if available.
    # The SlotManager enforces per-role max_concurrency and returns a free
    # service instance, or None if at capacity (card retries next cycle).
    slot_manager = state.get("slot_manager")
    service: AgentServiceProtocol | None = None
    if slot_manager is not None and hasattr(slot_manager, "acquire"):
        service = slot_manager.acquire(
            performer_stage, card_id, config=state.get("config"),
        )
        if service is None and hasattr(slot_manager, "is_at_capacity") and slot_manager.is_at_capacity(performer_stage):
            # At capacity — card waits. Return without changing phase so
            # the next poll cycle retries.
            logger.info(
                "dispatch_performer.at_capacity",
                performer_stage=performer_stage,
                card_id=card_id,
            )
            return state
        # If slot_manager returned None but NOT at capacity:
        # - Stage has no pool (unknown) → fall through to legacy
        # - Stage has pool with max=0 (disabled) → don't fall through
        #   (skip the role, same as if not configured)
        if service is None:
            pool = slot_manager.pools.get(performer_stage) if hasattr(slot_manager, "pools") else None
            if pool is not None and pool.max_concurrency <= 0:
                service = None  # disabled — will be skipped below
            else:
                service = performer_services.get(performer_stage)
    else:
        # Legacy path: single service per stage (backward compatible)
        service = performer_services.get(performer_stage)

    # Fallback to legacy agent_service only when performer_services is empty
    # (backward compatibility with pre-019 configurations).
    if service is None and not performer_services:
        service = state.get("agent_service")

    if service is None:
        # No service configured for this stage -- skip to the next role.
        logger.info(
            "dispatch_performer.no_service_for_stage",
            performer_stage=performer_stage,
        )
        updates = _advance_stage(state)
        for key, value in updates.items():
            state[key] = value  # type: ignore[literal-required]
        return state

    # 048: If we acquired a slot from the SlotManager, we must release it
    # on any early error return (health check, workspace, transport failure)
    # to prevent slot leaks.
    _acquired_via_slot_mgr = (
        slot_manager is not None
        and hasattr(slot_manager, "release")
        and service is not None
    )

    def _release_slot_on_error() -> None:
        if _acquired_via_slot_mgr:
            slot_manager.release(performer_stage, card_id)

    # --- Health check with retry (033) ---
    card_id = str(card.get("id", ""))

    config = state.get("config")
    max_attempts = 3
    backoff_base = 1.0
    if config is not None and hasattr(config, "health_check"):
        max_attempts = config.health_check.max_attempts
        backoff_base = config.health_check.backoff_seconds

    health: dict[str, Any] = {}
    health_status = "unknown"
    from time import monotonic as _monotonic
    _t0 = _monotonic()

    for attempt in range(1, max_attempts + 1):
        try:
            health = await service.check_health()
            health_status = str(health.get("status", "unknown"))
        except asyncio.CancelledError:
            raise
        except Exception:
            health_status = "unreachable"

        if health_status not in {"unknown", "unreachable"}:
            if attempt > 1:
                logger.info(
                    "dispatch_performer.health_check_retry_succeeded",
                    performer_stage=performer_stage,
                    attempt=attempt,
                    elapsed_seconds=round(_monotonic() - _t0, 1),
                )
            break

        if attempt < max_attempts:
            delay = backoff_base * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    state["agent_health_status"] = health_status

    if health_status in {"unknown", "unreachable"}:
        logger.warning(
            "dispatch_performer.health_check_retries_exhausted",
            health_status=health_status,
            performer_stage=performer_stage,
            attempts=max_attempts,
            elapsed_seconds=round(_monotonic() - _t0, 1),
        )
        _release_slot_on_error()
        state["phase"] = "idle"
        return state

    if health_status == "error":
        health_reason = health.get("reason", "") if isinstance(health, dict) else ""
        state["phase"] = "blocked"
        state["open_questions"] = [
            f"Performer health check failed for stage {performer_stage!r} "
            f"(status: {health_status}). "
            + (f"Reason: {health_reason}" if health_reason else
               "Check performer logs for details.")
        ]
        _release_slot_on_error()
        return state

    # --- Workspace setup (011) ---
    workspace_manager = state.get("workspace_manager")
    workspace_info = None

    if workspace_manager is not None:
        try:
            workspace_info = await workspace_manager.prepare(card)
            state["workspace_path"] = workspace_info.path
            state["workspace_branch"] = workspace_info.branch
        except WorkspaceSetupError as exc:
            logger.error(
                "workspace_setup_failed.card_blocked",
                card_id=card_id,
                performer_stage=performer_stage,
                error=str(exc),
            )
            try:
                await github.move_card(card_id, "BLOCKED")
            except Exception as move_exc:
                logger.warning(
                    "dispatch_performer.move_card_to_blocked_failed",
                    card_id=card_id,
                    error=str(move_exc),
                )
            state["workspace_path"] = None
            state["workspace_branch"] = None
            _release_slot_on_error()
            state["phase"] = "blocked"
            state["open_questions"] = [str(exc)]
            return state
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = (
                f"Workspace setup failed unexpectedly ({type(exc).__name__}). "
                "Check coordinare logs for details."
            )
            logger.error(
                "workspace_setup_unexpected_error.card_blocked",
                card_id=card_id,
                performer_stage=performer_stage,
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                await github.move_card(card_id, "BLOCKED")
            except Exception as move_exc:
                logger.warning(
                    "dispatch_performer.move_card_to_blocked_failed",
                    card_id=card_id,
                    error=str(move_exc),
                )
            state["workspace_path"] = None
            state["workspace_branch"] = None
            _release_slot_on_error()
            state["phase"] = "blocked"
            state["open_questions"] = [reason]
            return state

        # Validate required workspace fields before dispatching.
        if workspace_info is not None:
            required: list[tuple[str, str]] = [
                ("repo_url", workspace_info.repo_url),
                ("branch", workspace_info.branch),
            ]
            if workspace_info.path is not None:
                required.append(("github_token", workspace_info.github_token))
            missing = [field for field, value in required if not value]
            if missing:
                reason = (
                    f"Workspace context incomplete -- missing required fields: "
                    f"{', '.join(missing)}"
                )
                logger.error(
                    "dispatch_performer.incomplete_workspace",
                    card_id=card_id,
                    performer_stage=performer_stage,
                    missing=missing,
                )
                try:
                    await github.move_card(card_id, "BLOCKED")
                except Exception as move_exc:
                    logger.warning(
                        "dispatch_performer.move_card_to_blocked_failed",
                        card_id=card_id,
                        error=str(move_exc),
                    )
                if workspace_info.path is not None:
                    try:
                        await workspace_manager.teardown(workspace_info.path)
                    except Exception:
                        logger.warning(
                            "workspace_teardown_failed.after_incomplete_workspace",
                            card_id=card_id,
                        )
                state["workspace_path"] = None
                state["workspace_branch"] = None
                _release_slot_on_error()
                state["phase"] = "blocked"
                state["open_questions"] = [reason]
                return state

    # --- Build card context with persona instructions ---
    personas = load_personas_hot(state.get("config_path"), state.get("config"))
    card_context: dict[str, Any] = dict(card)

    role = _persona_role_for_stage(performer_stage)
    if role is not None:
        card_context["persona_instructions"] = get_effective_instructions(role, personas)

    # Include relay feedback when present (reviewer / QA feedback loops).
    relay_feedback: list[dict[str, Any]] | None = state.get("relay_feedback")  # type: ignore[assignment]
    if relay_feedback:
        card_context["relay_feedback"] = relay_feedback

    # 020: Include architecture plan in dispatch payload for downstream roles (FR-007).
    # The plan_path is set on the card by monitor_performer when the architect
    # returns plan_committed.
    plan_path = card.get("plan_path")
    if plan_path and performer_stage != "architecting":
        # Include plan path reference; downstream performers read from branch.
        card_context["architecture_plan_path"] = plan_path

    # Pass the performer role so the performer can gate behavior on it.
    card_context["role"] = performer_stage

    # 123 US4 (FR-011): on an assessor (re-)dispatch, carry forward answered Q&A
    # from prior assessor runs so the assessor doesn't re-ask questions already
    # answered on an earlier bounce. Injected as ``prior_clarifications`` only
    # when non-empty; absent on the first dispatch (empty list). Persisted by
    # monitor_performer after each successful assessor run (FR-010).
    if performer_stage == "assessing":
        prior_qa = state.get("assessor_open_questions") or []
        if prior_qa:
            card_context["prior_clarifications"] = [dict(q) for q in prior_qa]

    # 083 US1: coordinare-authoritative static-analysis floor. For the security
    # role ONLY, fetch the PR diff and run the scanner exactly once here at
    # dispatch. Findings are stashed on state (consumed by the monitor floor,
    # no re-scan) and injected into card_context as an advisory ceiling. Any
    # diff-fetch or scanner failure is fail-closed: a synthetic critical
    # ``scanner_unavailable`` finding (routing: halt) is stashed instead, so the
    # gate never silently passes on a broken scanner (FR-008).
    if role == "security":
        scanner_findings = await _run_security_floor(state, card)
        state["scanner_findings"] = scanner_findings
        card_context["scanner_findings"] = scanner_findings

    # Inject the raw PR diff for review roles so a model that does not fetch the
    # diff itself still has the changes to assess (drive-by fix: reviewer was
    # rejecting PRs with "no code changes were supplied for review"). Best-effort:
    # on any fetch failure the diff is omitted and the persona fallback applies.
    if role in _DIFF_REVIEW_ROLES:
        pr_diff_text = await _fetch_pr_diff_text(state, card)
        if pr_diff_text:
            card_context["pr_diff"] = pr_diff_text

    if performer_stage == "qa":
        latest_main_sha = state.get("last_known_main_sha")
        if latest_main_sha:
            card_context["latest_main_sha"] = latest_main_sha

    # 036: Include GitHub API URL so the performer connects to the same instance.
    config = state.get("config")
    if config is not None and hasattr(config, "github_api_url"):
        card_context["github_api_url"] = config.github_api_url

    # 037/055: Include per-role backend, model, and tuning params in dispatch payload.
    if config is not None and hasattr(config, "performers") and role is not None:
        role_config = config.performers.resolved_role(role)
        if role_config is not None:
            from coordinare.services.performer_tuning import translate_tuning
            card_context["backend"] = role_config.backend
            # 080: model + endpoint come from the role's mode (modes → model_endpoints
            # → endpoints), resolved against the root catalogs. Yields the same
            # card_context keys the downstream payload builder already consumes.
            card_context.update(config.resolve_performer_dispatch_model(role))
            # 080: for non-`single` strategies, carry the full orchestration block
            # (strategy + resolved upstream refs + params) so the performer can
            # launch the DualModelProxy. Rides card_context → metadata, like the
            # other per-role dispatch fields. Absent for single (no proxy).
            _orch = config.resolve_performer_orchestration(role)
            if _orch is not None:
                card_context["orchestration"] = _orch
            card_context.update(translate_tuning(role_config))

    # Apply per-persona scope_behavior tier (FR-007, FR-009, FR-010).
    # max_tool_calls + prompt_addon land as structured card_context fields;
    # the base persona prompt is never mutated.  Closer is scope-invariant
    # and only consumes `focus` as advisory context.
    if role is not None and card_id:
        slice_dict, scope_behavior = _resolve_persona_slice_and_behavior(
            state, card_id, role
        )
        if slice_dict is not None:
            focus = slice_dict.get("focus")
            if focus:
                card_context["scope_focus"] = focus
            if role != "closer":
                depth = slice_dict.get("depth")
                if scope_behavior is not None and depth:
                    tier = getattr(scope_behavior, depth, None)
                    if tier is not None:
                        max_tc = getattr(tier, "max_tool_calls", None)
                        if max_tc is not None:
                            card_context["max_tool_calls"] = max_tc
                        addon = getattr(tier, "prompt_addon", None)
                        if addon:
                            card_context["scope_addon"] = addon
                    else:
                        logger.debug(
                            "persona_scope.dispatch.tier_missing",
                            card_id=card_id,
                            persona=role,
                            depth=depth,
                        )

    # 089: implementer local-test gate — coordinare-configured but executes in
    # the performer, so the enable flag + timeout must ride the dispatch payload
    # (Score.local_test_gate). Implementer-only; max_fix_attempts stays
    # coordinare-side (consumed by the monitor self-fix loop).
    if role == "implementer":
        gate_cfg = _get_local_test_gate_config(state)
        if gate_cfg is not None:
            card_context["local_test_gate"] = {
                "enabled": bool(getattr(gate_cfg, "enabled", False)),
                "timeout_seconds": int(getattr(gate_cfg, "timeout_seconds", 600)),
            }
        # 090-L3: when the CI gate built an autonomous baseline-repair mandate
        # (INHERITED failures, gate enabled, budget not exhausted), ride it into
        # the implementer payload so the performer scopes its work to repairing
        # the inherited red checks. Present only on a repair-dispatch cycle.
        repair_mandate = state.get("repair_mandate")
        if repair_mandate:
            card_context["repair_mandate"] = repair_mandate

    # --- Dispatch ---
    # T022/T033/T037 (060): Attach per-symphony env-cache volume so performers find
    # pre-built dev environments without burning tokens on re-installation.
    _extra_volumes = None
    _symphony_name_for_ec = state.get("current_symphony")
    _env_cache_for_ec = state.get("env_cache")
    if _symphony_name_for_ec is not None and _env_cache_for_ec:
        from coordinare.models.env_cache import EnvCacheState
        from coordinare.services.env_cache import (
            DEFAULT_DEVENV_ROOT,
            _cache_dir_has_activate,
            bootstrap_hold_detail,
            get_env_volume_for_symphony,
            resolve_test_env_vars,
        )
        from coordinare.services.http_performer_service import HTTPPerformerService
        from coordinare.services.test_env_loader import TestEnvFileError

        # 077: Gate consumer dispatch on the env cache being CURRENT and VERIFIED
        # for this symphony. A consumer must not run until the env_bootstrap phase
        # has *successfully* completed for the CURRENT spec — otherwise it runs
        # against a stale/incomplete toolchain (the bug that let cards sail through
        # on a Chrome-less cache while a re-bootstrap was in flight or had failed).
        # The env_bootstrap role itself is exempt — it's the run that populates the
        # cache. "Current + verified" requires ALL of:
        #   * cache_dir_ready             — at least one bootstrap succeeded
        #   * last_bootstrap_succeeded    — the most recent one verified-passed
        #     (the verify.sh gate defines "succeeded")
        #   * not bootstrap_in_flight     — no bootstrap is mid-run
        #   * activate.sh present on disk — the cache dir physically exists
        #   * readme_sha == last_seen_spec_sha — the cache reflects the CURRENT
        #     spec; check_and_trigger refreshes last_seen_spec_sha every cycle, so
        #     a README/spec change holds consumers until a fresh bootstrap succeeds
        #     for it (closes the stale-cache window).
        # Held cards are DEFERRED (slot released), not failed — they retry on the
        # next pickup cycle. (Supersedes the old activate.sh-only + opt-in
        # serialize_env_bootstrap gates.)
        _ec_state_for_sym = _env_cache_for_ec.get(_symphony_name_for_ec)

        _is_bootstrap_dispatch = performer_stage == "env_bootstrap"
        if not _is_bootstrap_dispatch and isinstance(_ec_state_for_sym, EnvCacheState):
            _current_and_verified = (
                _ec_state_for_sym.cache_dir_ready
                and bool(_ec_state_for_sym.last_bootstrap_succeeded)
                and not _ec_state_for_sym.bootstrap_in_flight
                and _cache_dir_has_activate(_ec_state_for_sym.cache_dir)
                and _ec_state_for_sym.last_seen_spec_sha is not None
                and _ec_state_for_sym.readme_sha == _ec_state_for_sym.last_seen_spec_sha
            )
            if not _current_and_verified:
                logger.info(
                    "dispatch_performer.env_cache_not_current",
                    card_id=card_id,
                    performer_stage=performer_stage,
                    symphony=_symphony_name_for_ec,
                    cache_dir_ready=_ec_state_for_sym.cache_dir_ready,
                    last_bootstrap_succeeded=_ec_state_for_sym.last_bootstrap_succeeded,
                    bootstrap_in_flight=_ec_state_for_sym.bootstrap_in_flight,
                    readme_sha=_ec_state_for_sym.readme_sha,
                    last_seen_spec_sha=_ec_state_for_sym.last_seen_spec_sha,
                    bootstrap_exhausted=_ec_state_for_sym.bootstrap_exhausted,
                    # 088 (FR-010): names the exhausted breaker when tripped.
                    detail=bootstrap_hold_detail(_ec_state_for_sym),
                )
                _release_slot_on_error()
                return state

            # 093: Toolchain-readiness dispatch gate. The "current + verified"
            # guard above keys on last_bootstrap_succeeded — a persisted flag from
            # the LAST bootstrap, which can be true while the toolchain for THIS
            # spec sha is still being built (the live website-symphony race: QA
            # dispatched against a cache whose ruby wasn't installed yet). Re-run
            # the manifest-driven verify.sh checklist in a clean-context container
            # on EVERY code-running dispatch (no cached verdict, FR-006) so the
            # decision keys on what is actually present/running/usable now.
            #   True  (exit 0)  ⇒ proceed with dispatch.
            #   False (nonzero) ⇒ withhold + release slot; the FAIL falls into the
            #     same env_cache_not_current hold (re-bootstrap is triggered by the
            #     bootstrap-completion path / budget machinery — single hold point).
            #   None  (verify.sh absent / docker error) ⇒ degraded: MUST NOT block,
            #     fall through on the legacy last_bootstrap_succeeded path.
            _readiness_passed, _readiness_detail = await verify_env_cache_clean(
                state, _symphony_name_for_ec, service
            )
            if _readiness_passed is False:
                logger.info(
                    "dispatch_performer.env_cache_not_current",
                    card_id=card_id,
                    performer_stage=performer_stage,
                    symphony=_symphony_name_for_ec,
                    # 093: the readiness gate (not the current+verified guard) is
                    # the deciding factor here — the toolchain for this spec sha is
                    # not yet usable. Keys/paths only (secret invariant).
                    readiness="fail",
                    readme_sha=_ec_state_for_sym.readme_sha,
                    last_seen_spec_sha=_ec_state_for_sym.last_seen_spec_sha,
                    bootstrap_exhausted=_ec_state_for_sym.bootstrap_exhausted,
                    detail=_readiness_detail,
                )
                # 093 (FR-007): kick the cache back to env-bootstrap. Reuse the
                # existing forced-regen seam (the same one monitor_performer uses
                # for a services-health failure) — mark_runtime_health_failed
                # flags the cache so the next check_and_trigger cycle dispatches a
                # re-bootstrap for the current spec sha and the dispatch falls
                # into the existing bootstrap_in_flight hold. The loop is bounded
                # by the EXISTING env_bootstrap_max_attempts budget (check_and_trigger
                # gates the forced regen on `not bootstrap_exhausted`), so a
                # genuinely-broken cache surfaces the existing bootstrap_exhausted
                # env-blocked verdict instead of thrashing — no new counter.
                _env_cache_svc = state.get("env_cache_service")
                if _env_cache_svc is not None:
                    try:
                        _env_cache_svc.mark_runtime_health_failed(
                            _symphony_name_for_ec, state
                        )
                    except Exception as _exc:  # pragma: no cover - defensive
                        logger.warning(
                            "dispatch_performer.readiness_rebootstrap_error",
                            symphony=_symphony_name_for_ec,
                            error=str(_exc),
                        )
                _release_slot_on_error()
                return state

        _devenv_root = DEFAULT_DEVENV_ROOT
        if isinstance(service, HTTPPerformerService):
            _devenv_root = service.devenv_root
        if isinstance(service, HTTPPerformerService) and service.mode == "persistent":
            _has_ready_caches = any(
                isinstance(s, EnvCacheState) and _cache_dir_has_activate(s.cache_dir)
                for s in _env_cache_for_ec.values()
            )
            if _has_ready_caches:
                logger.warning(
                    "env_cache.persistent_performer_volumes_not_live_mountable",
                    performer_stage=performer_stage,
                    detail=(
                        "Env-cache volumes cannot be added to a running persistent container. "
                        "Restart the performer container to pick up the mount."
                    ),
                )
            # Do not pass volumes to persistent performers — the container is
            # already running and Docker cannot hot-add mounts.
        else:
            _ec_result = get_env_volume_for_symphony(
                _symphony_name_for_ec,
                _env_cache_for_ec,
                is_bootstrap=_is_bootstrap_dispatch,
                container_devenv_root=_devenv_root,
            )
            if _ec_result is not None:
                _ec_vol, _ec_container_path = _ec_result
                _extra_volumes = [_ec_vol]
                card_context["env_cache_path"] = _ec_container_path

        # 092 US2: inject the symphony's configured (or agent-discovered) test-env
        # vars into the consumer/QA-runtime dispatch via the redacted `secrets`
        # channel (http_performer_service routes card_context["test_env_vars"] →
        # secrets, BEFORE operational secrets so a test file can never clobber
        # them). The configured `test_env` block always wins (resolve_test_env_vars
        # ignores fallback_source when test_env is set); absent one, the persisted
        # agent-discovered path is reloaded. Bootstrap dispatch is exempt — it
        # receives test-env vars through the BootstrapJobPayload secrets seam.
        if not _is_bootstrap_dispatch:
            _sym_cfg_for_te = (state.get("symphony_configs") or {}).get(
                _symphony_name_for_ec
            )
            _config_for_te = state.get("config")
            _github_for_te = state.get("github_service")
            if (
                _sym_cfg_for_te is not None
                and _config_for_te is not None
                and _github_for_te is not None
            ):
                # state["config"] is already the ProjectConfiguration (the
                # global config), not a CoordinareConfiguration wrapper — pass it
                # directly to effective_config(), which expects a
                # ProjectConfiguration as its base.
                _eff_for_te = _sym_cfg_for_te.effective_config(_config_for_te)
                _fallback_src = (
                    _ec_state_for_sym.test_env_source
                    if isinstance(_ec_state_for_sym, EnvCacheState)
                    else None
                )
                try:
                    _test_env_vars = await resolve_test_env_vars(
                        symphony_name=_symphony_name_for_ec,
                        test_env=getattr(_sym_cfg_for_te, "test_env", None),
                        github_org=_eff_for_te.github_org,
                        repo=_eff_for_te.project_name or _symphony_name_for_ec,
                        github_service=_github_for_te,
                        fallback_source=_fallback_src,
                    )
                except TestEnvFileError as exc:
                    # A configured-but-missing file is a clear coordinare-side
                    # error (logged), never a silent empty dict. The consumer
                    # proceeds without the var and the genuinely-unset var still
                    # trips the services-start.sh exit-75 gate downstream.
                    logger.warning(
                        "dispatch_performer.test_env_load_failed",
                        card_id=card_id,
                        symphony=_symphony_name_for_ec,
                        error=str(exc),
                    )
                    _test_env_vars = {}
                if _test_env_vars:
                    card_context["test_env_vars"] = _test_env_vars

    try:
        await github.move_card(card_id, "IN_PROGRESS")
        _dispatch_kwargs: dict = {"workspace_info": workspace_info}
        if _extra_volumes is not None:
            from coordinare.services.http_performer_service import HTTPPerformerService
            if isinstance(service, HTTPPerformerService):
                _dispatch_kwargs["extra_volumes"] = _extra_volumes
        result = await service.dispatch_card(card_context, **_dispatch_kwargs)
    except PerformerAuthError as exc:
        logger.error(
            "dispatch_performer.permanent_config_error",
            card_id=card_id,
            performer_stage=performer_stage,
            error=str(exc),
        )
        try:
            await github.move_card(card_id, "BLOCKED")
        except Exception as move_exc:
            logger.warning(
                "dispatch_performer.move_card_to_blocked_failed",
                card_id=card_id,
                error=str(move_exc),
            )
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning("workspace_teardown_failed.after_permanent_error", card_id=card_id)
        state["workspace_path"] = None
        state["workspace_branch"] = None
        _release_slot_on_error()
        state["phase"] = "blocked"
        state["open_questions"] = [f"Performer config error: {exc}"]
        return state
    except TransportError as exc:
        reason = f"Transport failure during dispatch: {type(exc).__name__}"
        logger.warning(
            "dispatch_performer.transport_error",
            card_id=card_id,
            performer_stage=performer_stage,
            exc_type=type(exc).__name__,
        )
        # Reset stale error state from a previous card so this card gets
        # its full retry budget.
        if state.get("system_error_notified"):
            state["system_error_count"] = 0
            state["system_error_notified"] = False
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = reason
        _release_slot_on_error()
        state["phase"] = "system_error"
        card["previous_status"] = card.get("status", "TODO")
        card["status"] = "IN_PROGRESS"
        _set_current_card(state, card)
        # Tear down workspace to avoid leaking temp directories.
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning(
                    "workspace_teardown_failed.after_transport_error",
                    card_id=card_id,
                )
        state["workspace_path"] = None
        state["workspace_branch"] = None
        return state
    except PermanentGitHubError as exc:
        logger.error(
            "permanent_service_failure.card_blocked",
            card_id=card_id,
            performer_stage=performer_stage,
            error=str(exc),
        )
        try:
            await github.move_card(card_id, "BLOCKED")
        except Exception as move_exc:
            logger.warning(
                "dispatch_performer.move_card_to_blocked_failed",
                card_id=card_id,
                error=str(move_exc),
            )
        # Tear down workspace to avoid leaking temp directories.
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning("workspace_teardown_failed.after_permanent_error", card_id=card_id)
        state["workspace_path"] = None
        state["workspace_branch"] = None
        _release_slot_on_error()
        state["phase"] = "blocked"
        state["open_questions"] = [f"Permanent service failure: {exc}"]
        return state

    if result.get("status") == "error":
        reason = str(result.get("reason", "Performer returned an error on dispatch."))
        logger.error(
            "dispatch_performer.performer_error",
            card_id=card_id,
            performer_stage=performer_stage,
            reason=reason,
        )
        # Tear down workspace to avoid leaking temp directories.
        if workspace_manager is not None and workspace_info is not None and workspace_info.path is not None:
            try:
                await workspace_manager.teardown(workspace_info.path)
            except Exception:
                logger.warning("workspace_teardown_failed.after_dispatch_error", card_id=card_id)
        state["workspace_path"] = None
        state["workspace_branch"] = None
        # Reset stale error state from a previous card.
        if state.get("system_error_notified"):
            state["system_error_count"] = 0
            state["system_error_notified"] = False
        state["system_error_count"] = state.get("system_error_count", 0) + 1
        state["system_error_last_at"] = datetime.now(UTC)
        state["system_error_reason"] = f"Performer dispatch failed ({performer_stage}): {reason}"
        _release_slot_on_error()
        state["phase"] = "system_error"
        return state

    # --- Success ---
    # 034: Reset token counters when dispatching the first role for a new card.
    lifecycle = list(state.get("lifecycle_sequence") or [])
    if lifecycle and performer_stage == lifecycle[0]:
        state["card_tokens_total"] = 0
        state["card_cost_estimate"] = 0.0
        state["card_budget_alert_sent"] = False
        from coordinare.metrics import METRICS
        METRICS.card_cost_estimate_dollars.set(0)

    # Clear relay_feedback so it isn't re-sent to subsequent roles.
    state["relay_feedback"] = []  # type: ignore[typeddict-unknown-key]
    state["agent_dispatch"] = result
    state["agent_dispatch_at"] = datetime.now(UTC)
    state["performer_events"] = []
    state["performer_metrics"] = None
    # 072 FR-072-5(c): snapshot the pre-turn clarifications count so the
    # per-role zero-progress guardrail can tell whether new clarifications
    # arrived during this performer turn (via route_issue_comments or
    # check_board).  A growth in this count is a "progress" signal.
    _clarifications = state.get("card_clarifications") or []
    state["clarifications_count_at_dispatch"] = len(_clarifications) if isinstance(_clarifications, list) else 0
    card["previous_status"] = card.get("status", "TODO")
    card["status"] = "IN_PROGRESS"
    _set_current_card(state, card)
    state["phase"] = "monitoring_performer"
    # 065 Fix 18: preserve the retry counter across a handle_system_error
    # re-dispatch on the same card+stage. The dispatch itself succeeding
    # doesn't mean the card made progress — if the model keeps returning
    # empty / unparseable output on the same stage, monitor_performer will
    # re-flip phase to `system_error` and handle_system_error needs the
    # count to accumulate so it can eventually escalate to BLOCKED.
    # Mid-retry signal: system_error_last_at is set (handle_system_error
    # keeps it; fresh check_board pickup has it None) AND notified is False
    # (notified=True means the prior card already exhausted its budget — that
    # is the genuine "stale from previous card" case, handled here as before).
    mid_retry = (
        state.get("system_error_last_at") is not None
        and not state.get("system_error_notified")
    )
    if not mid_retry:
        state["system_error_count"] = 0
        state["system_error_last_at"] = None
        state["system_error_notified"] = False
        state["system_error_reason"] = None
    return state
