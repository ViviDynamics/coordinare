"""L3 inherited-repair gate: mandate construction and adversarial review (435, 090)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.attribution import coordinare_attribution
from coordinare.services.failure_signature import normalize_reason
from coordinare.services.test_integrity_guard import analyze_diff

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.services.ci_gate import FailedCheckWithSignature
    from coordinare.services.pr_checks_service import CheckRollup


from coordinare.graph.nodes.monitor.gate_config import (
    _get_inherited_repair_gate_config,
)

logger = structlog.get_logger(__name__)

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
            },
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


async def _reject_repair(
    *,
    state: CoordinareState,
    head_sha: str,
    attempt: int,
    now_iso: str,
    audit: list[dict[str, Any]],
    detail: str,
    kind_seq: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Fail-safe rejection: audit the verdict, escalate, and block (no push)."""
    records = list(kind_seq)
    records.append(
        _repair_record(
            head_sha=head_sha,
            attempt=attempt,
            kind="rejection",
            now_iso=now_iso,
            detail=detail,
        ),
    )
    open_qs = list(state.get("open_questions") or [])
    open_qs.append(
        f"Autonomous baseline repair (attempt {attempt}) on head {head_sha[:12]} "
        f"was rejected by the test-integrity guard and NOT landed: {detail} "
        "A human must review and resolve the inherited base-branch failure.",
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


async def _adjudicate_reviewed_repair(
    *,
    state: CoordinareState,
    card_id: str,
    pending: dict[str, Any],
    head_sha: str,
    attempt: int,
    now_iso: str,
    audit: list[dict[str, Any]],
    static_record: dict[str, Any],
    diff: str,
) -> tuple[dict[str, Any], bool]:
    """(3) adversarial reviewer + acceptance — the static half already cleared."""
    try:
        reviewer_safe, reviewer_detail = await _dispatch_repair_reviewer(
            state=state, card_id=card_id, diff=diff or "", pending=pending,
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
        return await _reject_repair(
            state=state,
            head_sha=head_sha,
            attempt=attempt,
            now_iso=now_iso,
            audit=audit,
            detail=reviewer_detail,
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
        "repair_audit": [*audit, static_record, reviewer_record, acceptance_record],
    }, False


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

    # (1) Fetch the candidate diff. A failure here is uncertainty → fail safe,
    # and we reject BEFORE recording a static_guard record (analyze_diff never
    # ran).
    github = state.get("github_service")
    if github is None or not hasattr(github, "get_pr_diff") or not pr_url:
        return await _reject_repair(
            state=state,
            head_sha=head_sha,
            attempt=attempt,
            now_iso=now_iso,
            audit=audit,
            detail="Could not fetch the candidate repair diff to adjudicate it.",
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
        return await _reject_repair(
            state=state,
            head_sha=head_sha,
            attempt=attempt,
            now_iso=now_iso,
            audit=audit,
            detail="Could not fetch the candidate repair diff to adjudicate it.",
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
        return await _reject_repair(
            state=state,
            head_sha=head_sha,
            attempt=attempt,
            now_iso=now_iso,
            audit=audit,
            detail=(
                "The static test-integrity check flagged the diff as weakening tests "
                f"({', '.join(flagged)})."
            ),
            kind_seq=[static_record],
        )

    return await _adjudicate_reviewed_repair(
        state=state,
        card_id=card_id,
        pending=pending,
        head_sha=head_sha,
        attempt=attempt,
        now_iso=now_iso,
        audit=audit,
        static_record=static_record,
        diff=diff or "",
    )

