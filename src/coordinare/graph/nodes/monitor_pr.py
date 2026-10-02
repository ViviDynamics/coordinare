from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState
from coordinare.graph.nodes.github_retry import (
    clear_deferred_github_operation,
    defer_github_operation,
    github_operation_ready,
    is_transient_github_outage_error,
)
from coordinare.graph.state import _set_current_card
from coordinare.models.notification import (
    EventType,
    NotificationEvent,
    NotificationSeverity,
)
from coordinare.models.review import (
    Review,
    ReviewerType,
    ReviewState,
    ReviewThread,
    StalenessClass,
    classify_reviewer,
)
from coordinare.services.board_provider import board_of, move_card_or_warn
from coordinare.services.review_staleness import (
    StalenessConfig,
    classify_review_staleness,
)

logger = structlog.get_logger(__name__)

# 127: review states that carry feedback coordinare must process before any
# merge decision (contracts/review-routing.md).
_ACTIONABLE_STATES = frozenset({"COMMENTED", "CHANGES_REQUESTED"})


def _parse_submitted_at(review: dict[str, object]) -> datetime | None:
    raw = review.get("submitted_at")
    if isinstance(raw, str) and raw:
        with contextlib.suppress(ValueError, TypeError):
            parsed = datetime.fromisoformat(raw)
            # Normalise to UTC-aware: a timezone-less ISO-8601 string (some
            # non-GitHub sources) parses naive, and comparing naive vs aware
            # (``new_t > cur_t``, or ``max()`` over mixed reviews) raises
            # TypeError. Assume naive timestamps are UTC.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    return None


def _latest_reviews_per_author(
    reviews: list[dict[str, object]],
) -> list[dict[str, object]]:
    """127 (FR-004): project a batch onto each reviewer's latest review.

    A reviewer's later review supersedes their earlier one for both the
    approval and deferral decisions. Timestamp ties — or an unparseable
    ``submitted_at`` on either side — resolve conservatively: the
    actionable-state review wins over APPROVED. Reviews without an
    ``author_login`` are never grouped (each stays effective). Input order
    is preserved in the returned list.
    """
    winner_by_author: dict[str, dict[str, object]] = {}
    for review in reviews:
        # ``or ""`` — author_login=None must normalise to "" (ungrouped), not
        # the truthy string "None" that would become a shared grouping key.
        # Group on the same normalisation classify_reviewer() uses
        # (strip().lower()) so one reviewer's variants ("Alice", "alice ")
        # aren't split into separate groups and thus escape supersession.
        login = str(review.get("author_login") or "").strip().lower()
        if not login:
            continue
        current = winner_by_author.get(login)
        if current is None:
            winner_by_author[login] = review
            continue
        cur_t = _parse_submitted_at(current)
        new_t = _parse_submitted_at(review)
        if cur_t is not None and new_t is not None and cur_t != new_t:
            if new_t > cur_t:
                winner_by_author[login] = review
        elif str(review.get("state", "")) in _ACTIONABLE_STATES:
            # Tie or unparseable timestamp: never let an APPROVED displace
            # (or survive over) an actionable review on ordering luck.
            winner_by_author[login] = review
    winners = set(map(id, winner_by_author.values()))
    # Use the SAME normalization as the grouping key so a whitespace-only
    # author_login (which normalises to "" and is therefore ungrouped) is
    # treated consistently as "no login" in the passthrough too.
    return [
        r
        for r in reviews
        if not str(r.get("author_login") or "").strip().lower() or id(r) in winners
    ]


def _latest_human_change_request(
    reviews: list[dict[str, object]], human_reviewers: list[str],
) -> dict[str, object] | None:
    """The most recent human CHANGES_REQUESTED review across the FULL list
    (not the processed-filtered subset) — GitHub gates on it regardless of what
    coordinare has already dispatched."""
    candidates = [
        r
        for r in reviews
        if str(r.get("state", "")) == "CHANGES_REQUESTED"
        and classify_reviewer(str(r.get("author_login", "")), human_reviewers, [])
        == ReviewerType.HUMAN
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda r: _parse_submitted_at(r) or datetime.min.replace(tzinfo=UTC),
    )


