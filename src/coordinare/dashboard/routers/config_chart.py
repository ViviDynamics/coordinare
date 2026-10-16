"""Authenticated chart adoption routes, using the dashboard's shared middleware."""

from __future__ import annotations

import os
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fastapi
import yaml
from fastapi.responses import JSONResponse

from coordinare.services.config_adoption import (
    AdoptionRefusalError,
    adopt_chart_sections,
    chart_preview,
)
from coordinare.services.config_ownership import SEED_ENV

if TYPE_CHECKING:
    from coordinare.dashboard.context import DashboardContext


def _paths(ctx: DashboardContext) -> tuple[Path, Path]:
    seed = os.environ.get(SEED_ENV)
    if not seed or ctx.config_path is None:
        message = "Persistent chart configuration is unavailable."
        raise AdoptionRefusalError(message, HTTPStatus.FORBIDDEN)
    return Path(seed), ctx.config_path


def register(app: fastapi.FastAPI, ctx: DashboardContext) -> None:
    router = fastapi.APIRouter()

    @router.get("/api/config/chart")
    async def preview_chart() -> JSONResponse:
        try:
            result = chart_preview(*_paths(ctx))
        except AdoptionRefusalError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            return JSONResponse(
                {"error": "Cannot read the chart seed or live configuration."},
                status_code=HTTPStatus.FORBIDDEN,
            )
        return JSONResponse(result, headers={"ETag": f'"{result["content_hash"]}"'})

    @router.post("/api/config/chart/adopt")
    async def adopt_chart(request: fastapi.Request) -> JSONResponse:
        expected_hash = request.headers.get("If-Match")
        if not expected_hash:
            return JSONResponse(
                {"error": "If-Match is required; review the chart diff first."},
                status_code=HTTPStatus.PRECONDITION_REQUIRED,
            )
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object."},
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        try:
            result: dict[str, Any] = adopt_chart_sections(*_paths(ctx), body, expected_hash)
        except AdoptionRefusalError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status)
        except (OSError, UnicodeError, ValueError, yaml.YAMLError):
            return JSONResponse(
                {"error": "Could not read or persist the configuration."},
                status_code=HTTPStatus.FORBIDDEN,
            )
        if result["applied"] == "hot_reloaded":
            try:
                ctx.daemon._config_reload_trigger.set()
                ctx.daemon._webhook_trigger.set()
            except Exception:
                result["applied"] = "staged_restart"
                result["message"] = "Saved to disk; restart the Pod to apply."
        return JSONResponse(result, headers={"ETag": f'"{result["new_hash"]}"'})

    app.include_router(router)
