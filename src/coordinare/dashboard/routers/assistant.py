"""Config-assistant routes, conditionally registered (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING

import structlog
from fastapi import (  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
        FastAPI,
        Request,
)
from fastapi.responses import JSONResponse

if TYPE_CHECKING:

        from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _register_assistant_status(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        if not ctx.assistant_on:
            return

        @app.get("/api/assistant/status")
        async def assistant_status() -> JSONResponse:
            """Whether the panel can be used, and if not, why."""
            from coordinare.services.config_assistant import opening_guidance

            backend = daemon.state.get("conducting_backend")
            cfg = daemon.state.get("coordinare_config")
            return JSONResponse(
                {
                    "enabled": True,
                    "ready": backend is not None,
                    "reason": None if backend is not None else "no conducting backend configured",
                    # 155: the panel opens on whatever is actually missing rather than
                    # on a blank prompt, which is the same problem as the YAML.
                    "opening": opening_guidance(cfg) if cfg is not None else "",
                },
            )


def _register_assistant_message(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        if not ctx.assistant_on:
            return

        @app.post("/api/assistant/message")
        async def assistant_message(request: Request) -> JSONResponse:
            """One turn. The conversation lives in the client, so nothing is stored.

            Session-scoped memory (FR-015) is structural here rather than a policy
            about a cache: the server keeps nothing between requests, so there is
            nothing to persist, expire, or leak into a later conversation.
            """
            from coordinare.services.config_assistant import run_turn

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                return JSONResponse({"error": "message is required"}, status_code=400)

            history = body.get("history")
            if not isinstance(history, list):
                history = []

            coordinare_cfg = daemon.state.get("coordinare_config")
            if coordinare_cfg is None:
                return JSONResponse({"error": "configuration is not loaded"}, status_code=503)

            turn = await run_turn(
                message=message,
                history=[h for h in history if isinstance(h, dict)][-20:],
                config=coordinare_cfg,
                backend=daemon.state.get("conducting_backend"),
            )

            proposal = None
            if turn.proposal is not None:
                proposal = {
                    "section": turn.proposal.section,
                    "values": turn.proposal.values,
                    "reason": turn.proposal.reason,
                }

            return JSONResponse(
                {"reply": turn.reply, "proposal": proposal, "error": turn.error},
            )


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_assistant_status(app, ctx)
    _register_assistant_message(app, ctx)
