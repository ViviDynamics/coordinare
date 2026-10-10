"""Optional shared-operator authentication for the dashboard (143, 497)."""
from __future__ import annotations

import base64
import binascii
import hmac
from ipaddress import IPv6Address
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from coordinare.dashboard_oidc import SESSION_COOKIE
from coordinare.localhost_guard import MUTATING_METHODS, is_loopback_bind

if TYPE_CHECKING:
    from pydantic import SecretStr
    from starlette.types import ASGIApp, Receive, Scope, Send

    from coordinare.dashboard_oidc import OidcFlow

_OIDC_PUBLIC_PATHS = frozenset({"/oidc/login", "/oidc/callback", "/oidc/logout"})


def _normalized_origin(origin: str) -> str:
    parsed = urlparse(origin)
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port
    if port is None:
        port = default_port
    suffix = "" if port == default_port else f":{port}"
    hostname = (parsed.hostname or "").lower()
    if parsed.netloc.rsplit("@", 1)[-1].startswith("["):
        hostname = f"[{IPv6Address(hostname).compressed}]"
    return f"{parsed.scheme}://{hostname}{suffix}"


def _safe_normalized_origin(origin: str) -> str:
    """Malformed request origins never match the configured public origin."""
    try:
        return _normalized_origin(origin)
    except ValueError:
        return ""


def _public_origin(flow: OidcFlow) -> str:
    return _normalized_origin(flow.config.redirect_url)


def validate_dashboard_bind(
    host: str, token: SecretStr | None, *, oidc: OidcFlow | None = None,
) -> None:
    """Refuse remote anonymous access before any daemon services are started."""
    if token is not None and len(token.get_secret_value()) < 32:
        raise ValueError("dashboard_auth_token must contain at least 32 characters")
    if not is_loopback_bind(host) and token is None and oidc is None:
        raise ValueError(
            "Non-loopback dashboard_host requires dashboard_auth_token. "
            "Configure a token or bind the dashboard to 127.0.0.1.",
        )


class DashboardAuthentication:
    """Protect every route, including SSE, without buffering requests/responses.

    Spec 497: when an OIDC flow is configured, an unauthenticated *page load* is
    redirected to the provider instead of getting 401, and a valid session
    cookie authenticates exactly like the bearer token would. Token auth
    short-circuits; the session store is only consulted when the header carries
    no valid credential, so authenticated paths do no provider I/O.
    """

    def __init__(
        self, app: ASGIApp, *, token: SecretStr | None = None,
        oidc: OidcFlow | None = None,
        signed_webhook_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self._token = (
            token.get_secret_value().encode("utf-8") if token is not None else None
        )
        self._oidc = oidc
        self._signed_webhook_paths = signed_webhook_paths

    def _authenticated(self, value: str) -> bool:
        if self._token is None:
            return False
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

    def _session_authenticated(self, request: Request) -> bool:
        if self._oidc is None:
            return False
        value = request.cookies.get(SESSION_COOKIE, "")
        return bool(value) and self._oidc.sessions.lookup(value) is not None

    def _is_page_load(self, request: Request) -> bool:
        if request.method not in ("GET", "HEAD"):
            return False
        path = request.url.path
        return path != "/events" and not path.startswith("/api")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        if request.method == "POST" and path in self._signed_webhook_paths:
            # The only configured exemption is a route with its own HMAC verifier.
            await self.app(scope, receive, send)
            return
        if self._oidc is not None and path in _OIDC_PUBLIC_PATHS:
            # OIDC routes carry their own integrity checks (state/nonce pairing);
            # the localhost guard above still applies in full. Logout mutates
            # session state while public, so it keeps the middleware's
            # exact-origin rule; its Origin is the configured public origin,
            # which also survives a TLS-terminating proxy (scheme/hostname of
            # the configured redirect URL, default ports folded).
            if request.method == "POST" and path == "/oidc/logout":
                origin = request.headers.get("origin")
                if origin is not None and (
                    _safe_normalized_origin(origin) != _public_origin(self._oidc)
                ):
                    refused_response = JSONResponse(
                        {"detail": "Cross-origin logout refused"}, status_code=403,
                    )
                    await refused_response(scope, receive, send)
                    return
            await self.app(scope, receive, send)
            return
        if not (
            self._authenticated(request.headers.get("authorization", ""))
            or self._session_authenticated(request)
        ):
            if self._oidc is not None and self._is_page_load(request):
                redirect_response = RedirectResponse("/oidc/login", status_code=302)
                await redirect_response(scope, receive, send)
                return
            unauthorized_response = JSONResponse(
                {"detail": "Dashboard authentication required"}, status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="Coordinare", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
            await unauthorized_response(scope, receive, send)
            return
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.url.netloc}"
        if self._oidc is not None:
            expected_origin = _public_origin(self._oidc)
            if origin is not None:
                origin = _safe_normalized_origin(origin)
        if request.method in MUTATING_METHODS and origin is not None and origin != expected_origin:
            refused_response = JSONResponse(
                {"detail": "Cross-origin mutation refused"}, status_code=403,
            )
            await refused_response(scope, receive, send)
            return
        await self.app(scope, receive, send)
