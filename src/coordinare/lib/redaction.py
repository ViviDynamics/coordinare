from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = {
    "token",
    "password",
    "secret",
    "api_key",
    "webhook_url",
    "authorization",
    "secrets",  # JobInitPayload.secrets dict
    "auth_token",  # PerformerEndpointConfig.auth_token
}

# Value-based redaction.  Used when untrusted text (e.g. performer stdout
# bleeding into transport logs) may contain credentials.  Mirrors the
# performer-side patterns in ``agent/performer/src/performer/models.py`` —
# keep these lists in sync when adding new token formats.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),      # fine-grained PAT
    re.compile(r"ghp_[A-Za-z0-9]{36}"),               # classic PAT
    re.compile(r"gho_[A-Za-z0-9]{36}"),               # OAuth app token
    re.compile(r"ghs_[A-Za-z0-9]{36}"),               # App installation token
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"),        # Anthropic API key
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}"),  # Bearer header value
    re.compile(r"AKIA[0-9A-Z]{16}"),                   # AWS access key
]


def redact_secrets(text: str) -> str:
    """Replace any recognised token/credential substrings with ``[REDACTED]``.

    Complements ``redact_mapping`` (key-based) for the case where secrets
    appear inside arbitrary text values rather than in fields with known
    sensitive keys — e.g. a non-JSON stdout line from a subprocess.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _is_sensitive_key(key: str) -> bool:
    return key.lower() in SENSITIVE_KEYS


def redact_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value


def redact_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if _is_sensitive_key(key):
            redacted[key] = "***REDACTED***"
        else:
            redacted[key] = redact_value(value)
    return redacted

