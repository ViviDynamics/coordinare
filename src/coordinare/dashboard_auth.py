"""Optional shared-operator authentication for the dashboard (143)."""
from __future__ import annotations

import base64
import binascii
import hmac
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from coordinare.localhost_guard import MUTATING_METHODS, is_loopback_bind

if TYPE_CHECKING:
    from pydantic import SecretStr
    from starlette.types import ASGIApp, Receive, Scope, Send


def validate_dashboard_bind(host: str, token: SecretStr | None) -> None:
    """Refuse remote anonymous access before any daemon services are started."""
    if token is not None and len(token.get_secret_value()) < 32:
        raise ValueError("dashboard_auth_token must contain at least 32 characters")
    if not is_loopback_bind(host) and token is None:
        raise ValueError(
            "Non-loopback dashboard_host requires dashboard_auth_token. "
            "Configure a token or bind the dashboard to 127.0.0.1.",
        )


class DashboardAuthentication:
    """Protect every route, including SSE, without buffering requests/responses."""

    def __init__(
        self, app: ASGIApp, *, token: SecretStr,
        signed_webhook_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self._token = token.get_secret_value().encode("utf-8")
        self._signed_webhook_paths = signed_webhook_paths

    def _authenticated(self, value: str) -> bool:
        scheme, _, credential = value.partition(" ")
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(credential, validate=True)
            except (ValueError, binascii.Error):
                return False
            user, separator, password = decoded.partition(b":")
            return bool(separator) and user == b"operator" and hmac.compare_digest(password, self._token)
        return scheme.lower() == "bearer" and hmac.compare_digest(
            credential.encode("utf-8"), self._token,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.method == "POST" and request.url.path in self._signed_webhook_paths:
            # The only configured exemption is a route with its own HMAC verifier.
            await self.app(scope, receive, send)
            return
        if not self._authenticated(request.headers.get("authorization", "")):
            response = JSONResponse(
                {"detail": "Dashboard authentication required"}, status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="Coordinare", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if request.method in MUTATING_METHODS and origin is not None and origin != expected_origin:
            response = JSONResponse({"detail": "Cross-origin mutation refused"}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
