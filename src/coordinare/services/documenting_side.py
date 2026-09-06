"""The documenter side run (spec 165 FR-014, FR-015, research R3).

The lifecycle is strictly linear: one stage, one dispatch per card. The
documenter therefore does not run as a stage. It runs beside the lifecycle,
the way env-bootstrap and wiki-init do: dispatched once per blueprint hash as
soon as the card has moved past architecting and the blueprint's
documentation brief is non-empty, polled to a terminal state, and recorded on
the session. It never blocks or advances the main lifecycle, and a failed run
is recorded, not retried; the spec-125 end-of-lifecycle documenting pass still
runs unchanged.

Pure decision functions plus one poll coroutine with the performer service
injected, so every rule is a unit test.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

import structlog

from coordinare.graph.nodes.dispatch_performer import (
    _DOCUMENTATION_BRIEF_FIELDS,
    documenter_side_run_wanted,
    project_brief,
)

logger = structlog.get_logger(__name__)

_STAGES_BEFORE_IMPLEMENTATION: frozenset[str] = frozenset({"assessing", "architecting"})
# A card that is blocked, idle or finished has no live lifecycle to run beside.
_INACTIVE_PHASES: frozenset[str] = frozenset({"blocked", "idle", "done", "completed", "closed", "merging", "system_error"})
_TERMINAL_OK: frozenset[str] = frozenset({"docs_committed", "done", "pr_opened", "approved"})
_TERMINAL_FAIL: frozenset[str] = frozenset({"error", "blocked", "session_expired", "token_limit", "changes_requested"})
DEFAULT_POLL_INTERVAL_S = 30.0
DEFAULT_POLL_TIMEOUT_S = 3600.0


def should_dispatch(session: dict[str, Any]) -> tuple[bool, str]:
    """Return ``(dispatch, reason)`` for one card session."""
    blueprint = session.get("blueprint")
    if not documenter_side_run_wanted(blueprint):
        return False, "no documentation brief"
    phase = str(session.get("phase") or "")
    if phase in _INACTIVE_PHASES:
        return False, f"card is {phase}"
    stage = str(session.get("performer_stage") or "")
    if stage in _STAGES_BEFORE_IMPLEMENTATION:
        return False, f"card still at {stage}"
    side = session.get("documenting_side")
    bp_hash = str(blueprint.get("blueprint_hash") or "")
    if isinstance(side, dict) and side.get("blueprint_hash") == bp_hash:
        return False, f"already {side.get('status', 'recorded')} for this blueprint"
    return True, "documentation brief present, not yet dispatched for this blueprint"


def build_card_context(
    card: dict[str, Any],
    session: dict[str, Any],
    *,
    persona: str,
    backend: str,
    model_block: dict[str, Any],
    repo_url: str,
    base_branch: str,
) -> dict[str, Any]:
    """The documenter's dispatch payload: its brief and nothing else of the plan."""
    blueprint = session["blueprint"]
    return {
        "id": str(card.get("id", "")),
        "card_id": str(card.get("id", "")),
        "role": "documenting",
        "doc_mode": "blueprint",
        "title": str(card.get("title", "")),
        "description": str(card.get("description") or card.get("body") or ""),
        "repo_url": repo_url,
        "branch": str(session.get("workspace_branch") or card.get("branch") or ""),
        "base_branch": base_branch,
        "persona_instructions": persona,
        "backend": backend,
        "documentation_brief": project_brief(blueprint, _DOCUMENTATION_BRIEF_FIELDS),
        "issue_number": card.get("issue_number"),
        **model_block,
    }


def record_dispatched(session: dict[str, Any], *, blueprint_hash: str, session_id: str) -> None:
    session["documenting_side"] = {
        "status": "running",
        "blueprint_hash": blueprint_hash,
        "dispatched_at": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "head_sha": None,
        "result_reason": None,
    }
    logger.info("documenting_side.dispatched", card_id=session.get("card_id"), blueprint_hash=blueprint_hash, session_id=session_id)


