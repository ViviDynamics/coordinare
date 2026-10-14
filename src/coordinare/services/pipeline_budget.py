"""Reserve issue slots across performer stages, independently of role capacity."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState


def has_live_side_writer(session: dict[str, Any]) -> bool:
    """An owned side writer holds capacity until its termination is confirmed."""
    side = session.get('documenting_side') or {}
    return bool(side.get('session_id')) and bool(
        side.get('status') == 'running' or side.get('writer_active'),
    )


_RELEASED_STATUSES = ('BACKLOG', 'BLOCKED', 'DONE', 'CLOSED', 'CANCELED')
_RELEASED_PHASES = ('idle', 'blocked', 'env_blocked', 'done')


def has_live_performer(session: dict[str, Any]) -> bool:
    """A foreground performer is running for this card right now."""
    return session.get('phase') == 'monitoring_performer' and bool(
        (session.get('agent_dispatch') or {}).get('session_id'),
    )


def is_parked_in_review(session: dict[str, Any]) -> bool:
    """#179: waiting on a PR (review, checks) with nothing of ours running.

    A parked card holds no pipeline slot. It is still ticked every cycle so its
    merge evaluation runs (#102), and it asks for a slot again only when it
    needs a performer.
    """
    card = session.get('current_card') or {}
    return (
        session.get('phase') == 'monitoring_pr'
        and card.get('status') not in _RELEASED_STATUSES
        and not has_live_performer(session)
        and not has_live_side_writer(session)
    )


def has_open_pr(session: dict[str, Any]) -> bool:
    """The card has already produced a PR, so it is work in flight, not new work."""
    card = session.get('current_card') or {}
    return bool(card.get('pr_node_id') or card.get('pr_number') or card.get('pr_url'))


def parked_in_review(sessions: dict[str, Any]) -> int:
    return sum(1 for session in sessions.values() if is_parked_in_review(session))


def review_cap_allows_new_card(sessions: dict[str, Any], review_limit: int | None) -> bool:
    """May a card that has not started be admitted under ``max_cards_in_review``?

    The cap counts cards parked in review, as issue #179 words it. Cards that
    already hold a slot when it is reached still finish and park, so the number
    in review can exceed the cap by at most ``max_concurrent_cards - 1``.
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


def select_pipelines(
    sessions: dict[str, Any], limit: int, eligible: set[str] | None = None,
    review_limit: int | None = None,
) -> set[str]:
    """Keep running jobs safe, then retained reservations, then queued issues.

    One rule for a slot (#179): a card holds a pipeline slot while it has work
    of ours to do or under way. Paused, idle, blocked and done issues release
    their reservation, and so does a card parked in review. A card that is
    waiting for a performer keeps it.

    Admission order is: running work, reservations already held, cards with an
    open PR that need a performer again, then cards that have not started, each
    in dictionary (FIFO) order. A returning card therefore goes ahead of new
    work but never displaces a card that already holds a slot.

    When ``review_limit`` cards are parked in review, cards that have not
    started are not admitted, so an absent reviewer cannot let open PRs pile
    up without bound. If running work already exceeds a reduced budget, it
    drains without admitting another issue.
    """
    candidates = []
    running = []
    for cid, session in sessions.items():
        card = session.get('current_card') or {}
        if has_live_performer(session) or has_live_side_writer(session):
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
    returning = [cid for cid in candidates if has_open_pr(sessions[cid])]
    fresh = candidates if review_cap_allows_new_card(sessions, review_limit) else []
    for cid in [*retained, *returning, *fresh]:
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
        has_open_pr(sessions.get(card_id) or {})
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
