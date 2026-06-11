from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from coordinare.graph.attribution import coordinare_attribution
from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)
from coordinare.graph.state import _rederive_current_card, _retire_active_session
from coordinare.lib.acceptance_criteria import parse_acceptance_criteria
from coordinare.models.dependency import DependencyStatus
from coordinare.services.dependency import build_graph, resolve_off_board_dependencies
from coordinare.services.dependency import filter_eligible_todo as _dep_filter
from coordinare.services.rebase import repo_url_from_config
from coordinare.session import create_session_from_card, session_to_state, state_to_session

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def _snapshot_stage_for_card(state: dict, card_id: str) -> str | None:
    """Return the snapshot-restored performer_stage for ``card_id`` if any.

    065 Fix 7a/7b: a freshly-rehydrated active_sessions entry (v2 snapshot
    restore in daemon._restore_from_snapshot) carries the durable stage.  For
    v1 snapshots — single-card persistence — the only stage available is the
    top-level ``performer_stage`` field, which we honour only when the card
    being re-adopted matches the restored ``current_card``.
    """
    sessions = state.get("active_sessions") or {}
    sess = sessions.get(card_id) if isinstance(sessions, dict) else None
    if isinstance(sess, dict):
        stage = sess.get("performer_stage")
        if isinstance(stage, str) and stage:
            return stage
    current = state.get("current_card")
    if isinstance(current, dict) and str(current.get("id", "")) == card_id:
        top_stage = state.get("performer_stage")
        if isinstance(top_stage, str) and top_stage:
            return top_stage
    return None


# 076 (live QA #150): per-stage output artifacts written to the card's
# ``docs/cards/<id>-<slug>/`` folder on its branch.  A stage is "complete" only
# when ALL its listed artifacts exist.  Stages absent from this map (e.g.
# ``implementing``, ``reviewing``) have no doc artifact — they're treated as the
# resume point once every earlier doc-producing stage is complete.  Mirrors the
# performer's ``_doc_folder`` naming (agent/performer/.../main.py:_doc_folder).
_STAGE_OUTPUT_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "assessing": ("assessment.md",),
    "architecting": ("plan.md", "tasks.md"),
}


def _card_docs_dir(card: dict) -> str | None:
    """Return ``docs/cards/<issue>-<slug>`` for *card*, matching the performer's
    ``_doc_folder`` slug algorithm (lowercased, non-alnum→dash, truncated 20)."""
    import re

    title = card.get("title")
    if not isinstance(title, str) or not title:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:20] or "untitled"
    issue_num = card.get("issue_number")
    if issue_num:
        return f"docs/cards/{issue_num}-{slug}"
    return f"docs/cards/{slug}"


async def _derive_resume_stage(github: object, card: dict, lifecycle_seq: list[str]) -> str | None:
    """076 (live QA #150): derive the stage to resume a re-adopted IN_PROGRESS
    card at, by probing its branch for completed-stage artifacts.

    Returns the EARLIEST lifecycle stage whose required output artifact(s) are
    missing — i.e. the earliest incomplete stage — so a card whose assessor or
    architect never actually finished resumes there instead of blindly jumping
    to ``implementing`` (the ``create_session_from_card`` default) and running
    the implementer with no plan.

    Returns ``None`` when the stage can't be determined (no github client,
    missing owner/repo, degenerate title, or any GitHub error); the caller then
    keeps its existing default.  Best-effort and fully fail-safe: this runs only
    on the rare re-adopt-without-snapshot-stage path, never the hot restart path.
    """
    if github is None or not lifecycle_seq:
        return None
    try:
        from coordinare.services.dispatch_guard import canonical_branch_name

        owner = getattr(github, "_org", None)
        repo = getattr(github, "_project_name", None)
        docs_dir = _card_docs_dir(card)
        if not owner or not repo or not docs_dir:
            return None
        branch = canonical_branch_name(card).full_name
        for stage in lifecycle_seq:
            artifacts = _STAGE_OUTPUT_ARTIFACTS.get(stage)
            if artifacts is None:
                # First stage with no doc contract (implementing+): every
                # earlier doc-producing stage is complete, so resume here.
                return stage
            for fname in artifacts:
                sha = await github.get_file_blob_sha(  # type: ignore[attr-defined]
                    owner, repo, f"{docs_dir}/{fname}", ref=branch
                )
                if not sha:
                    return stage  # this stage's output is missing — resume here
        return lifecycle_seq[-1]
    except Exception as exc:
        logger.warning("check_board.resume_stage_derivation_failed", error=str(exc))
        return None


# Phases where coordinare is passively waiting for an external actor (human reviewer,
# CI system) and the session is not consuming any active worker capacity.
PASSIVE_PHASES: frozenset[str] = frozenset({"monitoring_pr"})

# Phases that do not occupy a concurrency slot.  Blocked cards are waiting on a
# human answer and have no live performer running, so they shouldn't prevent
# the coordinare from picking up additional TODO work up to ``max_concurrent_cards``.
# Kept separate from PASSIVE_PHASES because the daemon clears ``current_card``
# whenever the fallback cycle settles into PASSIVE_PHASES — that's correct for
# ``monitoring_pr`` but would disrupt blocked-card answer detection.
NON_SLOT_PHASES: frozenset[str] = PASSIVE_PHASES | frozenset({"blocked"})


def _count_slot_consuming_sessions(state: dict) -> int:
    """Count active sessions occupying a concurrency slot."""
    sessions: dict = state.get("active_sessions") or {}
    return sum(
        1 for sess in sessions.values()
        if sess.get("phase") not in NON_SLOT_PHASES
    )


# 069: phases the per-session invocation must keep running on the next cycle.
# When check_board is invoked for an existing IN_PROGRESS card whose session
# is mid-flight, the routing layer dispatches monitor_performer / dispatcher /
# handle_blocked based on this phase — clobbering it to "idle" terminates
# monitoring entirely.
_IN_FLIGHT_WORKING_PHASES: frozenset[str] = frozenset(
    {"monitoring_performer", "dispatching", "blocked"}
)


def _has_in_flight_working_session(state: dict) -> bool:
    """Whether ``active_card_id`` points at a session in an in-flight working phase.

    Used by ``check_board`` to skip the end-of-cycle retire-and-go-idle path
    when the per-session invocation that triggered this cycle is still doing
    real work (multi-card primary IN_PROGRESS session falling through to TODO
    pickup with an empty TODO column).
    """
    active_id = state.get("active_card_id")
    sessions = state.get("active_sessions") or {}
    if not active_id or not isinstance(sessions, dict):
        return False
    sess = sessions.get(active_id)
    if not isinstance(sess, dict):
        return False
    return sess.get("phase") in _IN_FLIGHT_WORKING_PHASES


def _allowed_github_host(config: object) -> str | None:
    """Return the configured GitHub hostname for issue_url allowlisting.

    Reads ``config.github_endpoint`` if present (e.g. GHE), otherwise falls
    back to ``github.com``.  Returns lowercased hostname or None on parse
    failure (caller should reject the URL).
    """
    from urllib.parse import urlparse

    raw = getattr(config, "github_endpoint", None) if config is not None else None
    if not raw:
        return "github.com"
    try:
        parsed = urlparse(str(raw))
        host = (parsed.hostname or "").lower()
        # api.github.com → github.com for HTML issue URLs
        if host == "api.github.com":
            return "github.com"
        return host or "github.com"
    except Exception:
        return "github.com"


