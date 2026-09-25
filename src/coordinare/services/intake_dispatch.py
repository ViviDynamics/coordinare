"""Spec 173: the per-cycle gate for the two card-less intake roles.

The advocate and the curator are dispatched the way the wiki-init run is:
straight from the daemon, with no card and no lifecycle stage. The DECISION of
whether to start one is separated here into pure functions over
``EnvCacheState`` so it can be tested without a daemon, a container or a clock.

The gate logic is identical for both roles and only the trigger differs, which
is why there is one module rather than two.

Three rules this module exists to hold, each learned the hard way elsewhere in
this codebase:

* The in-flight marker is set by the CALLER before dispatching and rolled back
  on a synchronous failure. ``should_run`` only reads it. Setting it after the
  dispatch clobbers the reset a synchronous failure performs and wedges the
  role forever (see ``EnvCacheService._do_dispatch``).
* The marker is transient and never persisted, so a crash mid-run cannot leave
  a role permanently blocked.
* A failed run waits longer than a healthy one. The daemon poll is roughly
  every thirty seconds and a webhook can shorten a cycle to nearly nothing, so
  "once per cycle" is not a rate limit at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    import asyncio

    from coordinare.models.env_cache import EnvCacheState

logger = structlog.get_logger(__name__)

IntakeRole = Literal["advocate", "curator"]

#: Cap on the escalating backoff exponent. Without it, a role that failed a
#: dozen times would compute a wait longer than the heat death of the cluster
#: and never retry even after an operator fixed the cause.
_MAX_BACKOFF_EXPONENT = 4


def _as_aware(moment: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC rather than raising.

    A snapshot written by an older build can carry a naive datetime, and a gate
    that raises on one would take the role down instead of merely mis-timing a
    single cooldown.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def should_run(
    state: EnvCacheState,
    role: IntakeRole,
    *,
    enabled: bool,
    interval_seconds: int,
    now: datetime | None = None,
) -> bool:
    """True when a run of *role* should be started for this symphony now.

    Pure: reads state, decides, and changes nothing. The caller marks the run
    in flight, because only the caller knows whether the dispatch was attempted.
    """
    if not enabled:
        return False
    if getattr(state, f"{role}_in_flight"):
        return False
    if getattr(state, f"{role}_exhausted"):
        return False

    last_run = _as_aware(getattr(state, f"last_{role}_run_at"))
    if last_run is None:
        return True

    attempts = int(getattr(state, f"{role}_attempts") or 0)
    backoff = 2 ** min(attempts, _MAX_BACKOFF_EXPONENT)
    wait_seconds = interval_seconds * backoff
    elapsed = ((now or datetime.now(UTC)) - last_run).total_seconds()
    return bool(elapsed >= wait_seconds)


def register_failure(
    state: EnvCacheState,
    role: IntakeRole,
    error: str,
    *,
    max_attempts: int,
    now: datetime | None = None,
) -> bool:
    """Record a failed run. Returns True when this failure exhausted the role.

    Exhaustion is deliberate and sticky: a role whose repository is misconfigured
    should stop burning containers and wait for a human, not retry forever.
    """
    attempts = int(getattr(state, f"{role}_attempts") or 0) + 1
    setattr(state, f"{role}_attempts", attempts)
    setattr(state, f"last_{role}_succeeded", False)
    setattr(state, f"last_{role}_error", error)
    setattr(state, f"last_{role}_run_at", now or datetime.now(UTC))
    exhausted = attempts >= max_attempts
    if exhausted:
        setattr(state, f"{role}_exhausted", True)
    logger.warning(
        "intake.run_failed",
        role=role,
        symphony=state.symphony_name,
        attempts=attempts,
        exhausted=exhausted,
        error=error,
    )
    return exhausted


def register_success(
    state: EnvCacheState,
    role: IntakeRole,
    *,
    issues_seen: int,
    now: datetime | None = None,
) -> None:
    """Record a successful run and clear the breaker."""
    setattr(state, f"{role}_attempts", 0)
    setattr(state, f"{role}_exhausted", False)
    setattr(state, f"last_{role}_succeeded", True)
    setattr(state, f"last_{role}_error", None)
    setattr(state, f"last_{role}_issues_seen", int(issues_seen))
    setattr(state, f"last_{role}_run_at", now or datetime.now(UTC))
    logger.info(
        "intake.run_succeeded",
        role=role,
        symphony=state.symphony_name,
        issues_seen=issues_seen,
    )

# ---------------------------------------------------------------------------
# Building a run
# ---------------------------------------------------------------------------


def build_workflow_env(role: IntakeRole, config: object) -> dict[str, str]:
    """The role's configuration, flattened into the workflow env channel.

    ``workflow_env`` takes scalars, so lists are JSON-encoded and parsed back by
    the workflow's own settings loader. This is the existing operator channel
    into a workflow rather than a new one, which keeps the dispatch payload
    contract unchanged apart from the board id.
    """
    import json

    def text(name: str, default: str = "") -> str:
        return str(getattr(config, name, default) or default)

    if role == "advocate":
        return {
            "ADVOCATE_HANDLED_LABEL": text("handled_label"),
            "ADVOCATE_ESCALATION_LABEL": text("escalation_label"),
            "ADVOCATE_CONFIDENCE_THRESHOLD": str(getattr(config, "confidence_threshold", 0.7)),
            "ADVOCATE_SENSITIVE_KEYWORDS": json.dumps(list(getattr(config, "sensitive_keywords", []))),
            "ADVOCATE_DOC_SOURCES": json.dumps(list(getattr(config, "doc_sources", []))),
            "ADVOCATE_SUPPORT_URL": text("support_channel_url"),
            "ADVOCATE_HOLDING_TEMPLATE": text("holding_comment_template"),
            "ADVOCATE_ACK_TEMPLATE": text("acknowledgement_template"),
            "ADVOCATE_REDIRECT_TEMPLATE": text("redirect_template"),
            "ADVOCATE_DISCLOSURE_TEMPLATE": text("disclosure_template"),
        }
    return {
        "CURATOR_LABEL": text("label"),
        "CURATOR_BACKLOG_COLUMN": text("backlog_column"),
        "CURATOR_CRITERIA": json.dumps(list(getattr(config, "criteria", []))),
        "CURATOR_MAX_PER_RUN": str(getattr(config, "max_per_run", 5)),
        "CURATOR_SKIPPED_LABEL": text("skipped_label"),
        "CURATOR_ESCALATION_LABEL": text("escalation_label"),
        "CURATOR_SENSITIVE_KEYWORDS": json.dumps(list(getattr(config, "sensitive_keywords", []))),
        "CURATOR_MAX_PER_CALL": str(getattr(config, "max_per_call", 10)),
    }


def build_card_context(
    role: IntakeRole,
    *,
    symphony_name: str,
    org: str,
    repo: str,
    persona: str,
    backend: str,
    model_block: dict[str, Any] | None = None,
    project_id: str = "",
    workflow_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The hand-built context for a card-less dispatch.

    The identifier key is ``id``. It is NOT ``card_id``: the transport reads
    ``card_context["id"]`` and nothing reads ``card_id``, so the wiki-init
    precedent's synthetic id has never reached the wire. Copying that key would
    have inherited the same silent drop.

    The branch is never pushed. A branch that does not exist remotely becomes a
    local checkout off the default branch, which is all a read-only run needs.
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in symphony_name).strip("-").lower()
    titles = {
        "advocate": "Answer inbound issues",
        "curator": "Propose ready work for the board",
    }
    descriptions = {
        "advocate": (
            "Classify the repository's open inbound issues and answer the ones "
            "the documentation covers, escalating everything else to a human."
        ),
        "curator": (
            "Judge the repository's open issues against the selection criteria "
            "and add the ready ones to the board's backlog for a human to promote."
        ),
    }
    context: dict[str, Any] = {
        "id": f"{role}-{safe or 'symphony'}",
        "role": role,
        "workflow": role,
        "repo_url": f"https://github.com/{org}/{repo}.git",
        "branch": f"{role}/{safe or 'symphony'}",
        "base_branch": "main",
        "title": titles[role],
        "description": descriptions[role],
        "persona_instructions": persona,
        "backend": backend,
        "workflow_env": dict(workflow_env or {}),
        **(model_block or {}),
    }
    if role == "curator":
        context["project_id"] = project_id
    return context

# ---------------------------------------------------------------------------
# Completing a run
# ---------------------------------------------------------------------------

#: Terminal statuses a run can report. Anything else at the end of the poll
#: budget is a failure, including a run still working.
#: Strong references to in-flight snapshot flushes, so the event loop cannot
#: collect one before it runs.
_PENDING_FLUSHES: set[asyncio.Task[None]] = set()

TERMINAL_BY_ROLE: dict[str, str] = {
    "advocate": "advocate_complete",
    "curator": "curation_complete",
}


def flush_snapshot(state: dict[str, Any]) -> bool:
    """Force the snapshot to disk after a card-less completion.

    A run that owns no card moves no lifecycle signature, and the daemon's save
    is gated on one, so an outcome recorded here would sit in memory until the
    next stage transition and be lost to a restart in that window. The
    bootstrap completion already carries this call and the comment explaining
    why; not copying it re-introduces a bug this repository has paid for.

    Returns True when a flush was scheduled.
    """
    import asyncio

    save_fn = state.get("snapshot_save_fn")
    if not callable(save_fn):
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("intake.snapshot_flush_skipped")
        return False
    # Hold a reference: a task with no strong reference can be collected
    # mid-flight, which would silently drop the very write this function exists
    # to guarantee.
    task = loop.create_task(save_fn())
    _PENDING_FLUSHES.add(task)
    task.add_done_callback(_PENDING_FLUSHES.discard)
    return True


def _record_metrics(role: IntakeRole, record: dict[str, Any], report: dict[str, Any]) -> None:
    """Feed the spec-007 advocate counters from the run's own record.

    Coordinare no longer performs the scan, so these numbers now arrive one hop
    away, on the report. Failing to record them must never fail a completion,
    hence the broad guard: a metrics registry problem is not a reason to lose
    the outcome of a run that already posted its comments.
    """
    if role != "advocate":
        return
    try:
        from coordinare.metrics import METRICS

        for outcome in record.get("outcomes") or []:
            if not isinstance(outcome, dict):
                continue
            action = str(outcome.get("action") or "")
            if action:
                METRICS.advocate_issues_processed_total.labels(action=action).inc()
            if action == "escalated":
                reason = str(outcome.get("escalation_reason") or "unknown")
                METRICS.advocate_issues_escalated_total.labels(reason=reason).inc()
        steps = ((report.get("workflow_metrics") or {}).get("step_durations_ms")) or {}
        total_ms = sum(int(v) for v in steps.values() if isinstance(v, (int, float)))
        if total_ms:
            METRICS.advocate_scan_duration_seconds.observe(total_ms / 1000.0)
    except Exception as exc:
        logger.debug("intake.metrics_failed", role=role, error=str(exc))


def handle_run_result(
    cache_state: EnvCacheState,
    role: IntakeRole,
    status: dict[str, Any] | None,
    daemon_state: dict[str, Any],
    *,
    max_attempts: int = 3,
    now: datetime | None = None,
) -> bool:
    """Record a finished run, clear the in-flight marker, flush the snapshot.

    Returns True when the run succeeded. The marker is cleared on EVERY path,
    including an unrecognised status, because a marker left set is a role that
    never runs again until the process restarts.
    """
    setattr(cache_state, f"{role}_in_flight", False)

    reported = str((status or {}).get("status") or (status or {}).get("state") or "")
    succeeded = reported == TERMINAL_BY_ROLE[role]

    if succeeded:
        report = (status or {}).get("report") or {}
        key = "advocate" if role == "advocate" else "curation"
        record = report.get(key) if isinstance(report, dict) else {}
        seen = int((record or {}).get("issues_seen") or (record or {}).get("candidates_seen") or 0)
        register_success(cache_state, role, issues_seen=seen, now=now)
        _record_metrics(role, record or {}, report if isinstance(report, dict) else {})
    else:
        reason = (
            (status or {}).get("reason")
            or (status or {}).get("error")
            or reported
            or "the run reported no terminal status"
        )
        register_failure(cache_state, role, str(reason), max_attempts=max_attempts, now=now)

    flush_snapshot(daemon_state)
    return succeeded

