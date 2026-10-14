"""Reserve issue slots across performer stages, independently of role capacity."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

import structlog

if TYPE_CHECKING:
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


def utc_now() -> datetime:
    """The clock for hold bounds. One seam, so a test can cross the bound."""
    return datetime.now(UTC)


def configured_hold_seconds(config: Any) -> int:
    """``review_slot_hold_seconds``, or the default when the config does not carry it."""
    raw = getattr(config, 'review_slot_hold_seconds', None)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else REVIEW_SLOT_HOLD_SECONDS_DEFAULT


def expire_review_holds(sessions: dict[str, Any], *, hold_seconds: int) -> list[str]:
    """#186: a non-human hold in monitoring_pr that outlives its bound becomes a human wait.

    A card that is not waiting on a human, and has no live work, may keep its slot
    only while the daemon is about to act on it. The first sighting stamps
    ``review_wait_since`` (a card never evaluated, or restored after a restart, is
    timed from there). Past ``hold_seconds`` the card is recorded as waiting on a
    human: it releases its slot, still counts toward the review cap, is still ticked
    every cycle, and regains a slot through the returning path when it needs a
    performer. Logged once per expiry.
    """
    now = utc_now()
    expired: list[str] = []
    for cid, session in sessions.items():
        if session.get('phase') != 'monitoring_pr' or has_live_work(session):
            continue
        if session.get('review_wait') == REVIEW_WAIT_HUMAN:
            continue
        since = session.get('review_wait_since')
        if not isinstance(since, datetime):
            session['review_wait_since'] = now
            continue
        held = (now - since).total_seconds()
        if held <= hold_seconds:
            continue
        reason = session.get('review_wait_reason') or REVIEW_WAIT_UNEVALUATED
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
    parked: it keeps its reservation.
    """
    card = session.get('current_card') or {}
    return (
        session.get('phase') == 'monitoring_pr'
        and session.get('review_wait') == REVIEW_WAIT_HUMAN
        and card.get('status') not in _RELEASED_STATUSES
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


def parked_in_review(sessions: dict[str, Any]) -> int:
    return sum(1 for session in sessions.values() if is_parked_in_review(session))


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


def select_pipelines(
    sessions: dict[str, Any], limit: int, eligible: set[str] | None = None,
    review_limit: int | None = None, *, admit_unstarted: bool = True,
) -> set[str]:
    """Keep running jobs safe, then retained reservations, then queued issues.

    One rule for a slot (#179, #186): a card holds a pipeline slot while it has
    work of ours to do or under way. Paused, idle, blocked and done issues
    release their reservation, and so does a card waiting on a human in review.
    A card waiting on checks or on the daemon, or not evaluated yet, keeps it.

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
        card = session.get('current_card') or {}
        if has_live_work(session):
            running.append(cid)
        if (
            (eligible is None or cid in eligible)
            and card.get('status') not in _RELEASED_STATUSES
            and session.get('phase') not in _RELEASED_PHASES
            and not is_parked_in_review(session)
        ):
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
