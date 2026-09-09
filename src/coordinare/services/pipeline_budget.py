"""Reserve issue slots across performer stages, independently of role capacity."""
from __future__ import annotations

from typing import Any


def select_pipelines(
    sessions: dict[str, Any], limit: int, eligible: set[str] | None = None,
) -> set[str]:
    """Keep running jobs safe, then retained reservations, then queued issues.

    Dictionary order provides FIFO admission. Blocked/done issues release their
    reservation; waiting for a performer or PR review does not. If running work
    already exceeds a reduced budget, it drains without admitting another issue.
    """
    candidates = []
    running = []
    for cid, session in sessions.items():
        card = session.get('current_card') or {}
        live = session.get('phase') == 'monitoring_performer' and bool(
            (session.get('agent_dispatch') or {}).get('session_id')
        )
        if live:
            running.append(cid)
        if (eligible is None or cid in eligible) and card.get('status') not in (
            'BLOCKED', 'DONE', 'CLOSED', 'CANCELED',
        ) and session.get('phase') not in ('blocked', 'env_blocked', 'done'):
            candidates.append(cid)
    selected = set(running)
    retained = [cid for cid in candidates if sessions[cid].get('pipeline_admitted')]
    for cid in [*retained, *candidates]:
        if len(selected) >= max(1, limit):
            break
        selected.add(cid)
    for cid, session in sessions.items():
        session['pipeline_admitted'] = cid in selected
    return selected


def dispatch_has_pipeline_slot(state: dict[str, Any], card_id: str) -> bool:
    """Cover bootstrap/readoption and dispatch reached by blocked recovery too."""
    sessions = state.get('active_sessions') or {}
    if not sessions:
        return True  # Legacy single-card callers have no competing pipelines.
    raw = getattr(state.get('config'), 'max_concurrent_cards', 1)
    limit = max(1, raw if isinstance(raw, int) else 1)
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
        selected = select_pipelines(candidates, limit)
    elif card_id not in selected and len(selected) < limit:
        # Recovery may discover new eligible work during a graph tick. This set
        # is shared by fanout states; no await occurs while claiming the slot.
        selected.add(card_id)
    admitted = card_id in selected
    state['pipeline_admitted'] = admitted
    return admitted
