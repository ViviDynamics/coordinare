"""HTTP routes for the performer server (spec 056, T024).

Mirrors ``specs/056-performer-containerization/contracts/performer-http.openapi.yaml``:

- ``GET /status`` → ``PerformerStatus``
- ``POST /jobs`` → 202 ``JobAcceptResponse`` | 409 ``JobBusyResponse``
- ``GET /jobs/{job_id}`` → ``JobStatus`` | 404
- ``GET /jobs/{job_id}/stream`` → text/event-stream of ``JobStatus`` JSON
- ``POST /jobs/{job_id}/cancel`` → ``CancelResponse`` | 404

Health endpoints ``/healthz`` and ``/livez`` are unauthenticated (per
``server.auth.UNAUTH_PATHS``) and exist so external orchestrators can
probe liveness without a token.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from performer.server.job_runner import JobNotFoundError, JobRunner
from performer.server.models import (
    JobAcceptResponse,
    JobInitPayload,
    JobStatus,
    PerformerCapabilities,
    PerformerStatus,
)


class _RefreshSecretsPayload(BaseModel):
    """Body for PATCH /jobs/{job_id}/secrets — never logged."""

    secrets: dict[str, str] = Field(default_factory=dict)


def _runner(request: Request) -> JobRunner:
    return request.app.state.runner  # type: ignore[no-any-return]


def _capabilities(request: Request) -> PerformerCapabilities:
    return request.app.state.capabilities  # type: ignore[no-any-return]


def register_routes(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status", response_model=PerformerStatus)
    async def get_status(request: Request) -> PerformerStatus:
        runner = _runner(request)
        current_id = runner.current_job_id
        availability = "busy" if current_id is not None else "idle"
        return PerformerStatus(
            availability=availability,
            capabilities=_capabilities(request),
            auth_enabled=bool(request.app.state.auth_enabled),
            current_job_id=current_id,
            version=request.app.state.version,
        )

    @app.post("/jobs")
    async def submit_job(payload: JobInitPayload, request: Request) -> JSONResponse:
        runner = _runner(request)
        result = await runner.submit(payload)
        if isinstance(result, JobAcceptResponse):
            return JSONResponse(
                status_code=202,
                content=json.loads(result.model_dump_json()),
            )
        # T056: Map secret_missing reason to 422 status code
        status_code = 422 if result.reason == "secret_missing" else 409
        return JSONResponse(
            status_code=status_code,
            content=json.loads(result.model_dump_json()),
        )

    @app.get("/jobs/{job_id}", response_model=JobStatus)
    async def get_job(job_id: str, request: Request) -> JobStatus:
        try:
            return _runner(request).get(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/jobs/{job_id}/stream")
    async def stream_job(job_id: str, request: Request) -> StreamingResponse:
        runner = _runner(request)
        try:
            iterator = runner.stream(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

        async def event_stream() -> AsyncIterator[bytes]:
            async for status in iterator:
                payload = status.model_dump_json()
                yield f"data: {payload}\n\n".encode()

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.patch("/jobs/{job_id}/secrets", status_code=204)
    async def refresh_job_secrets(
        job_id: str, payload: _RefreshSecretsPayload, request: Request,
    ) -> Response:
        runner = _runner(request)
        try:
            await runner.refresh_secrets(job_id, payload.secrets)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        # 204 No Content — body intentionally empty so secrets never round-trip.
        return Response(status_code=204)

    @app.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request) -> JSONResponse:
        runner = _runner(request)
        try:
            response = await runner.cancel(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc
        return JSONResponse(
            status_code=200,
            content=json.loads(response.model_dump_json()),
        )


__all__ = ["register_routes"]