def _safe_issue_url(raw_url: str, allowed_host: str | None) -> str | None:
    """Return ``raw_url`` only if its hostname matches the allowlist.

    Defense-in-depth: ``issue_url`` flows from GitHub API responses (trusted)
    into the dashboard's JS innerHTML path.  Reject anything that doesn't
    parse as http(s) to the configured GitHub host so a corrupted snapshot
    can't smuggle ``javascript:`` or arbitrary domains into the UI.
    """
    if not raw_url or not allowed_host:
        return None
    from urllib.parse import urlparse

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host != allowed_host:
        return None
    return raw_url


def _dep_announcement_signature(item_id: str, reason: str, blockers: list[int]) -> str:
    """Canonical signature so we re-announce only when the blocker set changes."""
    return f"{item_id}|{reason}|" + ",".join(str(n) for n in sorted(blockers))


def _sort_by_priority(
    item_ids: list[str],
    item_field_values: dict[str, dict[str, str]],
    field_name: str,
    priority_order: list[str],
) -> list[str]:
    """Sort *item_ids* by their priority field value.

    - When *priority_order* is set, values rank by list index (lower = higher priority).
      Values not in *priority_order* sort after all listed values.
    - When *priority_order* is empty, values are compared lexicographically (ascending).
    - Items without a priority value (None/missing) sort after items that have one.
    - Stable sort: ties preserve the original board position order.
    """
    order_map = {v: i for i, v in enumerate(priority_order)} if priority_order else {}
    max_rank = len(priority_order)  # rank for unlisted values

    def _sort_key(item_id: str) -> tuple[int, int | str]:
        fields = item_field_values.get(item_id, {})
        value = fields.get(field_name)
        if value is None:
            # No value → sort last (after everything)
            return (1, "")
        if order_map:
            return (0, order_map.get(value, max_rank))
        return (0, value)

    return sorted(item_ids, key=_sort_key)


def _build_card_dict(item: str, board: dict, status: str) -> dict:
    """066 T013/FR-002: unified card_dict builder for the pickup paths.

    Returns a fresh card dict keyed off the per-cycle board snapshot.  Status
    is parameterised so the same helper serves TODO, IN_PROGRESS, and IN_REVIEW
    re-adopts; `previous_status` mirrors `status` because re-adopting a card
    from the board always treats the visible column as the canonical state.
    """
    descriptions = board.get("descriptions", {})
    description = str(descriptions.get(item, ""))
    return {
        "id": item,
        "issue_id": str(board.get("content_node_ids", {}).get(item, "")),
        "issue_number": int(board.get("issue_numbers", {}).get(item, 0)),
        "issue_url": str(board.get("issue_urls", {}).get(item, "")),
        "title": str(board.get("titles", {}).get(item, "")),
        "description": description,
        "acceptance_criteria": parse_acceptance_criteria(description),
        "status": status,
        "previous_status": status,
    }


def _ensure_active_card_id(state: CoordinareState) -> None:
    """066 FR-002 post-impl bootstrap: guarantee ``active_card_id`` points at
    a real session whenever any session exists.

    Cases handled (no-op if ``active_card_id`` is already set):
      - per-session invocation arrived with flat ``current_card`` matching a
        session entry — adopt its id.
      - otherwise promote an arbitrary remaining session (insertion order)
        and mirror its fields onto flat state so downstream nodes that still
        read flat fields see freshly-initialized per-card values, regardless
        of which column branch (TODO / IN_PROGRESS / IN_REVIEW / BLOCKED)
        created the session.
    """
    sessions = state.get("active_sessions") or {}
    if state.get("active_card_id") or not isinstance(sessions, dict) or not sessions:
        return
    flat = state.get("current_card") if isinstance(state.get("current_card"), dict) else None
    flat_id = str(flat.get("id", "")) if isinstance(flat, dict) else ""
    if flat_id and flat_id in sessions:
        state["active_card_id"] = flat_id
        return
    first_id = next(iter(sessions.keys()))
    state["active_card_id"] = first_id
    session_to_state(sessions[first_id], state)


def _finalize_active_card(state: CoordinareState) -> None:
    """066 FR-010: enforce the post-condition contract of ``check_board``.

    Ordering matters and is subtle:
      1. ``_ensure_active_card_id`` may *promote* a session and call
         ``session_to_state`` to mirror its fields onto flat state — this is
         what newly-picked-up sessions rely on so downstream nodes that still
         read flat ``current_card`` / ``performer_stage`` etc. see fresh values.
      2. ``_rederive_current_card`` then re-binds ``state["current_card"]`` to
         the session's ``current_card`` slot (dict identity, not copy) so any
         subsequent in-session mutation propagates to the mirror automatically.

    Inverting these steps would either leave the mirror pointing at a stale
    dict (no rederive after promotion) or write back over freshly-mirrored
    flat fields (rederive before promotion).  Always call both, in this order,
    and only at the end of ``check_board``.
    """
    _ensure_active_card_id(state)
    _rederive_current_card(state)


async def check_board(state: CoordinareState) -> CoordinareState:
    """Public entry point.  Always re-derives the current_card mirror on exit
    so the I3 invariant (FR-010) holds regardless of which internal branch ran.
    """
    # 066 FR-003 pre-impl adoption.  When a single-card caller invokes
    # check_board with a flat BLOCKED current_card and no active_sessions
    # (the legacy un-block path), mirror it into a session so the unified
    # un-block feedback_cycle reset inside _check_board_impl picks it up.
    _flat_cur_pre = state.get("current_card") if isinstance(state.get("current_card"), dict) else None
    _flat_id_pre = str(_flat_cur_pre.get("id", "")) if isinstance(_flat_cur_pre, dict) else ""
    _status_pre = str(_flat_cur_pre.get("status", "")) if isinstance(_flat_cur_pre, dict) else ""
    _sessions_pre = state.get("active_sessions") or {}
    if _flat_id_pre and _status_pre == "BLOCKED" and _flat_id_pre not in _sessions_pre:
        _sess_pre = state_to_session(state)  # type: ignore[arg-type]
        _sess_pre.setdefault("current_card", _flat_cur_pre)
        _sess_pre["phase"] = "blocked"
        _sessions_pre = dict(_sessions_pre)
        _sessions_pre[_flat_id_pre] = _sess_pre
        state["active_sessions"] = _sessions_pre
        if not state.get("active_card_id"):
            state["active_card_id"] = _flat_id_pre
    result = await _check_board_impl(state)
    _finalize_active_card(result)
    # 066 T020 / FR-004: single signal that the unified pickup path ran.  Read
    # off active_sessions/active_card_id (post-derivation) so the event reflects
    # the post-condition state, not pre-impl scratch.
    _sessions = result.get("active_sessions") or {}
    _config = result.get("config")
    _raw_max = getattr(_config, "max_concurrent_cards", 1) if _config else 1
    logger.info(
        "check_board.unified_pickup",
        active_card_id=result.get("active_card_id"),
        active_session_count=len(_sessions) if isinstance(_sessions, dict) else 0,
        phase=result.get("phase"),
        max_concurrent_cards=_raw_max if isinstance(_raw_max, int) else 1,
    )
    return result