async def _surface_stale_change_request(
    state: CoordinareState,
    github: object,
    card: dict[str, object],
    card_id: str,
    reviews: list[dict[str, object]],
    review_threads: list[dict[str, object]],
    head_oid: str,
    review_decision: str,
    human_reviewers: list[str],
) -> None:
    """128 (US1/US3/FR-011): when GitHub still gates on an outstanding human
    CHANGES_REQUESTED but nothing fresh is actionable, decide stale vs fresh.

    STALE_ADDRESSED  → re-request the reviewer + move the card to IN_REVIEW +
                       one deduped notification (the card rejoins review flow).
    STALE_UNADDRESSED→ one deduped notification; card stays parked.
    FRESH / none     → no-op (existing behavior). Fully fail-safe.
    """
    board_provider = board_of(state)
    surfaced: dict[str, str] = state.setdefault("surfaced_stale_reviews", {})

    # FR-011: verdict cleared (approved/dismissed) → drop dedup markers so a
    # future change-request re-surfaces, and let the normal merge path resume.
    if review_decision.upper() != "CHANGES_REQUESTED":
        if surfaced:
            surfaced.clear()
        return

    # 128 (review): prune dedup markers for reviews no longer in the list
    # (dismissed/superseded) so surfaced_stale_reviews can't grow unbounded
    # across co-review rotations while another reviewer's CR keeps the gate up.
    if surfaced:
        live_ids = {str(r.get("id", "")) for r in reviews}
        for rid in [k for k in surfaced if k not in live_ids]:
            del surfaced[rid]

    gating = _latest_human_change_request(reviews, human_reviewers)
    if gating is None:
        return
    review_id = str(gating.get("id", ""))
    commit_oid = str(gating.get("commit_oid", "") or "")

    review = Review(
        id=review_id,
        author_login=str(gating.get("author_login", "")),
        author_type=ReviewerType.HUMAN,
        state=ReviewState.CHANGES_REQUESTED,
        commit_oid=commit_oid,
        submitted_at=_parse_submitted_at(gating) or datetime.now(UTC),
    )
    threads = [
        ReviewThread(
            id=str(t.get("id", "")),
            is_resolved=bool(t.get("is_resolved", False)),
            review_id=(str(t["review_id"]) if t.get("review_id") else None),
        )
        for t in review_threads
    ]
    # commits_behind: precise counting needs a compare API (see T019/limitation);
    # for the safe default threshold (>=1 later commit) "commit != head" ⇒ >=1.
    commits_behind = 1 if (commit_oid and head_oid and commit_oid != head_oid) else 0
    result = classify_review_staleness(
        review, head_oid, commits_behind, threads, config=StalenessConfig(),
    )

    if result.classification is StalenessClass.FRESH:
        return

    # Dedup: act once per (review, head). Re-fires only when head advances.
    if surfaced.get(review_id) == head_oid:
        return

    logger.info(
        "stale_review.detected",
        card_id=card_id,
        review_id=review_id,
        classification=result.classification.value,
        reason=result.reason,
        review_commit=commit_oid[:8],
        head=head_oid[:8],
    )

    if result.classification is StalenessClass.STALE_ADDRESSED:
        reviewer = str(gating.get("author_login", ""))
        try:
            if hasattr(github, "request_reviews"):
                await github.request_reviews(pr_id=str(card.get("pr_node_id") or ""), reviewer_logins=[reviewer])
                logger.info("stale_review.re_requested", card_id=card_id, reviewer=reviewer)
        except Exception as exc:  # fail-safe: never crash the cycle
            logger.warning("stale_review.re_request_failed", card_id=card_id, error=str(exc))
        try:
            await move_card_or_warn(board_provider, card_id, "IN_REVIEW")
            card["status"] = "IN_REVIEW"
            _set_current_card(state, card)
            logger.info("stale_review.routed_in_review", card_id=card_id)
        except Exception as exc:
            logger.warning("stale_review.move_failed", card_id=card_id, error=str(exc))

    await _notify_stale_review(state, card, card_id, gating, commit_oid, head_oid, result.classification)
    surfaced[review_id] = head_oid


async def _notify_stale_review(
    state: CoordinareState,
    card: dict[str, object],
    card_id: str,
    gating: dict[str, object],
    commit_oid: str,
    head_oid: str,
    classification: StalenessClass,
) -> None:
    """One deduped operator notification per stale-review situation (FR-004)."""
    notification_service = state.get("notification_service")
    if notification_service is None:
        return
    review_id = str(gating.get("id", ""))
    reviewer = str(gating.get("author_login", ""))
    pr = str(card.get("pr_url") or card.get("pr_node_id") or "")
    action = (
        f"re-review or dismiss review {review_id} on {pr}"
        if classification is StalenessClass.STALE_ADDRESSED
        else f"feedback still open — address, then re-review/dismiss review {review_id} on {pr}"
    )
    try:
        await notification_service.dispatch(
            NotificationEvent(
                event_type=EventType.stale_review_surfaced,
                severity=NotificationSeverity.warning,
                source="monitor_pr",
                payload={
                    "card_id": card_id,
                    "card_title": str(card.get("title", "")),
                    "pr": pr,
                    "review_id": review_id,
                    "reviewer": reviewer,
                    "review_date": str(gating.get("submitted_at", "")),
                    "review_commit": commit_oid[:8],
                    "head_commit": head_oid[:8],
                    "classification": classification.value,
                    "next_action": action,
                },
                dedup_key=f"stale_review:{card.get('pr_node_id')}:{review_id}:{head_oid}",
            ),
        )
    except Exception as exc:  # notification must never crash the gate
        logger.warning("stale_review.notify_failed", card_id=card_id, error=str(exc))


