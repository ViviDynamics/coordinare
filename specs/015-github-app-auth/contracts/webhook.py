"""
Contract: Webhook endpoint and HMAC verification.

Branch: 015-github-app-auth
"""
from __future__ import annotations

import asyncio


# ---------------------------------------------------------------------------
# HMAC verification (pure function — no I/O)
# ---------------------------------------------------------------------------

def verify_github_signature(body: bytes, secret: str, header: str | None) -> bool:
    """Return True if the X-Hub-Signature-256 header is valid for the given body.

    Uses stdlib hmac.compare_digest for constant-time comparison.

    Args:
        body    — raw request body bytes
        secret  — webhook secret string (from WebhookConfig.secret)
        header  — value of the X-Hub-Signature-256 HTTP header (may be None)

    Returns False (not raises) when the signature is absent or invalid.
    """
    ...


# ---------------------------------------------------------------------------
# FastAPI route factory
# ---------------------------------------------------------------------------

def register_webhook_route(app: object, path: str, secret: str, trigger: asyncio.Event) -> None:
    """Mount a POST route at `path` on the FastAPI `app`.

    The route:
    1. Reads the raw request body.
    2. Calls verify_github_signature; returns HTTP 401 on failure (logs warning).
    3. Sets `trigger` (asyncio.Event) to wake the daemon loop.
    4. Returns HTTP 200 with `{"status": "ok"}`.

    Concurrent-cycle safety: setting an asyncio.Event is idempotent — if the
    daemon is mid-cycle, the event remains set and wakes the next iteration.

    Args:
        app     — FastAPI application instance
        path    — URL path (e.g. "/webhook/github")
        secret  — HMAC secret string
        trigger — asyncio.Event to set on valid delivery
    """
    ...
