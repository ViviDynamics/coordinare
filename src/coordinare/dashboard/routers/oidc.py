"""OIDC login routes (497): /oidc/login, /oidc/callback, /oidc/logout.

These routes are exempt from :class:`~coordinare.dashboard_auth.DashboardAuthentication`
(the middleware skips the three exact paths) but the localhost guard still
applies in full, so POST /oidc/logout keeps its same-origin check. Every
failure response is a fixed diagnostic; provider response bodies and
configuration secrets are never reflected.
"""
from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import structlog
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse

from coordinare.dashboard_oidc import (
    NONCE_COOKIE,
    SESSION_COOKIE,
    STATE_COOKIE,
    STATE_MAX_AGE_SECONDS,
    OidcLoginError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from coordinare.dashboard_oidc import OidcFlow

_log = structlog.get_logger(__name__)

_REFUSED_BODY = "Login refused. Check the daemon logs for details."
_UNAVAILABLE_BODY = "OIDC provider unavailable. Check the daemon logs for details."


def register_oidc_routes(app: FastAPI, flow: OidcFlow) -> None:
    secure = urlparse(flow.config.redirect_url).scheme == "https"

    @app.get("/oidc/login")
    async def oidc_login() -> Response:
        try:
            url, state, nonce = await flow.authorization_redirect()
        except OidcLoginError as exc:
            return _diagnostic(exc)
        response = RedirectResponse(url, status_code=302)
        _plant(response, STATE_COOKIE, state, secure)
        _plant(response, NONCE_COOKIE, nonce, secure)
        return response

    @app.get("/oidc/callback")
    async def oidc_callback(request: Request) -> Response:
        state = request.query_params.get("state")
        nonce = flow.consume_state(state)
        if nonce is None:
            return _refused("unknown or expired login state")
        if not secrets.compare_digest(
            request.cookies.get(STATE_COOKIE, ""), state or "",
        ):
            return _refused("login state cookie mismatch")
        cookie_nonce = request.cookies.get(NONCE_COOKIE, "")
        if not secrets.compare_digest(cookie_nonce, nonce):
            return _refused("login nonce cookie mismatch")
        try:
            subject = await flow.exchange(request.query_params.get("code", ""), nonce)
        except OidcLoginError as exc:
            return _diagnostic(exc)
        value, lifetime = flow.start_session(subject)
        response = RedirectResponse("/", status_code=302)
        response.set_cookie(
            SESSION_COOKIE, value, max_age=lifetime, httponly=True,
            samesite="lax", path="/", secure=secure,
        )
        response.delete_cookie(STATE_COOKIE, path="/oidc")
        response.delete_cookie(NONCE_COOKIE, path="/oidc")
        _log.info("dashboard_oidc.login_established", subject=subject)
        return response

    @app.post("/oidc/logout")
    async def oidc_logout(request: Request) -> Response:
        value = request.cookies.get(SESSION_COOKIE, "")
        if value:
            flow.sessions.revoke(value)
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/")
        _log.info("dashboard_oidc.logout")
        return response


def _plant(
    response: Response, name: str, value: str, secure: bool,
) -> None:
    response.set_cookie(
        name, value, max_age=STATE_MAX_AGE_SECONDS, httponly=True,
        samesite="lax", path="/oidc", secure=secure,
    )


def _refused(reason: str) -> PlainTextResponse:
    _log.info("dashboard_oidc.login_refused", reason=reason)
    return PlainTextResponse(_REFUSED_BODY, status_code=403)


def _diagnostic(exc: OidcLoginError) -> PlainTextResponse:
    _log.warning(
        "dashboard_oidc.provider_problem",
        reason=exc.reason, provider_unreachable=exc.provider_unreachable,
    )
    status = 502 if exc.provider_unreachable else 403
    return PlainTextResponse(
        _UNAVAILABLE_BODY if exc.provider_unreachable else _REFUSED_BODY,
        status_code=status,
    )