async def monitor_pr(state: CoordinareState) -> CoordinareState:
    github = state.get("github_service")
    board_provider = board_of(state)
    card = state.get("current_card")
    if github is None or not isinstance(card, dict):
        state["phase"] = "idle"
        return state

    # ``"None"`` (the literal string) can land here from older snapshots
    # written before the daemon snapshot serialisation bug was fixed —
    # treat it the same as missing so we attempt PR recovery rather than
    # querying GitHub with a string that resolves to no node.
    raw_pr_node_id = card.get("pr_node_id")
    pr_node_id = str(raw_pr_node_id or "")
    if pr_node_id in ("", "None", "null"):
        pr_node_id = ""

    if not pr_node_id:
        # Recovery path: state lacks a usable pr_node_id but the card sits in
        # IN_REVIEW, meaning a PR exists somewhere — find it via the issue's
        # "Closes #N" linkage and rehydrate the card so monitoring continues.
        issue_node_id = str(card.get("issue_id") or "")
        recovered = None
        ready, retry_in = github_operation_ready(state, "monitor_pr")
        if not ready:
            logger.info(
                "monitor_pr.deferred_wait",
                retry_in_seconds=round(retry_in, 1),
                reason="pr_recovery",
            )
            state["phase"] = "monitoring_pr"
            return state
        if issue_node_id:
            try:
                recovered = await github.find_pr_for_issue(issue_node_id)
                clear_deferred_github_operation(state, "monitor_pr")
            except Exception as exc:
                if is_transient_github_outage_error(exc):
                    deferred = defer_github_operation(
                        state,
                        operation="monitor_pr",
                        error=exc,
                        base_delay_seconds=30.0,
                        max_delay_seconds=600.0,
                    )
                    logger.warning(
                        "monitor_pr.pr_recovery_deferred",
                        issue_node_id=issue_node_id,
                        error=str(exc),
                        attempt=deferred.get("attempt"),
                        retry_at=deferred.get("retry_at"),
                    )
                    state["phase"] = "monitoring_pr"
                    return state
                logger.warning(
                    "monitor_pr.pr_recovery_failed",
                    issue_node_id=issue_node_id,
                    error=str(exc),
                )
        if recovered:
            card["pr_node_id"] = recovered["pr_node_id"]
            card["pr_url"] = recovered["pr_url"]
            _set_current_card(state, card)
            pr_node_id = recovered["pr_node_id"]
            logger.info(
                "monitor_pr.pr_recovered",
                pr_node_id=pr_node_id,
                pr_url=recovered["pr_url"],
                issue_node_id=issue_node_id,
            )
        else:
            logger.warning(
                "monitor_pr.no_pr_node_id_and_no_recovery",
                issue_node_id=issue_node_id,
                card_id=str(card.get("id", "")),
            )
            state["phase"] = "idle"
            return state

    # 065 Fix 8 — Re-gate PR checks while the card sits in IN_REVIEW so a new
    # HEAD push that turns CI red does not silently page a human reviewer.
    # On BOUNCE we move the card back to IN_PROGRESS and the gate's update
    # already sets phase=dispatching + performer_stage=implementing with a
    # relay_feedback payload naming the failed check(s), which the implementer
    # picks up on its next dispatch.  HOLD keeps us in monitoring_pr (the gate
    # natively returns monitoring_performer; we override that here).
    pr_url = str(card.get("pr_url") or "")
    card_id = str(card.get("id") or "")
    if pr_url and card_id:
        from coordinare.graph.nodes.monitor_performer import _evaluate_pr_checks_gate

        gate_updates, gate_stop = await _evaluate_pr_checks_gate(
            state, card_id, pr_url,
        )
        if gate_stop:
            is_bounce = gate_updates.get("phase") == "dispatching"
            for key, value in gate_updates.items():
                state[key] = value  # type: ignore[literal-required]
            if is_bounce:
                logger.warning(
                    "monitor_pr.checks_gate_bounce",
                    card_id=card_id,
                    pr_url=pr_url,
                )
                with contextlib.suppress(Exception):
                    await move_card_or_warn(board_provider, card_id, "IN_PROGRESS")
                card["status"] = "IN_PROGRESS"
                _set_current_card(state, card)
            else:
                # HOLD — gate returned monitoring_performer; we are in the PR
                # phase, so stay there and poll again next tick.
                state["phase"] = "monitoring_pr"
            return state
        # FORWARD or gate disabled — apply any cache updates and continue.
        for key, value in gate_updates.items():
            state[key] = value  # type: ignore[literal-required]

    try:
        ready, retry_in = github_operation_ready(state, "monitor_pr")
        if not ready:
            logger.info(
                "monitor_pr.deferred_wait",
                retry_in_seconds=round(retry_in, 1),
                reason="get_reviews",
            )
            state["phase"] = "monitoring_pr"
            return state
        # 128: prefer the richer single-query context (reviews + threads +
        # head_oid + reviewDecision) when available; fall back to the plain
        # reviews list so existing callers/doubles behave identically.
        if hasattr(github, "get_pr_review_context"):
            _ctx = await github.get_pr_review_context(pr_node_id)
            _ctx = _ctx if isinstance(_ctx, dict) else {}
            reviews = _ctx.get("reviews", []) or []
            _review_threads = _ctx.get("review_threads", []) or []
            _pr_head_oid = str(_ctx.get("head_oid", "") or "")
            _review_decision = str(_ctx.get("review_decision", "") or "")
        else:
            reviews = await github.get_pr_reviews(pr_node_id)
            _review_threads, _pr_head_oid, _review_decision = [], "", ""
        clear_deferred_github_operation(state, "monitor_pr")
    except Exception as exc:
        if is_transient_github_outage_error(exc):
            deferred = defer_github_operation(
                state,
                operation="monitor_pr",
                error=exc,
                base_delay_seconds=30.0,
                max_delay_seconds=600.0,
            )
            logger.warning(
                "monitor_pr.get_reviews_deferred",
                error=str(exc),
                attempt=deferred.get("attempt"),
                retry_at=deferred.get("retry_at"),
            )
            state["phase"] = "monitoring_pr"
            return state
        logger.warning("monitor_pr.get_reviews_failed", error=str(exc))
        state["phase"] = "monitoring_pr"  # stay in current phase, retry next cycle
        return state
    human_reviewers = state.get("human_reviewers", [])
    trusted_bot_reviewers = state.get("trusted_bot_reviewers", [])

    # Filter reviews using two mechanisms:
    # 1. lifecycle_completed_at timestamp — reviews before this are from a prior cycle
    # 2. processed_review_ids — reviews already dispatched (survives cutoff clears)
    lifecycle_completed_at = state.get("lifecycle_completed_at")
    cutoff: datetime | None = None
    if isinstance(lifecycle_completed_at, datetime):
        cutoff = lifecycle_completed_at
    elif isinstance(lifecycle_completed_at, str) and lifecycle_completed_at:
        with contextlib.suppress(ValueError, TypeError):
            cutoff = datetime.fromisoformat(lifecycle_completed_at)

    processed_ids: set[str] = state.get("processed_review_ids") or set()

    filtered: list[dict[str, object]] = []
    for review in reviews:
        review_id = str(review.get("id", ""))
        login = str(review.get("author_login", ""))
        reviewer_type = classify_reviewer(
            login,
            human_reviewers if isinstance(human_reviewers, list) else [],
            trusted_bot_reviewers if isinstance(trusted_bot_reviewers, list) else [],
        )
        # Store the enum's value (a plain string like "HUMAN") rather than
        # the enum instance — relay_feedback dicts are JSON-serialized via
        # json.dumps(..., default=str), and enum __str__ gives
        # "ReviewerType.HUMAN" instead of the intended stable "HUMAN".
        review["author_type"] = ReviewerType(reviewer_type).value

        # Skip reviews already processed in a previous dispatch cycle
        if review_id and review_id in processed_ids:
            continue

        # Filter out reviews submitted before the lifecycle completed.
        if cutoff is not None:
            submitted_raw = review.get("submitted_at", "")
            if isinstance(submitted_raw, str) and submitted_raw:
                try:
                    submitted_at = datetime.fromisoformat(submitted_raw)
                    if submitted_at <= cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

        filtered.append(review)

    # 127 (FR-004): within the batch, each reviewer's latest review governs —
    # a reviewer's own later APPROVED supersedes their earlier change request
    # and vice versa. Cross-reviewer states never supersede each other.
    effective = _latest_reviews_per_author(filtered)

    actionable = [
        r
        for r in effective
        if r.get("author_type")
        in (ReviewerType.HUMAN.value, ReviewerType.TRUSTED_BOT.value)
        and str(r.get("state", "")) in _ACTIONABLE_STATES
    ]
    # Only HUMAN approval triggers merge — trusted bots can provide
    # feedback but cannot approve on behalf of a human. When several human
    # approvals are effective, the latest one names the deferral event.
    approvals = [
        r
        for r in effective
        if r.get("author_type") == ReviewerType.HUMAN.value
        and r.get("state") == "APPROVED"
    ]
    approval_review = (
        max(
            approvals,
            key=lambda r: _parse_submitted_at(r)
            or datetime.min.replace(tzinfo=UTC),
        )
        if approvals
        else None
    )

    state["pending_reviews"] = actionable
    if actionable:
        # 127 (FR-001/FR-002): actionable feedback always routes to
        # classification first. A coexisting approval is deferred — never
        # consumed: its review ID never rides pending_reviews, so it stays
        # unprocessed and re-surfaces to merge on a later evaluation once
        # nothing actionable remains (FR-003).
        if approval_review is not None:
            logger.info(
                "monitor_pr.merge_deferred",
                card_id=str(card.get("id", "")),
                approval_review_id=str(approval_review.get("id", "")),
                actionable_review_ids=[str(r.get("id", "")) for r in actionable],
            )
        logger.info(
            "monitor_pr.actionable_reviews",
            count=len(actionable),
            cutoff=cutoff.isoformat() if cutoff else None,
        )
        state["phase"] = "relay_feedback"
    elif approval_review is not None:
        # 090 L1 (US1) — refuse to advance to merge while a REQUIRED check on
        # the PR's *base* branch is red.  Default-off and fail-open, so when the
        # gate is disabled / indeterminate this is byte-identical to going
        # straight to "merging" (SC-006).  Re-evaluated each cycle (no latch).
        from coordinare.graph.nodes.monitor_performer import (
            _evaluate_baseline_prevention_gate,
        )

        base_updates, base_stop = await _evaluate_baseline_prevention_gate(
            state, card_id, pr_url,
        )
        if base_stop:
            for key, value in base_updates.items():
                state[key] = value  # type: ignore[literal-required]
            return state
        # 510 — refuse to advance to merge while GitHub's aggregate
        # reviewDecision still says CHANGES_REQUESTED. The 064 checks gate
        # consulted checks alone, so on a repo whose branch protection
        # ignores review state the closer would merge despite an open
        # change request (observed in the 2026-10-02 e2e run: 50 minutes of
        # FORWARD polls against an unmergeable PR). This hold sits *after*
        # fresh actionable feedback has been relayed (the relay branch
        # above returns first), so the implementer does receive the
        # requested changes; it blocks only the merge itself and
        # self-clears when the reviewer re-reviews or dismisses. An empty
        # decision (legacy service without get_pr_review_context) fails
        # open; transient fetch failures never reach this point (deferred
        # above), so the merge transition is only reachable with review
        # state successfully fetched this cycle.
        if _review_decision == "CHANGES_REQUESTED":
            logger.warning(
                "monitor_pr.merge_held_changes_requested",
                card_id=str(card.get("id", "")),
                pr_url=pr_url,
                review_decision=_review_decision,
            )
            state["phase"] = "monitoring_pr"
            # 510 (round 2): surface the stale-review state the same way the
            # no-approval branch does before parking, so an addressed stale
            # review is re-requested rather than silently blocking forever.
            try:
                await _surface_stale_change_request(
                    state,
                    github,
                    card,
                    card_id,
                    reviews,
                    _review_threads,
                    _pr_head_oid,
                    _review_decision,
                    human_reviewers if isinstance(human_reviewers, list) else [],
                )
            except Exception as exc:
                logger.warning("stale_review.handler_failed", error=str(exc))
            return state
        state["phase"] = "merging"
    else:
        # 128: no fresh-actionable feedback and no approval. If GitHub still
        # gates on an outstanding human CHANGES_REQUESTED, surface it —
        # stale-addressed → re-request + IN_REVIEW; stale-unaddressed → notify;
        # fresh → unchanged. Fail-safe: a handler error never breaks the cycle.
        try:
            await _surface_stale_change_request(
                state,
                github,
                card,
                card_id,
                reviews,
                _review_threads,
                _pr_head_oid,
                _review_decision,
                human_reviewers if isinstance(human_reviewers, list) else [],
            )
        except Exception as exc:
            logger.warning("stale_review.handler_failed", error=str(exc))
        state["phase"] = "monitoring_pr"
    return state
