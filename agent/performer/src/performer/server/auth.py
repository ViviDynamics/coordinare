"""Bearer-token auth middleware for performer HTTP server (spec 056, T008).

When ``expected_token`` is None, auth is disabled and all requests pass through;
the ``/status`` payload exposes ``auth_enabled=False`` so operators can see this.

When ``expected_token`` is set, every request (other than the listed unauth paths)
must carry ``Authorization: Bearer <token>`` whose value matches exactly.
Mismatches return 401 with ``WWW-Authenticate: Bearer`` and a small JSON body.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

UNAUTH_PATHS: Final[frozenset[str]] = frozenset({"/healthz", "/livez"})


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Enforces ``Authorization: Bearer <token>`` when a token is configured."""

    def __init__(self, app: object, expected_token: str | None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._expected = expected_token

    @property
    def auth_enabled(self) -> bool:
        return self._expected is not None

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._expected is None or request.url.path in UNAUTH_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, self._expected):
            return JSONResponse(
                {"error": "unauthorized", "detail": "invalid or missing bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
