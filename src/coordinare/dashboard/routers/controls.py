"""Performer control action routes (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import structlog
from fastapi import (
        FastAPI,  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
)
from fastapi.responses import JSONResponse

from coordinare.dashboard.helpers import _ACTIVE_PHASES

if TYPE_CHECKING:

        from typing import Any

        from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _control_sessions(state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Read working sessions for the current symphony and runtime-owned peers."""
    flat_sessions = state.get("active_sessions") or {}
    runtimes = state.get("symphony_states") or {}
    sessions: dict[str, Any] = {}
    owners: dict[str, list[str]] = {}
    if runtimes:
        for name, runtime in runtimes.items():
            for cid, session in (getattr(runtime, "active_sessions", None) or {}).items():
                owners.setdefault(cid, []).append(name)
                sessions[cid] = session
        # During a symphony tick the flat map is its live working map; other
        # symphonies retain ownership in their runtime, not a stale aggregate.
        current = state.get("current_symphony")
        if current is not None:
            for cid, session in flat_sessions.items():
                if cid not in owners or owners[cid] == [current]:
                    sessions[cid] = session
            for cid in list(sessions):
                if owners.get(cid) == [current] and cid not in flat_sessions:
                    del sessions[cid]
    else:
        sessions = flat_sessions
    return sessions, owners


def _eligible_control_session(cid: str, session: Any, owners: dict[str, list[str]]) -> bool:
    card = session.get("current_card") if isinstance(session, dict) else None
    return (
        isinstance(session, dict) and session.get("phase") in _ACTIVE_PHASES
        and not session.get("board_paused") and isinstance(card, dict)
        and card.get("id") == cid and len(owners.get(cid, [])) <= 1
    )


def _legacy_transient_owner(state: dict[str, Any], sessions: dict[str, Any]) -> dict[str, Any] | None:
    """A legacy graph may materialize its sole owner before flat writeback."""
    if state.get("symphony_states") or len(sessions) != 1 or state.get("phase") not in _ACTIVE_PHASES:
        return None
    cid, session = next(iter(sessions.items()))
    if not isinstance(session, dict) or session.get("phase") is not None:
        return None
    if (state.get("current_card") or {}).get("id") != cid:
        return None
    if not _eligible_control_session(cid, {**session, "phase": state["phase"]}, {}):
        return None
    return session


def _control_target(state: dict[str, Any], card_id: str | None) -> dict[str, Any] | JSONResponse:
    """Resolve an eligible owning session without guessing between live cards."""
    sessions, owners = _control_sessions(state)
    transient = _legacy_transient_owner(state, sessions)

    if card_id is not None:
        target = sessions.get(card_id)
        if not _eligible_control_session(card_id, target, owners) and (transient is None or target is not transient):
            return JSONResponse({"error": "No eligible active card for this target"}, status_code=400)
        return cast("dict[str, Any]", target)
    live = [session for cid, session in sessions.items() if _eligible_control_session(cid, session, owners)]
    if transient is not None:
        live.append(transient)
    if len(live) > 1:
        return JSONResponse({"error": "Multiple active cards; specify card_id"}, status_code=409)
    if live:
        return cast("dict[str, Any]", live[0])
    # Preserve the original single-card API only when no session map owns work.
    if not sessions and not state.get("symphony_states") and state.get("phase") in _ACTIVE_PHASES:
        return state
    return JSONResponse({"error": "No active card to override"}, status_code=400)


def _queue_override(target: dict[str, Any], override: dict[str, Any]) -> JSONResponse | None:
    queued_control = target.get("pending_override")
    if isinstance(queued_control, dict) and queued_control and not queued_control.get("applied"):
        return JSONResponse({"error": "A control is already pending for this card"}, status_code=409)
    # Identical repeated commands are separate human decisions, including in
    # flat state. The receipt distinguishes a fresh request from one consumed.
    override["control_id"] = uuid4().hex
    target["pending_override"] = override
    return None


def _register_skip_role(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/skip-role")
        async def skip_role(card_id: str | None = None) -> JSONResponse:
            """Queue a skip-role override for the next graph cycle (031)."""
            target = _control_target(daemon.state, card_id)
            if isinstance(target, JSONResponse):
                return target
            conflict = _queue_override(target, {"action": "skip"})
            if conflict is not None:
                return conflict
            return JSONResponse({"status": "override_queued", "action": "skip"})


def _register_restart_from(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/restart-from/{role}")
        async def restart_from(role: str, card_id: str | None = None) -> JSONResponse:
            """Queue a restart-from override for the next graph cycle (031).

            Accepts both role nouns (e.g. ``architect``) and stage names
            (e.g. ``architecting``).
            """
            target = _control_target(daemon.state, card_id)
            if isinstance(target, JSONResponse):
                return target
            lifecycle = list(daemon.state.get("lifecycle_sequence") or [])
            # Accept role nouns (architect) as well as stage names (architecting)
            from coordinare.graph.nodes.classify_human_feedback import _resolve_stage
            resolved = _resolve_stage(role, lifecycle)
            if resolved not in lifecycle:
                return JSONResponse(
                    {"error": f"Role {role!r} not in lifecycle: {lifecycle}"},
                    status_code=400,
                )
            conflict = _queue_override(target, {"action": "restart", "target_stage": resolved})
            if conflict is not None:
                return conflict
            return JSONResponse({"status": "override_queued", "action": "restart", "target_stage": resolved})


def _register_veto(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/veto")
        async def veto(card_id: str | None = None) -> JSONResponse:
            """Queue a veto override for the next graph cycle (031)."""
            target = _control_target(daemon.state, card_id)
            if isinstance(target, JSONResponse):
                return target
            conflict = _queue_override(target, {"action": "veto"})
            if conflict is not None:
                return conflict
            return JSONResponse({"status": "override_queued", "action": "veto"})


def _register_dry_run_card(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/dry-run/{card_id}")
        async def dry_run_card(card_id: str) -> JSONResponse:
            """Run a dry-run preview for a card and return the result as JSON (038).

            Builds the lifecycle plan without making any GitHub API calls or
            spawning any performers.
            """
            from coordinare.dry_run import execute_dry_run

            cfg = daemon.state.get("config")
            if cfg is None:
                return JSONResponse({"error": "Config not available"}, status_code=500)

            try:
                result = await execute_dry_run(card_id, cfg)
            except Exception:
                _log.exception("dry_run.api_error", card_id=card_id)
                return JSONResponse({"error": "Internal error during dry-run"}, status_code=500)

            return JSONResponse(result.model_dump())


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_skip_role(app, ctx)
    _register_restart_from(app, ctx)
    _register_veto(app, ctx)
    _register_dry_run_card(app, ctx)
