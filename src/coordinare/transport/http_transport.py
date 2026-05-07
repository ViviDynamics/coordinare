"""HTTP client for the containerized performer job protocol (spec 056, T012).

Speaks the protocol defined in
``specs/056-performer-containerization/contracts/performer-http.openapi.yaml``.
Distinct from ``AgentTransport`` — the existing protocol carries a single
``ProtocolMessage``; the spec-056 protocol is a request/response/poll job
lifecycle (``POST /jobs`` → ``GET /jobs/{id}`` → optional ``POST /jobs/{id}/cancel``).

This module is the coordinare-side client only; the performer-side server lives
in ``agent/performer/src/performer/server/``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from coordinare.models.performer_endpoint import (
    CancelResponse,
    JobAcceptResponse,
    JobBusyResponse,
    JobInitPayload,
    JobStatus,
    PerformerStatus,
)
from coordinare.transport.base import TransportError, TransportTimeoutError

logger = structlog.get_logger(__name__)


class PerformerUnreachableError(TransportError):
    """Raised when the performer endpoint cannot be contacted."""


class PerformerAuthError(TransportError):
    """Raised on 401 from the performer."""


class PerformerHTTPClient:
    """Thin async HTTP client for one performer endpoint.

    A single instance is bound to one ``base_url`` + optional ``auth_token``.
    The client owns a single ``httpx.AsyncClient`` for connection reuse; call
    :meth:`aclose` on shutdown.
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: str | None = None,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token
        self._timeout = timeout_seconds
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if self._auth_token:
            return {"Authorization": f"Bearer {self._auth_token}"}
        return {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.request(
                method, url, headers=self._headers(), json=json
            )
        except httpx.TimeoutException as exc:
            raise TransportTimeoutError(self._timeout) from exc
        except httpx.HTTPError as exc:
            raise PerformerUnreachableError(
                f"performer at {self._base_url} unreachable: {exc}"
            ) from exc
        if response.status_code in {401, 403}:
            raise PerformerAuthError(f"{response.status_code} from {self._base_url}{path}")
        return response

    async def get_status(self) -> PerformerStatus:
        r = await self._request("GET", "/status")
        r.raise_for_status()
        return PerformerStatus.model_validate(r.json())

    async def post_job(
        self, payload: JobInitPayload
    ) -> JobAcceptResponse | JobBusyResponse:
        body = payload.model_dump(mode="json", exclude_none=True)
        # SecretStr serializes as "**********" in JSON mode; unwrap to real values.
        if payload.secrets:
            body["secrets"] = {k: v.get_secret_value() for k, v in payload.secrets.items()}
        r = await self._request("POST", "/jobs", json=body)
        if r.status_code == 202:
            return JobAcceptResponse.model_validate(r.json())
        if r.status_code == 409:
            return JobBusyResponse.model_validate(r.json())
        if r.status_code == 422:
            # 422 signals a permanent config error (e.g. secret_missing) — raise
            # so the caller can block the card rather than retrying every cycle.
            try:
                _body = r.json()
                detail = _body.get("detail") or _body.get("reason", "unknown")
            except Exception:
                detail = r.text or "unknown"
            raise PerformerAuthError(f"permanent performer config error: {detail}")
        r.raise_for_status()
        raise TransportError(f"unexpected POST /jobs status {r.status_code}")

    async def get_job(self, job_id: str) -> JobStatus:
        r = await self._request("GET", f"/jobs/{job_id}")
        r.raise_for_status()
        return JobStatus.model_validate(r.json())

    async def cancel_job(self, job_id: str) -> CancelResponse:
        r = await self._request("POST", f"/jobs/{job_id}/cancel")
        r.raise_for_status()
        return CancelResponse.model_validate(r.json())

    async def stream_job(self, job_id: str) -> AsyncIterator[JobStatus]:
        """Yield ``JobStatus`` objects parsed from the SSE stream until terminal."""
        url = f"{self._base_url}/jobs/{job_id}/stream"
        try:
            async with self._client.stream(
                "GET", url, headers=self._headers()
            ) as response:
                if response.status_code in {401, 403}:
                    raise PerformerAuthError(f"{response.status_code} from {url}")
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: ") :]
                    yield JobStatus.model_validate(json.loads(payload))
        except httpx.TimeoutException as exc:
            raise TransportTimeoutError(self._timeout) from exc
        except httpx.HTTPError as exc:
            raise PerformerUnreachableError(
                f"performer at {self._base_url} unreachable: {exc}"
            ) from exc

    async def __aenter__(self) -> PerformerHTTPClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def utc_now() -> datetime:
    """Module-level helper for tests; isolated for monkeypatching."""
    return datetime.now(UTC)
