"""Confirmed cancellation of a card's foreground and parallel writers."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services.pipeline_budget import has_live_side_writer

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)


def has_owned_writers(session: dict[str, Any]) -> bool:
    return bool((session.get("agent_dispatch") or {}).get("session_id")) or has_live_side_writer(session)


async def _stop_owned_writer(state: CoordinareState, card_id: str, stage: Any, identity: dict[str, Any]) -> bool:
    service = (state.get("performer_services_by_id") or {}).get(str(identity.get("performer_id") or ""))
    if service is None:
        service = (state.get("performer_services") or {}).get(str(stage or ""))
    stop = getattr(service, "stop_session_confirmed", None)
    try:
        if callable(stop):
            return await asyncio.wait_for(stop(identity["session_id"]), timeout=30.0) is True
    except Exception as exc:
        logger.warning("daemon.board_pause_stop_failed", card_id=card_id, error_type=type(exc).__name__)
    return False


async def stop_owned_writers(state: CoordinareState, card_id: str, session: dict[str, Any], *, reason: str) -> bool:
    """Clear only identities whose owning service confirms termination."""
    dispatch = session.get("agent_dispatch") or {}
    writers = [(session.get("performer_stage"), dispatch, False)]
    if has_live_side_writer(session):
        writers.append(("documenting", session["documenting_side"], True))
    stopped = True
    for stage, identity, is_side in writers:
        if not identity.get("session_id"):
            continue
        if not await _stop_owned_writer(state, card_id, stage, identity):
            stopped = False
        elif is_side:
            # A poll already awaiting a status response must see supersession
            # rather than resurrect this confirmed-absent writer after resume.
            identity.update(status="failed", writer_active=False, result_reason=reason,
                            session_id=None, job_id=None)
        else:
            session["agent_dispatch"] = {}
            session["agent_dispatch_at"] = None
    return stopped
