"""Gate evaluators: PR checks, CI gate, env-blocked hold, baseline prevention (435)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.metrics import METRICS
from coordinare.services.base_gate import evaluate_base_gate
from coordinare.services.ci_gate import CIGateDecision, FailedCheck
from coordinare.services.env_signature import match_env_signature
from coordinare.services.failure_signature import normalize_reason
from coordinare.services.pr_checks_policy import decide
from coordinare.services.pr_checks_service import PrChecksService
from coordinare.services.required_checks_resolver import resolve

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
    from coordinare.services.required_checks_resolver import RequiredChecksList

from coordinare.graph.nodes.monitor.artefacts import (
    _parse_job_id_from_details_url,
    _pr_url_parts,
)
from coordinare.graph.nodes.monitor.baseline import (
    _classify_head_failures,
    _get_persona_check_map,
    _get_session_persona_scope,
)
from coordinare.graph.nodes.monitor.gate_config import (
    _get_baseline_prevention_gate_config,
    _get_ci_gate_config,
    _get_closer_pr_checks_config,
    _get_env_blocked_gate_config,
)
from coordinare.graph.nodes.monitor.repair import (
    _build_repair_mandate,
    _l3_budget_exhausted,
    _post_repair_comment,
    _repair_record,
)
from coordinare.graph.nodes.monitor.verdict import _stamp_feedback_bounce

logger = structlog.get_logger(__name__)


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
                    "pr_checks_gate.fast_path_reuse", pr=pr_num, age=round(age, 1),
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
                "pr_checks_gate.fail_open_on_error", pr=pr_num, error=str(exc),
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
                    },
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
                        owner, repo, job_id, max_chars=slice_size,
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
                    f"**Log tail (job {job_id}, check {name})**:\n```\n{tail}\n```",
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
    resolved: RequiredChecksList,
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
            github, "_pr_checks_service_cache", None,
        ) or {}
        if not hasattr(github, "_pr_checks_service_cache"):
            github._pr_checks_service_cache = _svc_cache  # type: ignore[attr-defined]  # duck-typed lazy attach (state.py:41)
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
                ),
            )
            open_questions = list(state.get("open_questions") or [])
            open_questions.append(reason)
            await _post_repair_comment(
                state, f"🤖 **Baseline repair budget exhausted.** {reason}",
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
                ),
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
            github, "_pr_checks_service_cache", None,
        ) or {}
        if not hasattr(github, "_pr_checks_service_cache"):
            github._pr_checks_service_cache = _svc_cache  # type: ignore[attr-defined]  # duck-typed lazy attach (state.py:41)
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
            base_rollup, scope, persona_check_map=persona_check_map,
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

