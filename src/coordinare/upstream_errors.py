"""UpstreamHTTPError envelope — cross-boundary contract (spec 067).

Performer-side adapters emit this envelope on non-2xx responses from the model
endpoint; the coordinare receives it embedded inside
``PerformerResponse.metrics.upstream_http_error`` and uses it to classify
transient vs. permanent failures (FR-005) and to log the verbatim upstream
body (FR-004).

Contract pinned at ``specs/067-compatibility-first-backend/contracts/upstream_http_error.md``.
"""
from __future__ import annotations

from datetime import datetime  # noqa: TC003 — runtime use by pydantic v2
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

BODY_CAP_BYTES: int = 2048
TRUNCATION_SUFFIX: str = "...<truncated>"


class UpstreamHTTPError(BaseModel):
    """Verbatim upstream non-2xx response envelope. See contract for field rules."""

    kind: Literal["upstream_http_error"] = "upstream_http_error"
    status: int = Field(ge=100, le=599)
    body: str
    body_truncated: bool
    route: str
    base_url: str
    upstream_request_id: str | None = None
    elapsed_ms: int = Field(ge=0)
    occurred_at: datetime


def strip_base_url_credentials(base_url: str) -> str:
    """Remove userinfo and `api_key` query param from a base URL before logging."""
    parts = urlsplit(base_url)
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() != "api_key"
        ],
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def truncate_body(raw: str) -> tuple[str, bool]:
    """Truncate *raw* to BODY_CAP_BYTES; return (body, was_truncated).

    The suffix is included in the cap.
    """
    encoded = raw.encode("utf-8")
    if len(encoded) <= BODY_CAP_BYTES:
        return raw, False
    keep = BODY_CAP_BYTES - len(TRUNCATION_SUFFIX.encode("utf-8"))
    truncated = encoded[:keep].decode("utf-8", errors="ignore")
    return truncated + TRUNCATION_SUFFIX, True
