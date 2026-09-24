"""Performer control action routes (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING

import structlog
from fastapi import (
        FastAPI,  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
)
from fastapi.responses import JSONResponse

from coordinare.dashboard.helpers import _ACTIVE_PHASES

if TYPE_CHECKING:

        from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _register_skip_role(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/skip-role")
        async def skip_role() -> JSONResponse:
            """Queue a skip-role override for the next graph cycle (031)."""
            if daemon.state.get("phase") not in _ACTIVE_PHASES:
                return JSONResponse({"error": "No active card to override"}, status_code=400)
            daemon.state["pending_override"] = {"action": "skip"}
            return JSONResponse({"status": "override_queued", "action": "skip"})


def _register_restart_from(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/restart-from/{role}")
        async def restart_from(role: str) -> JSONResponse:
            """Queue a restart-from override for the next graph cycle (031).

            Accepts both role nouns (e.g. ``architect``) and stage names
            (e.g. ``architecting``).
            """
            if daemon.state.get("phase") not in _ACTIVE_PHASES:
                return JSONResponse({"error": "No active card to override"}, status_code=400)
            lifecycle = list(daemon.state.get("lifecycle_sequence") or [])
            # Accept role nouns (architect) as well as stage names (architecting)
            from coordinare.graph.nodes.classify_human_feedback import _resolve_stage
            resolved = _resolve_stage(role, lifecycle)
            if resolved not in lifecycle:
                return JSONResponse(
                    {"error": f"Role {role!r} not in lifecycle: {lifecycle}"},
                    status_code=400,
                )
            daemon.state["pending_override"] = {"action": "restart", "target_stage": resolved}
            return JSONResponse({"status": "override_queued", "action": "restart", "target_stage": resolved})


def _register_veto(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/veto")
        async def veto() -> JSONResponse:
            """Queue a veto override for the next graph cycle (031)."""
            if daemon.state.get("phase") not in _ACTIVE_PHASES:
                return JSONResponse({"error": "No active card to override"}, status_code=400)
            daemon.state["pending_override"] = {"action": "veto"}
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
