"""156: the per-symphony figures the Symphonies table shows, computed on the server.

Every function here reads only what the snapshot already holds: the symphony's config, its
env-cache state, its runtime sessions and the #134 spend ledger. None makes a GitHub call.
The browser renders the result; it does not compute spend, counts or env state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coordinare.lib.redaction import clip_session_error

ENV_NOT_CONFIGURED = "not_configured"
ENV_BOOTSTRAPPING = "bootstrapping"
ENV_FAILED = "failed"
ENV_READY = "ready"
ENV_NOT_BOOTSTRAPPED = "not_bootstrapped"

_NO_ERROR_TEXT = "the error was not recorded"


def env_status(
    *,
    configured: bool,
    in_flight: bool,
    succeeded: bool | None,
    cache_ready: bool,
    error: str | None,
) -> dict[str, str]:
    """The env column's state and the reason for it, in the order a reader needs them.

    ``configured`` is whether the symphony names an env bootstrap performer. Without one there
    is no manifest to bootstrap, so that is the reason, whatever the cache says.
    """
    if not configured:
        return {
            "state": ENV_NOT_CONFIGURED,
            "reason": "No env bootstrap performer is configured for this symphony.",
        }
    if in_flight:
        return {"state": ENV_BOOTSTRAPPING, "reason": "An env bootstrap is running."}
    if succeeded is False:
        return {
            "state": ENV_FAILED,
            "reason": clip_session_error(error) if error else _NO_ERROR_TEXT,
        }
    if cache_ready and succeeded is True:
        return {
            "state": ENV_READY,
            "reason": "The env cache is ready for this symphony's performers.",
        }
    return {"state": ENV_NOT_BOOTSTRAPPED, "reason": "No env bootstrap has finished yet."}


def session_counts(sessions: Mapping[str, Any]) -> dict[str, int]:
    """How many of one symphony's sessions are active, queued for a slot, and paused on the board.

    A queued session waits for a performer slot, so it is not counted as active. A paused
    session is counted in ``paused`` on its own, because a paused card can also be queued.
    """
    queued = 0
    paused = 0
    for session in sessions.values():
        if not isinstance(session, Mapping):
            continue
        if session.get("slot_queued_since"):
            queued += 1
        if session.get("board_paused"):
            paused += 1
    return {"active": len(sessions) - queued, "queued": queued, "paused": paused}


def week_spend(token_usage: Mapping[str, Any] | None, name: str) -> dict[str, Any]:
    """One symphony's spend this week, in the shape the cost cell reads.

    Only the figures keyed to ``name`` are returned. The deployment total and the other
    symphonies' figures never leave this function, so a scoped view cannot show them.
    """
    if not isinstance(token_usage, Mapping) or token_usage.get("tracked") is not True:
        reason = token_usage.get("reason") if isinstance(token_usage, Mapping) else None
        return {
            "tracked": False,
            "reason": str(reason or "Spend is not recorded."),
            "tokens": None,
            "cost": None,
        }
    by_symphony = token_usage.get("by_symphony")
    figures = by_symphony.get(name) if isinstance(by_symphony, Mapping) else None
    week = figures.get("week") if isinstance(figures, Mapping) else None
    if not isinstance(week, Mapping):
        return {
            "tracked": False,
            "reason": "No spend is recorded for this symphony.",
            "tokens": None,
            "cost": None,
        }
    return {"tracked": True, "reason": None, **dict(week)}