def record_result(session: dict[str, Any], *, status: str, head_sha: str | None, reason: str | None) -> None:
    side = dict(session.get("documenting_side") or {})
    side.update({"status": status, "head_sha": head_sha, "result_reason": reason})
    session["documenting_side"] = side
    logger.info("documenting_side.%s" % ("completed" if status == "done" else "failed"),
                card_id=session.get("card_id"), blueprint_hash=side.get("blueprint_hash"), head_sha=head_sha, reason=reason)


def classify_status(status: dict[str, Any] | None) -> str | None:
    """'done', 'failed', or None while the run is still going."""
    marker = str((status or {}).get("status") or "")
    if marker in _TERMINAL_OK:
        return "done"
    if marker in _TERMINAL_FAIL:
        return "failed"
    return None


async def poll_to_completion(
    session: dict[str, Any],
    check_status: Callable[[str], Awaitable[dict[str, Any]]],
    *,
    session_id: str,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    timeout_s: float = DEFAULT_POLL_TIMEOUT_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] | None = None,
) -> str:
    """Poll the documenter job until terminal and record the outcome.

    Returns the recorded status. A poll error or the timeout records
    ``failed``; nothing here ever raises into the daemon loop.
    """
    clock = now or (lambda: asyncio.get_event_loop().time())
    deadline = clock() + timeout_s
    while True:
        try:
            status = await check_status(session_id)
        except Exception as exc:
            record_result(session, status="failed", head_sha=None, reason=f"poll error: {exc}"[:300])
            return "failed"
        outcome = classify_status(status)
        if outcome == "done":
            record_result(session, status="done", head_sha=(status or {}).get("head_sha"), reason=None)
            return "done"
        if outcome == "failed":
            record_result(session, status="failed", head_sha=None, reason=str((status or {}).get("reason") or (status or {}).get("status"))[:300])
            return "failed"
        if clock() >= deadline:
            record_result(session, status="failed", head_sha=None, reason="documenter side run timed out")
            return "failed"
        await sleep(interval_s)


async def run_cycle(
    sessions: dict[str, dict[str, Any]],
    *,
    svc: Any,
    resolve: Callable[[str, dict[str, Any]], Awaitable[tuple[dict[str, Any], Any] | None]],
    spawn: Callable[[Awaitable[str]], Any],
) -> int:
    """One daemon cycle: dispatch every side run that should run; return the count.

    *resolve* turns ``(card_id, session)`` into ``(card_context, workspace_info)``
    or ``None`` when the daemon cannot (no repo, no token); *spawn* schedules
    the completion poll (``asyncio.create_task`` in the daemon, awaited inline
    in tests). A dispatch failure is recorded as a failed run for this
    blueprint hash so it is not retried every cycle.
    """
    dispatched = 0
    for card_id, session in list(sessions.items()):
        ok, _reason = should_dispatch(session)
        if not ok:
            continue
        bp_hash = str((session.get("blueprint") or {}).get("blueprint_hash") or "")
        try:
            resolved = await resolve(card_id, session)
        except Exception as exc:
            logger.warning("documenting_side.resolve_failed", card_id=card_id, error=str(exc)[:200])
            continue
        if resolved is None:
            continue
        card_context, workspace_info = resolved
        try:
            result = await svc.dispatch_card(card_context, workspace_info=workspace_info)
        except Exception as exc:
            record_dispatched(session, blueprint_hash=bp_hash, session_id="")
            record_result(session, status="failed", head_sha=None, reason=f"dispatch failed: {exc}"[:300])
            continue
        session_id = str((result or {}).get("session_id") or (result or {}).get("job_id") or "")
        if not session_id:
            record_dispatched(session, blueprint_hash=bp_hash, session_id="")
            record_result(session, status="failed", head_sha=None, reason="dispatch returned no session id")
            continue
        record_dispatched(session, blueprint_hash=bp_hash, session_id=session_id)
        spawn(poll_to_completion(session, svc.check_status, session_id=session_id))
        dispatched += 1
    return dispatched
