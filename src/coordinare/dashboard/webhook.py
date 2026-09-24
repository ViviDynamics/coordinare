"""GitHub webhook signature verification and route registration (436)."""
from __future__ import annotations

import asyncio  # noqa: TC003 — annotation-only today, but cheap and matches module style

import structlog
from fastapi import (  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse

_log = structlog.get_logger(__name__)


def verify_github_signature(body: bytes, secret: str, header: str | None) -> bool:
    """Return True iff the X-Hub-Signature-256 header matches the HMAC-SHA256 of body.

    Uses stdlib hmac + hashlib; constant-time comparison via hmac.compare_digest.
    Returns False (never raises) when header is missing or malformed.
    """
    import hashlib
    import hmac

    if not header:
        return False
    if not header.startswith("sha256="):
        return False
    expected_sig = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_sig, header)

def register_webhook_route(
    app: FastAPI,
    path: str,
    secret: str,
    trigger: asyncio.Event,
) -> None:
    """Register a POST route on *app* that validates GitHub webhook delivery.

    Valid deliveries set *trigger* and return HTTP 200 ``{"status": "ok"}``.
    Invalid/missing signatures return HTTP 401 and are structured-logged.
    """
    from fastapi import Response

    @app.post(path, include_in_schema=False)
    async def _webhook_handler(request: Request) -> Response:
        body = await request.body()
        sig_header = request.headers.get("X-Hub-Signature-256")
        if not verify_github_signature(body, secret, sig_header):
            _log.warning(
                "webhook_signature_invalid",
                path=path,
                sig_header=sig_header,
            )
            return Response(content='{"error":"invalid signature"}', status_code=401, media_type="application/json")
        trigger.set()
        _log.info("webhook_received", path=path)
        return JSONResponse({"status": "ok"})
