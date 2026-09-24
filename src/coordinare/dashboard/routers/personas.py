"""Persona read/write routes (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import (  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
        FastAPI,
        Request,
)
from fastapi.responses import JSONResponse, Response

from coordinare.dashboard.guards import _version_headers, _version_refusal

if TYPE_CHECKING:

        from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _register_get_personas(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.get("/api/personas")
        async def get_personas() -> JSONResponse:
            """Return effective instructions and is_default flag for all 8 roles.

            Reads config_path on every request for hot-reload behaviour.
            Falls back to the daemon's last-known config so the UI reflects the
            effective personas actually in use rather than bare defaults.
            """
            from coordinare.services.persona_service import (
                VALID_ROLES,
                get_effective_instructions,
                load_personas_hot,
            )

            personas = load_personas_hot(config_path, daemon.state.get("config"))

            result: list[dict[str, Any]] = []
            for role in sorted(VALID_ROLES):
                instructions = get_effective_instructions(role, personas)
                is_default = not getattr(personas, role).instructions.strip()
                result.append({"role": role, "instructions": instructions, "is_default": is_default})
            return JSONResponse(result, headers=_version_headers(config_path))


def _register_update_persona(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.put("/api/personas/{role}")
        async def update_persona(role: str, request: Request) -> JSONResponse:
            """Update persona instructions for a role. Returns 404 for unknown roles, 400 for oversized instructions."""
            import yaml

            from coordinare.config import PERSONA_MAX_LENGTH
            from coordinare.services.persona_service import (
                VALID_ROLES,
                save_persona,
            )

            if role not in VALID_ROLES:
                return JSONResponse({"error": f"Unknown role: {role!r}"}, status_code=404)

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

            if "instructions" not in body:
                return JSONResponse({"error": "Missing required field: instructions"}, status_code=400)
            raw_instructions = body["instructions"]
            if not isinstance(raw_instructions, str):
                return JSONResponse({"error": "instructions must be a string"}, status_code=400)
            instructions: str = raw_instructions
            if len(instructions) > PERSONA_MAX_LENGTH * 2 or len(instructions.strip()) > PERSONA_MAX_LENGTH:
                return JSONResponse(
                    {"error": f"Instructions exceed maximum length ({PERSONA_MAX_LENGTH} chars)"},
                    status_code=400,
                )

            if config_path is None or not config_path.is_file():
                return JSONResponse({"error": "Config file not found"}, status_code=500)


            refusal = _version_refusal(config_path, request.headers.get("If-Match"))
            if refusal is not None:
                return refusal
            try:
                save_persona(role, instructions, config_path)
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                # 158 (#241), sixth review round: save_persona begins with
                # `yaml.safe_load(config_path.read_text())`, so a config file that is not
                # valid YAML or not valid UTF-8 raises out of it. Neither ValueError nor
                # OSError below is a superclass of YAMLError, so that left the handler as
                # a 500 with a traceback -- while the symphony write, which wraps its
                # persist in `except Exception`, answered the same broken file with a
                # structured refusal. UnicodeDecodeError *is* a ValueError, and so was
                # being reported as a 400: a file corrupt on disk is not a bad request.
                from coordinare.services.config_write_service import safe_failure_reason

                _log.warning("persona_write_failed_unreadable", role=role, error=str(exc))
                return JSONResponse(
                    {"error": f"Failed to read config: {safe_failure_reason(exc)}"},
                    status_code=500,
                )
            except ValueError as exc:
                # ValueError here means malformed config shape (role/length already
                # validated above); treat as validation error per API contract. Logged
                # because the body is deliberately terse and an operator debugging a 400
                # otherwise has nothing.
                _log.warning("persona_write_rejected", role=role, error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=400)
            except OSError as exc:
                # Not the exception itself: str(OSError) carries the absolute path.
                from coordinare.services.config_write_service import safe_failure_reason

                _log.warning("persona_write_failed", error=str(exc))
                return JSONResponse(
                    {"error": f"Failed to write config: {safe_failure_reason(exc)}"},
                    status_code=500,
                )

            # Re-read from config to return what's actually stored/effective.
            from coordinare.services.persona_service import (
                get_effective_instructions,
                load_personas_hot,
            )

            personas = load_personas_hot(config_path, daemon.state.get("config"))
            effective = get_effective_instructions(role, personas)
            is_default = not getattr(personas, role).instructions.strip()
            return JSONResponse(
                {"role": role, "instructions": effective, "is_default": is_default},
                headers=_version_headers(config_path),
            )


def _register_reset_persona_endpoint(app: FastAPI, ctx: DashboardContext) -> None:
        config_path = ctx.config_path

        @app.delete("/api/personas/{role}", status_code=204)
        async def reset_persona_endpoint(role: str, request: Request) -> Response:
            """Reset a role's persona to built-in defaults (clears custom instructions)."""
            import yaml

            from coordinare.services.persona_service import VALID_ROLES, reset_persona

            if role not in VALID_ROLES:
                return JSONResponse({"error": f"Unknown role: {role!r}"}, status_code=404)

            if config_path is None or not config_path.is_file():
                return JSONResponse({"error": "Config file not found"}, status_code=500)


            refusal = _version_refusal(config_path, request.headers.get("If-Match"))
            if refusal is not None:
                return refusal
            try:
                reset_persona(role, config_path)
            except (yaml.YAMLError, UnicodeDecodeError) as exc:
                # The same hole as the save above, for the same reason: reset_persona
                # reads and re-dumps the same file.
                from coordinare.services.config_write_service import safe_failure_reason

                _log.warning("persona_reset_failed_unreadable", role=role, error=str(exc))
                return JSONResponse(
                    {"error": f"Failed to read config: {safe_failure_reason(exc)}"},
                    status_code=500,
                )
            except ValueError as exc:
                _log.warning("persona_reset_rejected", role=role, error=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=400)
            except OSError as exc:
                # Not the exception itself: str(OSError) carries the absolute path.
                from coordinare.services.config_write_service import safe_failure_reason

                _log.warning("persona_write_failed", error=str(exc))
                return JSONResponse(
                    {"error": f"Failed to write config: {safe_failure_reason(exc)}"},
                    status_code=500,
                )

            return Response(status_code=204, headers=_version_headers(config_path))


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_get_personas(app, ctx)
    _register_update_persona(app, ctx)
    _register_reset_persona_endpoint(app, ctx)