async def _check_board_impl(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    logger.info(
        "check_board.entered",
        github_present=github is not None,
        github_type=type(github).__name__ if github is not None else None,
        project_id=getattr(github, "project_id", None) if github is not None else None,
        active_sessions=len(state.get("active_sessions") or {}),
        current_symphony=state.get("current_symphony"),
    )
    if github is None:
        logger.warning("check_board.no_github_service")
        state["phase"] = "idle"
        return state

    # In multi-session mode the daemon invokes the graph once per active
    # session within a single cycle.  Cache the board result to avoid
    # redundant GitHub polls.  Only used when max_concurrent_cards > 1;
    # single-card mode always polls fresh to avoid stale cache issues.
    config = state.get("config")
    _raw_max = getattr(config, "max_concurrent_cards", 1) if config else 1
    max_cards = _raw_max if isinstance(_raw_max, int) else 1
    cached_board = state.get("_board_cache") if max_cards > 1 else None
    if cached_board:
        board = cached_board
    else:
        ready, retry_in = github_operation_ready(state, "poll_board")
        if not ready:
            logger.info(
                "check_board.poll_deferred_wait",
                retry_in_seconds=round(retry_in, 1),
            )
            # Keep running without crashing; a later cycle will retry.
            return state
        try:
            board = await github.poll_board()
            clear_deferred_github_operation(state, "poll_board")
        except Exception as exc:
            if is_transient_github_outage_error(exc):
                deferred = defer_github_operation(
                    state,
                    operation="poll_board",
                    error=exc,
                    base_delay_seconds=30.0,
                    max_delay_seconds=600.0,
                )
                logger.warning(
                    "check_board.poll_deferred",
                    error=str(exc),
                    attempt=deferred.get("attempt"),
                    retry_at=deferred.get("retry_at"),
                )
                # Do not drop active in-flight state on transient GitHub outages.
                return state
            logger.error("check_board.poll_failed", error=str(exc))
            state["phase"] = "idle"
            return state
        if max_cards > 1:
            state["_board_cache"] = board
        state["last_poll_at"] = datetime.now(UTC)

    snapshot = board.get("snapshot")
    state["board_snapshot"] = snapshot if isinstance(snapshot, dict) else {}
    # 046: Stash titles and issue_numbers so assess_card can inject active-card
    # context into the assessor's prompt for implicit dependency detection.
    state["_board_titles"] = board.get("titles", {})
    state["_board_issue_numbers"] = board.get("issue_numbers", {})
    state["_board_issue_urls"] = board.get("issue_urls", {})
    state["_board_pr_urls"] = board.get("pr_urls", {})

    # 047: Detect external merges (non-coordinare PRs merged by humans) by
    # comparing last_known_main_sha against the current main HEAD.  If
    # changed, trigger a rebase round for all active sessions.
    active_sessions = state.get("active_sessions") or {}
    if active_sessions and config is not None:
        # Derive repo URL from config (not WorkspaceManager internals)
        repo_url = repo_url_from_config(config)
        # Get token from github_service (which owns the auth)
        token = ""
        if github is not None and hasattr(github, "_current_token"):
            import contextlib
            with contextlib.suppress(Exception):
                token = await github._current_token()
        if repo_url and token:
            from coordinare.services.rebase import fetch_main_sha, run_rebase_round
            # Cache the fetched main SHA per cycle to avoid N ls-remote
            # calls in multi-session mode (check_board runs once per
            # active session within a single daemon cycle).
            cached_main = state.get("_main_sha_cache")
            if cached_main and max_cards > 1:
                current_main = cached_main
            else:
                current_main = await fetch_main_sha(repo_url, token)
                if current_main and max_cards > 1:
                    state["_main_sha_cache"] = current_main  # type: ignore[typeddict-unknown-key]
            if current_main:
                prev_main = state.get("last_known_main_sha")
                if prev_main is None:
                    # First cycle — initialize without triggering rebase
                    state["last_known_main_sha"] = current_main  # type: ignore[typeddict-unknown-key]
                elif current_main != prev_main:
                    logger.info(
                        "check_board.main_head_changed",
                        old_sha=prev_main[:8] if prev_main else "?",
                        new_sha=current_main[:8],
                    )
                    state["last_known_main_sha"] = current_main  # type: ignore[typeddict-unknown-key]
                    try:
                        notification_svc = state.get("notification_service")
                        rr = await run_rebase_round(
                            active_sessions, current_main, repo_url, token,
                            notification_service=notification_svc,
                            github=github,
                            human_reviewers=state.get("human_reviewers"),
                        )
                        state["last_rebase_round"] = rr.to_dict()  # type: ignore[typeddict-unknown-key]
                        # US2: attempt performer conflict resolution for
                        # the first BLOCKED job (same as merge_pr path).
                        from coordinare.models.rebase import RebaseOutcome
                        from coordinare.services.rebase import prepare_conflict_resolution
                        for _job in rr.jobs:
                            if _job.outcome == RebaseOutcome.BLOCKED:
                                _sess = active_sessions.get(_job.card_id)
                                if isinstance(_sess, dict):
                                    prepare_conflict_resolution(_job, _sess, human_reviewers=state.get("human_reviewers"))
                                break
                    except Exception as exc:
                        logger.warning("check_board.rebase_round_failed", error=str(exc))

    # 045: Refresh current_card metadata from the fresh board snapshot whenever
    # we have an active card.  Without this, fields that aren't persisted in
    # WorkflowSnapshot (or that change between restarts — title edits, label
    # tweaks) stay stale.  The concrete failure this repairs: after a restart
    # the restore path used to rebuild current_card with issue_number=0, so the
    # next dispatch opened a PR whose body lacked ``Closes #N``.  Refreshing
    # here also keeps title/description in sync when the human edits the issue
    # mid-flight.  Skip when the card is mid-cancellation (``active_card
    # disappeared`` handler below has its own logic).
    active_card = state.get("current_card")
    if isinstance(active_card, dict):
        active_id = str(active_card.get("id", ""))
        if active_id:
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            issue_urls = board.get("issue_urls", {})
            content_node_ids = board.get("content_node_ids", {})
            if active_id in titles or active_id in issue_numbers:
                refreshed_description = str(descriptions.get(active_id, active_card.get("description", "")))
                active_card["title"] = str(titles.get(active_id, active_card.get("title", "")))
                active_card["description"] = refreshed_description
                active_card["acceptance_criteria"] = parse_acceptance_criteria(refreshed_description)
                fresh_number = int(issue_numbers.get(active_id, 0))
                if fresh_number:
                    active_card["issue_number"] = fresh_number
                fresh_url = str(issue_urls.get(active_id, ""))
                if fresh_url:
                    active_card["issue_url"] = fresh_url
                fresh_content_id = str(content_node_ids.get(active_id, ""))
                if fresh_content_id:
                    active_card["issue_id"] = fresh_content_id

    # 065 Fix 22: re-dispatch stale monitor sessions after restart.  When the
    # daemon restarts mid-flight, snapshot restore brings back the previous
    # session_id and phase=monitoring_performer/monitoring_agent, but the
    # ephemeral container is gone — _active_jobs is empty in the new process,
    # so the next monitor_performer cycle raises TransportError ("no endpoint
    # resolved yet" / "no live session") and burns retry budget for nothing.
    # Detect that case here and route back through dispatching for a fresh
    # container.  Do NOT touch system_error_count: this is a clean restart
    # re-dispatch, not a retry of a real failure.
    #
    # 076 (T057, FR-008): previously this block unconditionally cleared
    # agent_dispatch and routed back to dispatching whenever
    # has_live_session returned False — that is the exact trigger
    # sequence that produced today's duplicate-dispatch incident.  Now
    # we route through reconciliation.handle_potentially_stale_session
    # which attempts adopt → reap+replace → fresh as a triage,
    # preserving in-flight container work where possible.
    _stale_phases = ("monitoring_performer", "monitoring_agent")
    _performer_services = state.get("performer_services") or {}
    if isinstance(_performer_services, dict) and _performer_services:
        def _is_stale(_phase: str, _stage: str, _dispatch: object) -> bool:
            if _phase not in _stale_phases:
                return False
            if not isinstance(_dispatch, dict):
                return False
            _sid = _dispatch.get("session_id")
            if not _sid:
                return False
            _svc = _performer_services.get(_stage)
            _check = getattr(_svc, "has_live_session", None) if _svc is not None else None
            if _check is None:
                return False
            try:
                return not bool(_check(str(_sid)))
            except Exception:
                return False

        # Top-level "stale current session" check.  Lazy import to avoid
        # circulars (reconciliation imports docker_executor + http stack).
        if _is_stale(
            str(state.get("phase") or ""),
            str(state.get("performer_stage") or ""),
            state.get("agent_dispatch"),
        ):
            _card = state.get("current_card") or {}
            _top_card_id = str(_card.get("id", "")) if isinstance(_card, dict) else ""
            if _top_card_id:
                from coordinare.services.reconciliation import handle_potentially_stale_session
                await handle_potentially_stale_session(state, _top_card_id)
            else:
                # No card id → fall back to the pre-076 clearing path so
                # we don't wedge on a malformed session.
                state["phase"] = "dispatching"
                state["agent_dispatch"] = {}
                state["agent_dispatch_at"] = None

        # Also rewrite stale entries in active_sessions so other sessions
        # don't trip the same transport error when their turn comes.
        _sessions = state.get("active_sessions") or {}
        if isinstance(_sessions, dict):
            for _cid, _sess in _sessions.items():
                if not isinstance(_sess, dict):
                    continue
                if _is_stale(
                    str(_sess.get("phase") or ""),
                    str(_sess.get("performer_stage") or ""),
                    _sess.get("agent_dispatch"),
                ):
                    from coordinare.services.reconciliation import handle_potentially_stale_session
                    await handle_potentially_stale_session(state, _cid)

    in_progress = state["board_snapshot"].get("IN_PROGRESS", [])
    in_review = state["board_snapshot"].get("IN_REVIEW", [])
    blocked = state["board_snapshot"].get("BLOCKED", [])
    todo = state["board_snapshot"].get("TODO", [])

    # 026: Detect active card removed from all known columns (cancellation)
    active_card = state.get("current_card")
    active_phase = state.get("phase", "idle")
    if active_card and isinstance(active_card, dict) and active_phase not in ("idle",):
        active_id = str(active_card.get("id", ""))
        all_known_ids = {
            str(item_id) for col in (
                in_progress, in_review, blocked, todo,
                state["board_snapshot"].get("DONE", []),
                state["board_snapshot"].get("BACKLOG", []),
            ) for item_id in col
        }
        if active_id and active_id not in all_known_ids:
            logger.warning(
                "check_board.active_card_disappeared",
                card_id=active_id,
                previous_phase=active_phase,
            )
            from coordinare.cancel import cancel_active_card
            await cancel_active_card(state, move_to_todo=False)
            return state

    if in_review:
        # Preserve dispatching phase from classify_human_feedback even if
        # the GitHub move to IN_PROGRESS failed and the card is still in
        # IN_REVIEW.  The dispatch will move it on the next attempt.
        # Also preserve blocked phase from veto override (031).
        #
        # 087 (sticky-blocked-phase trap): this early-return must be
        # slot-aware, exactly like the IN_PROGRESS guard below.  A stale
        # global phase of "dispatching"/"blocked" persisted from an earlier
        # cycle (e.g. set when a BLOCKED card was handled) would otherwise
        # short-circuit TODO pickup forever while concurrency slots sit free.
        # Only short-circuit when there are no open slots; otherwise fall
        # through so the IN_REVIEW card is re-adopted as a passive
        # monitoring_pr session and free slots are filled from TODO.
        if (
            state.get("phase") in ("dispatching", "blocked")
            and _count_slot_consuming_sessions(state) >= max_cards
        ):
            return state

        # 066 T013/FR-002/FR-009: Re-adopt orphaned IN_REVIEW cards into
        # active_sessions for any N (including N=1).  These sessions sit in
        # the passive `monitoring_pr` phase and don't consume a concurrency
        # slot, so we fall through to TODO pickup to fill open slots.
        active_sessions: dict = state.get("active_sessions") or {}
        already_active_ids = set(active_sessions.keys())
        titles_m = board.get("titles", {})  # kept for the log statement below
        readopted_any = False
        for item in in_review:
            if item in already_active_ids:
                continue
            card_dict = _build_card_dict(item, board, "IN_REVIEW")
            sess = create_session_from_card(card_dict)
            sess["phase"] = "monitoring_pr"
            # 065 Fix 7a: if a snapshot restored a performer_stage for this
            # card, preserve it so a closer restart lands in closing_review
            # rather than being demoted to implementing.
            _snapshot_stage = _snapshot_stage_for_card(state, item)
            if _snapshot_stage:
                sess["performer_stage"] = _snapshot_stage
            active_sessions[item] = sess
            already_active_ids.add(item)
            readopted_any = True
            logger.info(
                "check_board.readopted_in_review_card",
                card_id=item,
                title=str(titles_m.get(item, "")),
            )
        if readopted_any:
            state["active_sessions"] = active_sessions
        # If this per-session invocation is for an IN_REVIEW card, preserve
        # monitoring_pr and return.  Otherwise fall through to TODO pickup.
        current = state.get("current_card")
        current_id = (
            str(current.get("id", "")) if isinstance(current, dict) else ""
        )
        if current_id and current_id in in_review:
            state["phase"] = "monitoring_pr"
            return state
        # Fall through to TODO pickup; passive sessions don't block new work.
    if in_progress:
        # Fresh-start recovery (no current_card) should re-adopt the active
        # board card even if stale system_error_count residue exists.
        # Keep system_error routing for actively tracked cards — but ONLY when
        # the live phase actually is system_error.  After Fix 18 preserved
        # system_error_count across a successful retry-dispatch, a non-zero
        # counter coexists with phase=monitoring_performer (the new container
        # is being polled).  Without the phase check, this branch would
        # rewrite that phase back to system_error every cycle, monitor_performer
        # would never run, and handle_system_error would re-dispatch on every
        # iteration — leaking a fresh container each loop.  (065 Fix 21.)
        if (
            state.get("system_error_count", 0) > 0
            and state.get("current_card") is not None
            and state.get("phase") == "system_error"
        ):
            return state
        # Preserve dispatching, monitoring_performer, and blocked phases so
        # the lifecycle re-entry, performer monitoring, and veto overrides
        # aren't overwritten by check_board.  But only when there is an
        # active card to protect — without a current_card these phases are
        # stale residue (e.g. snapshot restored phase but per-symphony swap
        # cleared the card), and we must fall through to re-adopt instead
        # of looping with no work.
        #
        # 065 Fix 5: in multi-card mode, also fall through when concurrency
        # slots remain open so the 065 US2 IN_PROGRESS re-adopt + TODO
        # pickup at the bottom of this function can fill those slots on the
        # same cycle.  Without this, every per-session invocation in steady
        # state (phase=monitoring_performer + current_card set) short-circuits
        # here and the daemon silently serialises work to one card at a time
        # even when ``max_concurrent_cards > 1``.
        current_phase = state.get("phase")
        if (
            current_phase in ("dispatching", "monitoring_performer", "blocked")
            and state.get("current_card") is not None
            and _count_slot_consuming_sessions(state) >= max_cards
        ):
            return state
        # Otherwise: either no active card, or open slots remain — fall through
        # to the re-adopt + TODO pickup below.  The monitoring_agent override
        # is gated to skip when the existing phase is a more-specific in-flight
        # phase, so the live performer/dispatch state stays intact for the
        # per-session invocation that owns it.

        # 066 T014/FR-002: unified IN_PROGRESS re-adopt for any N (including
        # N=1).  Re-adopt every uncovered IN_PROGRESS card into active_sessions
        # so the per-session graph invocations have a session to land in.
        # IN_PROGRESS sessions consume a concurrency slot (unlike the passive
        # monitoring_pr re-adoptions in the IN_REVIEW branch).
        active_sessions: dict = state.get("active_sessions") or {}
        already_active_ids = set(active_sessions.keys())
        titles_p = board.get("titles", {})  # kept for the log statement below
        readopted_any = False
        # 076 (live QA #150): resume-stage policy used when no snapshot stage is
        # available.  The old policy blindly downgraded assessor/architect →
        # implementing (assuming a re-adopted IN_PROGRESS card was always
        # mid-implementation), which skipped an assessor/architect that never
        # actually finished.  We now derive the resume stage from the card's
        # completed-stage artifacts on its branch (see _derive_resume_stage),
        # resuming at the earliest INCOMPLETE stage.
        lifecycle_seq = [
            str(stage) for stage in (state.get("lifecycle_sequence") or ["implementing"])
            if isinstance(stage, str) and stage
        ]
        _flat_cur = state.get("current_card") if isinstance(state.get("current_card"), dict) else None
        _flat_cur_id = str(_flat_cur.get("id", "")) if isinstance(_flat_cur, dict) else ""
        for item in in_progress:
            if item in already_active_ids:
                continue
            # 066 FR-002: when the flat current_card matches the card we are
            # readopting (post-restart restore path), reuse it as the seed so
            # PR-specific fields (pr_url, pr_node_id) persist through readopt.
            if _flat_cur_id and _flat_cur_id == item and isinstance(_flat_cur, dict):
                card_dict = dict(_flat_cur)
                card_dict["status"] = "IN_PROGRESS"
                card_dict.setdefault("previous_status", "IN_PROGRESS")
            else:
                card_dict = _build_card_dict(item, board, "IN_PROGRESS")
            sess = create_session_from_card(card_dict)
            # 065 Fix 7a: if the snapshot restored a performer_stage for this
            # card, preserve it and resume monitoring rather than re-dispatch.
            _snapshot_stage = _snapshot_stage_for_card(state, item)
            if _snapshot_stage:
                sess["performer_stage"] = _snapshot_stage
            else:
                # 076 (live QA #150): no durable stage to restore — derive the
                # resume stage from completed-stage artifacts on the card branch
                # so we resume at the earliest INCOMPLETE stage rather than the
                # blind "implementing" default (which skipped an assessor /
                # architect that never finished).  Falls back to that default
                # only when derivation can't determine a stage.
                _derived_stage = await _derive_resume_stage(github, card_dict, lifecycle_seq)
                if _derived_stage:
                    sess["performer_stage"] = _derived_stage
            # 069: a freshly-readopted session has no agent_dispatch.session_id,
            # so monitor_performer cannot poll status — leave the default
            # phase="dispatching" from create_session_from_card so a fresh
            # container spins up. (Snapshot-restored sessions that carry a real
            # session_id are handled by Fix 22 above and never reach this loop.)
            active_sessions[item] = sess
            already_active_ids.add(item)
            readopted_any = True
            logger.info(
                "check_board.readopted_in_progress_card",
                card_id=item,
                title=str(titles_p.get(item, "")),
            )
        if readopted_any:
            state["active_sessions"] = active_sessions
        # If this per-session invocation is for an IN_PROGRESS card, preserve
        # monitoring_agent for that session and return — the bootstrap
        # invocation (no current_card, or current_card not in in_progress)
        # falls through to TODO pickup below.
        current = state.get("current_card")
        current_id = (
            str(current.get("id", "")) if isinstance(current, dict) else ""
        )
        if current_id and current_id in in_progress:
            # 065 Fix 5: don't clobber a more-specific in-flight phase that the
            # per-session invocation arrived with.
            # 073 post-US6: also preserve "monitoring_pr" — assess_card sets
            # this when the card already has an open PR, and clobbering to
            # monitoring_agent here causes monitor_performer (with no
            # session_id) to reset phase=dispatching, re-triggering
            # dispatch_card → notify on every cycle.
            if current_phase not in (
                "monitoring_performer",
                "monitoring_pr",
                "dispatching",
                "blocked",
            ):
                state["phase"] = "monitoring_agent"
            # Pick the lexicographically smallest IN_PROGRESS card as the
            # "primary" that falls through to TODO pickup — deterministic
            # across the fanout, so only one session traverses GitHub
            # side-effect paths per cycle.
            if _count_slot_consuming_sessions(state) >= max_cards:
                return state
            if current_id != min(in_progress):
                return state
            # else: primary in-flight session, open slots remain — fall through.
        # Fall through to TODO pickup so remaining concurrency slots fill.
    if blocked:
        # Multi-card mode: per-session graph invocations re-enter check_board
        # with state["current_card"] already populated by session_to_state.
        # If the current session is processing a non-blocked card (e.g. a TODO
        # card just dispatching), the blocked branch must NOT clobber its
        # current_card — the BLOCKED card will be handled by its own session's
        # invocation (or by the bootstrap call when current_card is unset).
        _cur = state.get("current_card") or {}
        _cur_id = str(_cur.get("id", "")) if isinstance(_cur, dict) else ""
        if _cur_id and _cur_id not in set(blocked):
            # This session belongs to a different card — skip blocked handling
            # and fall through to TODO pickup (which is also guarded against
            # overwriting current_card when a session is loaded).
            pass
        elif state.get("system_error_notified"):
            state["phase"] = "idle"
            _retire_active_session(state)
            # Don't return yet when there are TODO cards we could pick up.
            if not todo:
                return state
            # else: fall through to TODO pickup below — skip blocked-card handling.
        else:
            item = blocked[0]
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            issue_urls = board.get("issue_urls", {})
            content_node_ids = board.get("content_node_ids", {})
            description = str(descriptions.get(item, ""))
            blocked_card_dict = {
                "id": item,
                "issue_id": str(content_node_ids.get(item, "")),
                "issue_number": int(issue_numbers.get(item, 0)),
                "issue_url": str(issue_urls.get(item, "")),
                "title": str(titles.get(item, "")),
                "description": description,
                "acceptance_criteria": parse_acceptance_criteria(description),
                "status": "BLOCKED",
                "previous_status": "BLOCKED",
            }
            # 066 FR-002/FR-010: adopt blocked card into active_sessions so the
            # unified bootstrap + _rederive_current_card pipeline preserves it.
            _sessions_b = state.get("active_sessions") or {}
            if item not in _sessions_b:
                _sess_b = create_session_from_card(blocked_card_dict)
                _sess_b["phase"] = "blocked"
                _sessions_b[item] = _sess_b
                state["active_sessions"] = _sessions_b
            if not state.get("active_card_id"):
                state["active_card_id"] = item
            _rederive_current_card(state)

            # 069 follow-up: prefer the per-card session watermark over the
            # top-level mirror.  The top-level value is reset to None when the
            # daemon restores from snapshot (or when handle_blocked's no-questions
            # requeue path fires), but the session-level value survives via
            # PersistedSession.last_blocked_notified_at.  Without this fallback,
            # check_board can never detect new user comments after a restart and
            # the card stays blocked indefinitely.  Mirrors handle_blocked.py:152.
            sess_for_watermark = (state.get("active_sessions") or {}).get(item)
            sess_last = (
                sess_for_watermark.get("last_blocked_notified_at")
                if isinstance(sess_for_watermark, dict)
                else None
            )
            last_notified = sess_last if sess_last is not None else state.get("last_blocked_notified_at")
            issue_node_id = str(content_node_ids.get(item, ""))
            if last_notified is not None and isinstance(last_notified, datetime):
                try:
                    details = await github.get_issue_details(issue_node_id or item)
                except Exception as exc:
                    logger.warning("check_board.get_issue_details_failed", card_id=item, error=str(exc))
                    state["phase"] = "blocked"
                    return state
                comments_node = details.get("comments")
                comments = (
                    comments_node.get("nodes", [])
                    if isinstance(comments_node, dict)
                    else []
                )
                for comment in comments:
                    if not isinstance(comment, dict):
                        continue
                    # 042: Skip comments authored by the coordinare bot itself.
                    # GitHub's ``createdAt`` is second-precision while our local
                    # ``last_blocked_notified_at`` is sub-second — so the bot's
                    # own freshly-posted reminder comment can appear "newer than
                    # the cutoff" due to rounding, get misread as a user answer,
                    # and trigger an infinite blocked → dispatch loop.  Filtering
                    # by author is the correct primary check (only humans can
                    # supply answers); the timestamp remains a secondary guard
                    # so very old human comments from prior rounds don't count.
                    author = comment.get("author") or {}
                    author_login = str(author.get("login", "")) if isinstance(author, dict) else ""
                    if author_login.endswith("[bot]") or author_login == "vivi-coordinare":
                        continue
                    created_raw = comment.get("createdAt", "")
                    if not isinstance(created_raw, str) or not created_raw:
                        continue
                    try:
                        created_at = datetime.fromisoformat(
                            created_raw.replace("Z", "+00:00")
                        )
                        if created_at > last_notified:
                            # Record the user's answer alongside the questions that
                            # were asked, so assess_card can pass the full Q&A history
                            # to Claude and avoid asking the same questions again.
                            answer_body = str(comment.get("body", "")).strip()
                            prior_questions = [
                                str(q) for q in (state.get("open_questions") or [])
                            ]
                            clarification: dict = {
                                "questions": prior_questions,
                                "answer": answer_body,
                            }
                            existing = state.get("card_clarifications") or []
                            state["card_clarifications"] = [*existing, clarification]
                            state["open_questions"] = []
                            state["agent_dispatch"] = {}

                            await github.move_card(item, "IN_PROGRESS")
                            # 066 T018/FR-011: write through the session entry only.
                            # The session map is authoritative;
                            # _rederive_current_card at end of check_board
                            # propagates the change to the top-level mirror.
                            _sessions_qa: dict = state.get("active_sessions") or {}
                            _sess_qa = _sessions_qa.get(item)
                            if isinstance(_sess_qa, dict):
                                _sess_card_qa = _sess_qa.get("current_card")
                                if isinstance(_sess_card_qa, dict):
                                    _sess_card_qa["previous_status"] = "BLOCKED"
                                    _sess_card_qa["status"] = "IN_PROGRESS"
                            # Re-run assess_card with full Q&A history rather than
                            # trying to check status on an already-terminated performer.
                            state["phase"] = "dispatching"
                            state["last_blocked_notified_at"] = None
                            return state
                    except (ValueError, TypeError):
                        continue

            # 069 follow-up: always route a still-blocked card to
            # handle_blocked. The previous "phase=idle when reminder not due"
            # branch wedged the session — state_to_session mirrored that "idle"
            # back onto session.phase, which _count_slot_consuming_sessions
            # treats as slot-consuming. Result: blocked card occupied a slot
            # indefinitely with no work and TODO pickup never fired.
            # handle_blocked has its own reminder-window dedup, so routing
            # there unconditionally is safe and keeps session.phase="blocked"
            # (in NON_SLOT_PHASES) so the slot is correctly released.
            state["phase"] = "blocked"

            # A blocked card is waiting on a human and does not occupy a
            # worker slot, so fall through to fill remaining slots from TODO
            # instead of returning.  TODO pickup below preserves current_card
            # (already set to the blocked card), so router still sends this
            # cycle to handle_blocked for the reminder; newly added
            # active_sessions entries get dispatched on the next cycle.
            if not todo:
                return state
    if todo:
        # Filter out items carrying advocate labels (FR-001a)
        advocate_labels = set()
        handled = state.get("advocate_handled_label", "")
        escalation = state.get("advocate_escalation_label", "")
        if handled:
            advocate_labels.add(str(handled))
        if escalation:
            advocate_labels.add(str(escalation))

        item_labels = board.get("item_labels", {})
        eligible_todo = [
            item_id for item_id in todo
            if not (set(item_labels.get(item_id, [])) & advocate_labels)
        ]

        # 050: Filter by assignee when assignee_filter is configured
        config = state.get("config")
        assignee_filter = getattr(config, "assignee_filter", None) if config is not None else None
        filter_login = str(assignee_filter).strip().lower() if isinstance(assignee_filter, str) else ""
        if filter_login:
            item_assignees = board.get("item_assignees", {})
            pre_assignee_count = len(eligible_todo)
            eligible_todo = [
                item_id for item_id in eligible_todo
                if filter_login in item_assignees.get(item_id, [])
            ]
            skipped = pre_assignee_count - len(eligible_todo)
            if skipped:
                logger.info(
                    "check_board.assignee_filtered",
                    skipped=skipped,
                    assignee_filter=filter_login,
                )

        # 025: Sort by priority field if configured
        if config is not None and hasattr(config, "priority"):
            prio_cfg = config.priority
            if prio_cfg.field_name:
                item_field_values = board.get("item_field_values", {})
                has_field = any(
                    prio_cfg.field_name in item_field_values.get(iid, {})
                    for iid in eligible_todo
                )
                if has_field:
                    eligible_todo = _sort_by_priority(
                        eligible_todo, item_field_values,
                        prio_cfg.field_name, prio_cfg.priority_order,
                    )
                else:
                    logger.debug(
                        "check_board.priority_field_not_found_on_cards",
                        field_name=prio_cfg.field_name,
                    )

        # 046: Filter out cards whose explicit dependencies haven't reached DONE.
        # Build the dependency graph from the full board (not just TODO) so we
        # can resolve blocker statuses across all columns.  Circular deps are
        # detected here too; cycle members are handled after filtering.
        # Clear blocked_by_dependencies here (not at cycle top) so early-return
        # paths for in_progress / in_review / blocked cards don't lose the
        # dependency context that was set on a previous cycle.
        state["blocked_by_dependencies"] = []  # type: ignore[typeddict-unknown-key]
        if eligible_todo:
            dep_graph = build_graph(board)
            # 046: Resolve off-board dependencies by checking GitHub issue state.
            # This upgrades UNRESOLVABLE → SATISFIED for closed issues that
            # were removed from the project board after completion.
            github = state.get("github_service")
            config = state.get("config")
            repo_slug = ""
            if config is not None:
                org = getattr(config, "github_org", "")
                project = getattr(config, "project_name", "")
                if org and project:
                    repo_slug = f"{org}/{project}"
            await resolve_off_board_dependencies(dep_graph, github, repo_slug)
            pre_filter_list = list(eligible_todo)  # preserve priority-sorted order
            eligible_todo = _dep_filter(eligible_todo, dep_graph)
            post_filter_set = set(eligible_todo)
            # Keep insertion order from pre_filter_list so the first filtered
            # card is the highest-priority one (not an arbitrary set member).
            filtered_ids = [iid for iid in pre_filter_list if iid not in post_filter_set]
            if filtered_ids:
                logger.info(
                    "check_board.dependency_filtered",
                    filtered_count=len(filtered_ids),
                    remaining_count=len(eligible_todo),
                )
                # Populate blocked_by_dependencies for the first filtered card
                # (in the priority-sorted eligible_todo order, not raw board
                # order) so the operator sees the highest-priority blocker.
                titles_map = board.get("titles", {})
                issue_urls_map = board.get("issue_urls", {})
                content_node_ids = board.get("content_node_ids", {})
                allowed_host = _allowed_github_host(config)
                blocked_deps_for_state: list[dict] = []
                for item_id in filtered_ids:
                    deps = dep_graph.by_dependent.get(item_id, [])
                    for d in deps:
                        if d.status != DependencyStatus.SATISFIED:
                            blocker_item = dep_graph.issue_to_item.get(d.blocker_issue_number)
                            raw_url = issue_urls_map.get(blocker_item, "") if blocker_item else ""
                            blocked_deps_for_state.append({
                                "issue_number": d.blocker_issue_number,
                                "title": titles_map.get(blocker_item, "") if blocker_item else None,
                                "column": dep_graph.issue_to_column.get(d.blocker_issue_number),
                                "issue_url": _safe_issue_url(raw_url, allowed_host),
                                "source": d.source.value,
                            })
                    if blocked_deps_for_state:
                        break  # show deps for first blocked card only
                state["blocked_by_dependencies"] = blocked_deps_for_state  # type: ignore[typeddict-unknown-key]

                # FR-010: Cards with UNRESOLVABLE deps (off-board issue not
                # closed) must be blocked with a comment, not silently left
                # in TODO.  Move them to BLOCKED and post a diagnostic — but
                # only ONCE per (item_id, blocker-set) so a card stuck for
                # many cycles doesn't accumulate duplicate comments.
                github_svc = state.get("github_service")
                announced: dict[str, str] = state.get("_dep_announcements") or {}  # type: ignore[typeddict-unknown-key]
                if github_svc is not None:
                    for item_id in filtered_ids:
                        unresolvable = [
                            d for d in dep_graph.by_dependent.get(item_id, [])
                            if d.status == DependencyStatus.UNRESOLVABLE
                        ]
                        if not unresolvable:
                            continue
                        signature = _dep_announcement_signature(
                            item_id,
                            "unresolvable",
                            [d.blocker_issue_number for d in unresolvable],
                        )
                        if announced.get(item_id) == signature:
                            continue  # already announced this exact blocker set
                        import contextlib
                        dep_labels = ", ".join(f"#{d.blocker_issue_number}" for d in unresolvable)
                        with contextlib.suppress(Exception):
                            await github_svc.move_card(item_id, "BLOCKED")
                        issue_node = content_node_ids.get(item_id)
                        if issue_node:
                            with contextlib.suppress(Exception):
                                header = coordinare_attribution(state.get("config"), None)
                                await github_svc.add_comment(
                                    issue_node,
                                    f"{header}\n\n"
                                    f"🔗 **Unresolvable dependency**: {dep_labels}\n\n"
                                    "The referenced issue(s) are not on the project board "
                                    "and could not be verified as closed (the issue may be "
                                    "open, missing, or the API check failed).  Add them to "
                                    "the board or close them to unblock this card.",
                                )
                        announced[item_id] = signature
                    state["_dep_announcements"] = announced  # type: ignore[typeddict-unknown-key]
            # (No else needed — blocked_by_dependencies is reset at the top
            # of every poll cycle; it's only populated when cards are filtered.)

            # Block cards involved in circular dependencies — move them to
            # BLOCKED on the board and post a diagnostic comment so operators
            # know which cards are deadlocked.
            if dep_graph.cycles:
                cycle_item_ids = {iid for cycle in dep_graph.cycles for iid in cycle}
                issue_numbers_map = board.get("issue_numbers", {})
                cycle_issues = [
                    f"#{issue_numbers_map.get(iid, '?')}" for iid in sorted(cycle_item_ids)
                ]
                cycle_desc = ", ".join(cycle_issues)
                logger.warning(
                    "check_board.circular_dependency_detected",
                    cycle_item_ids=sorted(cycle_item_ids),
                    cycle_description=cycle_desc,
                )
                # Remove cycle members from eligible (they can't be dispatched)
                eligible_todo = [
                    iid for iid in eligible_todo if iid not in cycle_item_ids
                ]
                # Best-effort: move cycle members to BLOCKED on the board and
                # post a comment.  This is a fire-and-forget — if it fails,
                # the cards stay in TODO but still won't be dispatched (the
                # filter already removed them).
                github = state.get("github_service")
                content_node_ids = board.get("content_node_ids", {})
                import contextlib

                # Only move TODO cards to BLOCKED — DONE/IN_PROGRESS/etc. cards
                # may appear in cycle_item_ids because build_graph parses all
                # descriptions, but moving a DONE card back to BLOCKED would be
                # destructive.
                todo_set = set(todo)
                if github is not None:
                    cycle_announced: dict[str, str] = state.get("_dep_announcements") or {}  # type: ignore[typeddict-unknown-key]
                    cycle_blockers = [
                        issue_numbers_map.get(iid, 0) for iid in sorted(cycle_item_ids)
                    ]
                    for iid in cycle_item_ids:
                        if iid not in todo_set:
                            continue
                        signature = _dep_announcement_signature(
                            iid, "cycle", cycle_blockers,
                        )
                        if cycle_announced.get(iid) == signature:
                            continue  # already announced this exact cycle
                        with contextlib.suppress(Exception):
                            await github.move_card(iid, "BLOCKED")
                        issue_node_id = content_node_ids.get(iid)
                        if issue_node_id:
                            with contextlib.suppress(Exception):
                                header = coordinare_attribution(state.get("config"), None)
                                await github.add_comment(
                                    issue_node_id,
                                    f"{header}\n\n"
                                    f"🔄 **Circular dependency detected** involving: {cycle_desc}\n\n"
                                    "These cards form a dependency cycle — none can "
                                    "proceed.  Resolve by removing or reordering the "
                                    "dependency declarations in one of the issue bodies.",
                                )
                        cycle_announced[iid] = signature
                    state["_dep_announcements"] = cycle_announced  # type: ignore[typeddict-unknown-key]

        if eligible_todo:
            titles = board.get("titles", {})
            descriptions = board.get("descriptions", {})
            issue_numbers = board.get("issue_numbers", {})
            issue_urls = board.get("issue_urls", {})
            content_node_ids = board.get("content_node_ids", {})

            # 035: Multi-card pickup — fill active_sessions up to concurrency limit
            max_cards = 1
            config = state.get("config")
            if config is not None and hasattr(config, "max_concurrent_cards"):
                max_cards = max(1, int(config.max_concurrent_cards))

            active_sessions: dict = state.get("active_sessions") or {}
            already_active_ids = set(active_sessions.keys())

            # 066 T016/FR-002: unified un-block reset for any N (including N=1).
            # When the operator moves a card from BLOCKED back to TODO, the
            # existing session is retained with current_card.status="BLOCKED"
            # and phase="blocked".  Detect that the card is now eligible again
            # and reset feedback_cycle_count to 0 so the next dispatch gets a
            # fresh budget.  Monotonic stats (total_feedback_cycles,
            # triage_blocks) are preserved.
            _eligible_set = set(eligible_todo)
            _unblocked_ids: set[str] = set()
            for _cid, _sess in active_sessions.items():
                if _cid not in _eligible_set:
                    continue
                _sess_card = _sess.get("current_card") or {}
                if str(_sess_card.get("status", "")) != "BLOCKED":
                    continue
                _prior_count = int(_sess.get("feedback_cycle_count") or 0)
                _sess["feedback_cycle_count"] = 0
                _sess_card["previous_status"] = "BLOCKED"
                _sess_card["status"] = "TODO"
                _sess["current_card"] = _sess_card
                # Re-enter the lifecycle so the next graph step dispatches.
                _sess["phase"] = "dispatching"
                _sess["open_questions"] = []
                _unblocked_ids.add(_cid)
                # 066 FR-010 I3: re-derive flat mirror for legacy callers
                # and the single-card un-block path that started here.
                if state.get("active_card_id") == _cid or not state.get("active_card_id"):
                    state["feedback_cycle_count"] = 0  # type: ignore[typeddict-unknown-key]
                    state["phase"] = "dispatching"
                    state["open_questions"] = []
                    if not state.get("active_card_id"):
                        state["active_card_id"] = _cid
                    _rederive_current_card(state)
                logger.info(
                    "dispatcher.feedback_cycle_reset",
                    card_id=_cid,
                    prior_count=_prior_count,
                    total_feedback_cycles=int(_sess.get("total_feedback_cycles") or 0),
                    triage_blocks=int(_sess.get("triage_blocks") or 0),
                    mode="multi" if max_cards > 1 else "single",
                )

            # 066 T017/FR-002: unified TODO pickup for any N (including N=1).
            # Passive-phase sessions (monitoring_pr) do not consume a concurrency slot.
            active_count = sum(
                1 for sess in active_sessions.values()
                if sess.get("phase") not in NON_SLOT_PHASES
            )
            slots_available = max(0, max_cards - active_count)
            lifecycle_seq = [
                str(s) for s in (state.get("lifecycle_sequence") or ["implementing"])
                if isinstance(s, str) and s
            ]
            _start_stage = lifecycle_seq[0] if lifecycle_seq else "implementing"
            picked = 0
            for item in eligible_todo:
                if picked >= slots_available:
                    break
                if item in already_active_ids:
                    continue  # deduplicate — covers both pre-existing sessions
                    # and duplicate IDs within eligible_todo
                card_dict = _build_card_dict(item, board, "TODO")
                sess = create_session_from_card(card_dict)
                # Honor configured lifecycle start stage (single-card parity).
                sess["performer_stage"] = _start_stage
                active_sessions[item] = sess
                already_active_ids.add(item)
                picked += 1

            state["active_sessions"] = active_sessions

            # 066 FR-002: bootstrap (active_card_id + session_to_state) is
            # hoisted to the public ``check_board`` wrapper so the same logic
            # applies regardless of which branch populated active_sessions.
            if not active_sessions:
                state["phase"] = "idle"
            return state

        # All TODO items filtered (by advocate labels or dependencies).
        # If blocked_by_dependencies is non-empty, some cards are waiting on
        # blockers — log it so operators know the board isn't truly empty.
        if state.get("blocked_by_dependencies"):
            logger.info(
                "check_board.all_todo_dependency_blocked",
                blocked_dep_count=len(state["blocked_by_dependencies"]),
                todo_count=len(todo),
            )
        # 069: A multi-card primary IN_PROGRESS session falls through here
        # when its session is mid-flight and TODO becomes empty after
        # filtering.  Preserve the working phase so monitor_performer keeps
        # running on the next cycle instead of being terminated.
        if _has_in_flight_working_session(state):
            return state
        # Clear stale current_card so persisted snapshots don't carry
        # forward a card that's no longer eligible.
        _retire_active_session(state)

    # 069: same guard for the no-TODO path — an IN_PROGRESS session with an
    # empty TODO column would otherwise be clobbered to phase="idle" and
    # never re-enter monitor_performer.
    if _has_in_flight_working_session(state):
        return state
    state["phase"] = "idle"
    return state
