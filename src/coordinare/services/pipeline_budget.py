"""Reserve issue slots across performer stages, independently of role capacity."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
    from collections.abc import Collection

    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

# #186: what a card in monitoring_pr is waiting on, as monitor_pr records it in
# the session's ``review_wait``. Only a wait on a human releases the slot and
# counts toward max_cards_in_review. Anything the daemon or CI resolves next
# without a person is "automation": the card keeps its reservation, but only for
# ``review_slot_hold_seconds``. A longer hold is treated as a wait on a human.
REVIEW_WAIT_HUMAN: Final = 'human'
REVIEW_WAIT_AUTOMATION: Final = 'automation'
REVIEW_SLOT_HOLD_SECONDS_DEFAULT: Final = 900
REVIEW_WAIT_UNEVALUATED: Final = 'unevaluated'
_HOLD_PHASES: Final = ('monitoring_pr', 'merging')


def hold_clock() -> float:
    """The clock for hold bounds: monotonic, so a wall-clock jump cannot shorten or stretch a hold.

    Holds are not persisted, so a value is only ever compared with another value from
    this process. One seam, so a test can cross the bound.
    """
    return time.monotonic()


def configured_hold_seconds(config: Any) -> int:
    """``review_slot_hold_seconds``, or the default when the config does not carry it."""
    raw = getattr(config, 'review_slot_hold_seconds', None)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else REVIEW_SLOT_HOLD_SECONDS_DEFAULT


def expire_review_holds(sessions: dict[str, Any], *, hold_seconds: int) -> list[str]:
    """#186: a non-human hold that outlives its bound becomes a human wait.

    Applies to a card in monitoring_pr waiting on checks, the daemon, or not yet
    evaluated, and to a card in merging whose merge keeps failing. A card with live
    work is never expired. The clock starts at the first sighting of the hold
    (``review_wait_since`` is None), so a card restored after a restart is timed
    from its first sighting. Past ``hold_seconds`` the card is recorded as waiting
    on a human: it releases its slot, still ticks every cycle, and regains a slot
    through the returning path when it needs a performer. Logged once per expiry.
    """
    now = hold_clock()
    expired: list[str] = []
    for cid, session in sessions.items():
        if session.get('phase') not in _HOLD_PHASES or has_live_work(session):
            continue
        if session.get('review_wait') == REVIEW_WAIT_HUMAN:
            continue
        since = session.get('review_wait_since')
        if not isinstance(since, float):
            session['review_wait_since'] = now
            continue
        held = now - since
        if held <= hold_seconds:
            continue
        reason = session.get('review_wait_reason') or (
            'merge' if session.get('phase') == 'merging' else REVIEW_WAIT_UNEVALUATED
        )
        session['review_wait'] = REVIEW_WAIT_HUMAN
        session['review_wait_expired'] = True
        expired.append(cid)
        logger.warning('monitor_pr.slot_hold_expired', card_id=cid, reason=reason, seconds=int(held))
    return expired


def has_live_side_writer(session: dict[str, Any]) -> bool:
    """An owned side writer holds capacity until its termination is confirmed."""
    side = session.get('documenting_side') or {}
    return bool(side.get('session_id')) and bool(
        side.get('status') == 'running' or side.get('writer_active'),
    )


_RELEASED_STATUSES = ('BACKLOG', 'BLOCKED', 'DONE', 'CLOSED', 'CANCELED')
_RELEASED_PHASES = ('idle', 'blocked', 'env_blocked', 'done')
_LIVE_PERFORMER_PHASES = ('monitoring_performer', 'monitoring_agent')


def has_live_performer(session: dict[str, Any]) -> bool:
    """A foreground performer or agent is running for this card right now."""
    return session.get('phase') in _LIVE_PERFORMER_PHASES and bool(
        (session.get('agent_dispatch') or {}).get('session_id'),
    )


def has_live_work(session: dict[str, Any]) -> bool:
    """The one liveness test for a slot (#186): a live performer or a live side writer.

    Pickup counting in check_board and the budget both start from this, so a
    session counts as running work the same way in both places.
    """
    return has_live_performer(session) or has_live_side_writer(session)


def is_parked_in_review(session: dict[str, Any]) -> bool:
    """#179, #186: waiting on a human review with nothing of ours running.

    A parked card holds no pipeline slot and is not counted toward
    max_cards_in_review. It is still ticked every cycle so its merge evaluation
    runs (#102), and it asks for a slot again only when it needs a performer.
    A card waiting on checks or on the daemon, or not evaluated yet, is not
    parked: it keeps its reservation until the bound.
    """
    card = session.get('current_card') or {}
    return (
        session.get('phase') == 'monitoring_pr'
        and session.get('review_wait') == REVIEW_WAIT_HUMAN
        and card.get('status') not in _RELEASED_STATUSES
        and not has_live_work(session)
    )


def is_released_merge(session: dict[str, Any]) -> bool:
    """#186: a merge that has been held past the bound. It holds no slot, and it is still ticked.

    It is not counted toward max_cards_in_review, which counts review waits only. It
    is still ticked every cycle, so the merge is retried and merges as soon as it can.
    """
    return (
        session.get('phase') == 'merging'
        and session.get('review_wait') == REVIEW_WAIT_HUMAN
        and not has_live_work(session)
    )


def has_open_pr(session: dict[str, Any]) -> bool:
    """The card has already produced a PR."""
    card = session.get('current_card') or {}
    return bool(card.get('pr_node_id') or card.get('pr_number') or card.get('pr_url'))


def has_started(session: dict[str, Any]) -> bool:
    """#186: the card was accepted and has work of ours in flight, PR or not.

    Started means the work was accepted: an open PR, a blueprint (architecting
    done), a stage verdict for a stage past assessing, or an accepted assessment.
    A declined assessment (not work, or needs a split) is not started, so a card
    that comes back to To Do is new work again. A started card that has lost its
    PR fields (blocked then unblocked, or readopted after restart) is returning
    work: it is admitted ahead of cards that have not started and is not held by
    the review cap.
    """
    assessment = session.get('assessment') or {}
    accepted = bool(assessment) and not assessment.get('declined')
    verdicts = session.get('stage_verdicts') or {}
    past_assessing = any(stage != 'assessing' for stage in verdicts)
    return has_open_pr(session) or accepted or bool(session.get('blueprint')) or past_assessing


def is_waiting_on_review(session: dict[str, Any]) -> bool:
    """#171: waiting on a human review, whether or not a documenter side run is live for it.

    The review cap counts these. A side writer is not a reason to stop counting: a
    card that a side run admitted is still waiting on a person, so the cap must not
    free up because of it. A lifecycle performer is the only thing that moves a
    card off review.
    """
    card = session.get('current_card') or {}
    return (
        session.get('phase') == 'monitoring_pr'
        and session.get('review_wait') == REVIEW_WAIT_HUMAN
        and card.get('status') not in _RELEASED_STATUSES
        and not has_live_performer(session)
    )


def parked_in_review(sessions: dict[str, Any]) -> int:
    return sum(1 for session in sessions.values() if is_waiting_on_review(session))


def review_cap_allows_new_card(sessions: dict[str, Any], review_limit: int | None) -> bool:
    """May a card that has not started be admitted under ``max_cards_in_review``?

    The cap counts cards parked on a human review, as issue #179 words it. Cards
    that already hold a slot when it is reached still finish and park, so the
    number in review can exceed the cap by at most ``max_concurrent_cards - 1``.
    """
    return review_limit is None or parked_in_review(sessions) < review_limit


def configured_review_limit(config: Any) -> int | None:
    """The effective ``max_cards_in_review``, or None for no cap.

    It is never below ``max_concurrent_cards``: a deployment that runs five
    cards at once is not throttled to three by the default.
    """
    raw = getattr(config, 'max_cards_in_review', None)
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    limit = getattr(config, 'max_concurrent_cards', 1)
    return max(raw, limit) if isinstance(limit, int) and not isinstance(limit, bool) else raw


def review_cap_detail(review_limit: int | None, waiting: int) -> str:
    """The one wording for a card held back by ``max_cards_in_review`` (#149)."""
    return f"max_cards_in_review is {review_limit}: {waiting} waiting on review"


def _is_candidate(session: dict[str, Any]) -> bool:
    """A card the budget may admit: not released, not parked on review, not a held merge."""
    card = session.get('current_card') or {}
    return (
        card.get('status') not in _RELEASED_STATUSES
        and session.get('phase') not in _RELEASED_PHASES
        and not is_parked_in_review(session)
        and not is_released_merge(session)
    )


def select_pipelines(
    sessions: dict[str, Any], limit: int, eligible: set[str] | None = None,
    review_limit: int | None = None, *, admit_unstarted: bool = True,
) -> set[str]:
    """Keep running jobs safe, then retained reservations, then queued issues.

    One rule for a slot (#179, #186): a card holds a pipeline slot while it has
    work of ours to do or under way. Paused, idle, blocked and done issues
    release their reservation, and so does a card waiting on a human in review or
    a merge held past its bound. A card waiting on checks or on the daemon, or not
    evaluated yet, keeps it until the bound.

    Admission order is: running work, reservations already held, cards that have
    started and need a performer again, then cards that have not started, each
    in dictionary (FIFO) order. A returning card therefore goes ahead of new
    work but never displaces a card that already holds a slot.

    When ``review_limit`` cards are waiting on a human in review, cards that have
    not started are not admitted, so an absent reviewer cannot let open PRs pile
    up without bound. If running work already exceeds a reduced budget, it
    drains without admitting another issue.

    ``admit_unstarted=False`` is the first pass of a cycle (#186): only started
    work is selected, so a parked card's tick can still claim the slot when its
    feedback arrives. The fanout runs the full selection after that pass.
    """
    candidates = []
    running = []
    for cid, session in sessions.items():
        if has_live_work(session):
            running.append(cid)
        if (eligible is None or cid in eligible) and _is_candidate(session):
            candidates.append(cid)
    selected = set(running)
    retained = [cid for cid in candidates if sessions[cid].get('pipeline_admitted')]
    returning = [cid for cid in candidates if has_started(sessions[cid])]
    fresh = candidates if admit_unstarted and review_cap_allows_new_card(sessions, review_limit) else []
    order = [*retained, *returning, *fresh]
    if not admit_unstarted:
        order = [cid for cid in order if has_started(sessions[cid])]
    for cid in order:
        if len(selected) >= max(1, limit):
            break
        selected.add(cid)
    for cid, session in sessions.items():
        session['pipeline_admitted'] = cid in selected
    return selected


def admitted_side_runs(
    sessions: dict[str, Any], wanted: Collection[str], *, limit: int, eligible: Collection[str],
) -> set[str]:
    """#171: the cards wanting a documenter side run that may start this cycle.

    A documenter side run is a claim on a pipeline slot. The daemon passes only the
    cards whose run has already resolved, so a card that cannot resolve never holds
    a slot it will not use. Admission order matches ``select_pipelines``: running
    work, reservations already held, then cards that have started and need a
    performer again, then the side runs themselves. Fresh cards come after side
    runs, so a backlog cannot starve one, and a side run never displaces a
    lifecycle card that already has work.

    A card that already holds a slot shares it. A card whose lifecycle performer is
    live is never admitted, because its side run starts only after that performer
    has finished. A card left out waits and is asked again next cycle. Nothing is
    recorded against it.
    """
    if not wanted:
        return set()
    # The same eligible set select_pipelines is given: a paused or otherwise
    # ineligible card is never admitted, so it cannot hold a slot nobody will use.
    selected = {cid for cid, session in sessions.items() if has_live_work(session)}
    candidates = [cid for cid, session in sessions.items() if cid in eligible and _is_candidate(session)]
    retained = [cid for cid in candidates if sessions[cid].get('pipeline_admitted')]
    returning = [cid for cid in candidates if has_started(sessions[cid])]
    side = [
        cid for cid in sessions
        if cid in wanted and cid in eligible and cid not in selected and not has_live_performer(sessions[cid])
    ]
    for cid in [*retained, *returning, *side]:
        if len(selected) >= max(1, limit):
            break
        selected.add(cid)
    return {cid for cid in wanted if cid in selected and cid in eligible and not has_live_performer(sessions[cid])}


def dispatch_has_pipeline_slot(state: CoordinareState, card_id: str) -> bool:
    """Cover bootstrap/readoption and dispatch reached by blocked recovery too."""
    sessions = state.get('active_sessions') or {}
    if not sessions:
        return True  # Legacy single-card callers have no competing pipelines.
    raw = getattr(state.get('config'), 'max_concurrent_cards', 1)
    limit = max(1, raw if isinstance(raw, int) else 1)
    review_limit = configured_review_limit(state.get('config'))
    selected = state.get('_pipeline_selected')
    if selected is None:
        # A clarification can re-enter dispatch before its old session mirror
        # has been updated from BLOCKED. The dispatch node is the authoritative
        # transition; compare that candidate against all existing reservations.
        candidates = dict(sessions)
        candidate = dict(candidates.get(card_id) or {})
        candidate.update(current_card={'id': card_id, 'status': 'IN_PROGRESS'},
                         phase='dispatching')
        candidates[card_id] = candidate
        selected = select_pipelines(candidates, limit, review_limit=review_limit)
    elif card_id not in selected and len(selected) < limit and (
        has_started(sessions.get(card_id) or {})
        or review_cap_allows_new_card(sessions, review_limit)
    ):
        # Recovery may discover new eligible work during a graph tick, and a
        # card parked in review asks for its slot here when it needs a performer
        # again. This set is shared by fanout states; no await occurs while
        # claiming the slot. New work still respects the review cap.
        selected.add(card_id)
    admitted = card_id in selected
    state['pipeline_admitted'] = admitted
    return admitted


# Pickup counting also respects the latest board column, including restored sessions.
NON_SLOT_PHASES = frozenset({"monitoring_pr", "blocked", "idle", "done", "env_blocked"})


def session_consumes_slot(session: dict[str, Any], board_status: str | None = None) -> bool:
    """Quiescent history releases admission; owned writers keep their slot.

    Pickup counting also reads the latest board column, which the budget does
    not have. A session with no live work is then judged by its board column and its
    phase, the way the budget judges a candidate.
    """
    if has_live_work(session):
        return True
    phase = session.get("phase")
    card = session.get("current_card") or {}
    status = board_status if board_status is not None else card.get("status")
    if session.get("board_paused") or status in {"BACKLOG", "BLOCKED", "DONE", "CLOSED", "CANCELED"}:
        return False
    return board_status == "IN_PROGRESS" or phase not in NON_SLOT_PHASES


def count_slot_consuming_sessions(state: CoordinareState) -> int:
    """Count active sessions occupying a concurrency slot."""
    sessions: dict[str, Any] = state.get("active_sessions") or {}
    snapshot = state.get("board_snapshot") or {}
    live_statuses = {
        card_id: column
        for column, card_ids in snapshot.items()
        if isinstance(card_ids, list)
        for card_id in card_ids
    }

    def consumes_slot(card_id: str, session: dict[str, Any]) -> bool:
        card = session.get("current_card") or {}
        candidates = (card_id, str(card.get("id") or ""), str(card.get("content_id") or ""))
        status = next((live_statuses[candidate] for candidate in candidates if candidate in live_statuses), None)
        return session_consumes_slot(session, status)

    return sum(
        1 for card_id, sess in sessions.items()
        if consumes_slot(card_id, sess)
    )
