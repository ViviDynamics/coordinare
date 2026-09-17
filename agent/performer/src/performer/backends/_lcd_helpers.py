"""Shared helpers for LCD-OpenAI-compatible performer backends (spec 067).

Performer-local mirror of constants/functions in ``coordinare.upstream_errors``.
Kept performer-side so adapters can build the ``upstream_http_error`` envelope
dict without importing from the coordinare package — spec 067 explicitly adds
no coordinare↔performer field (see plan.md "Dispatch payload"), so the two
copies stay in lockstep via the contract test
``tests/contract/test_067_upstream_http_error_schema.py``.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

BODY_CAP_BYTES: int = 2048
TRUNCATION_SUFFIX: str = "...<truncated>"

REDACTED: str = "***REDACTED***"

# Key-level denylist — case-insensitive substring match, only for unambiguous
# secret-key names long enough that real LCD payload fields (e.g.
# ``max_tokens`` contains ``token``) won't false-positive.
REDACT_KEY_SUBSTRINGS: tuple[str, ...] = (
    "api_key", "authorization", "openai_api_key", "secret", "password",
)
# Short tokens that *would* false-positive as substrings (``token`` matches
# ``max_tokens``). Matched only as the whole, lower-cased key.
REDACT_KEY_EXACT: frozenset[str] = frozenset({
    "token", "access_token", "refresh_token", "bearer", "bearer_token",
})
# Value-level pattern denylist — applies to string leaves.
SECRET_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "sk-***REDACTED***"),
    (re.compile(r"gho_[A-Za-z0-9_\-]{16,}"), "gho_***REDACTED***"),
    (re.compile(r"ghp_[A-Za-z0-9_\-]{16,}"), "ghp_***REDACTED***"),
    (re.compile(r"OPENAI_API_KEY=\S+"), "OPENAI_API_KEY=***REDACTED***"),
    # Bearer alphabet covers base64 / JWT bodies ('=', '+', '/' + URL-safe).
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.=+/]+"), "Bearer ***REDACTED***"),
    # Bare JWT shape (header.payload.signature) outside a Bearer prefix.
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-=+/]+\.[A-Za-z0-9_\-=+/]+\.[A-Za-z0-9_\-=+/]+"),
        "***REDACTED_JWT***",
    ),
)


def redact_request_body(body: Any) -> Any:
    """Return a redacted copy of ``body`` safe for debug logging.

    Walks dicts/lists, constructing new containers (input is never mutated —
    scalar leaves are returned unchanged by reference). Values whose **keys**
    match a denylist (exact short-token OR case-insensitive substring) are
    replaced with ``REDACTED``; string leaves matching known secret patterns
    are pattern-replaced. See research.md R5.
    """
    if isinstance(body, dict):
        out: dict[str, Any] = {}
        for k, v in body.items():
            klower = str(k).lower()
            if klower in REDACT_KEY_EXACT or any(
                pat in klower for pat in REDACT_KEY_SUBSTRINGS
            ):
                out[k] = REDACTED
            else:
                out[k] = redact_request_body(v)
        return out
    if isinstance(body, list):
        return [redact_request_body(v) for v in body]
    if isinstance(body, str):
        redacted = body
        for pat, repl in SECRET_VALUE_PATTERNS:
            redacted = pat.sub(repl, redacted)
        return redacted
    return body


def truncate_body(text: str) -> tuple[str, bool]:
    """Truncate ``text`` to ``BODY_CAP_BYTES`` (UTF-8), suffix included."""
    raw = text.encode("utf-8")
    if len(raw) <= BODY_CAP_BYTES:
        return text, False
    suffix = TRUNCATION_SUFFIX.encode("utf-8")
    keep = BODY_CAP_BYTES - len(suffix)
    truncated = raw[:keep].decode("utf-8", errors="ignore") + TRUNCATION_SUFFIX
    return truncated, True


def strip_base_url_credentials(url: str) -> str:
    """Remove userinfo + ``api_key`` query param from ``url`` before logging."""
    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if k.lower() != "api_key"],
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
