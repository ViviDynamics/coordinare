from __future__ import annotations

import logging
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
    re.compile(r"https://hooks\.slack(?:-gov)?\.com/services/[^\s\"\'<>]+"),
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


# 148: shell commands and tool output carry secrets in forms the value patterns
# above do not match. Each pattern keeps its prefix in group 1 and replaces the
# secret span, so one substitution serves every form. The performer keeps an
# identical list (agent/performer/src/performer/models.py); a parity test pins it.
_COMMAND_MARKERS = ("=", ":", "-", "@", "configure")
_QUOTED_OR_BARE = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_COMMAND_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # FOO_TOKEN=abc, PGPASSWORD=x, AWS_SECRET_ACCESS_KEY=...
    re.compile(
        r"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|AUTH|CREDENTIAL)[A-Za-z0-9_]*=)"
        + _QUOTED_OR_BARE,
        re.IGNORECASE,
    ),
    # --token x, --password=x, --api-key x
    re.compile(
        r"((?:^|\s)--?(?:token|password|passwd|pass|secret|api[-_]?key)[=\s]+)" + _QUOTED_OR_BARE,
        re.IGNORECASE,
    ),
    # docker login -p x, sshpass -p x, mysql -pVALUE (the password follows -p, attached or not)
    re.compile(
        r"(\b(?:docker\s+login|mysql|mariadb|psql|sshpass|mongosh?)\b[^|;&\n]*?\s-p)\s*"
        + _QUOTED_OR_BARE,
        re.IGNORECASE,
    ),
    # curl -u user:pass, --user user:pass (only the password after the colon)
    re.compile(r"((?:^|\s)(?:-u|--user)(?:=|\s+)[\"']?[^\s:\"']*:)[^\s\"']+", re.IGNORECASE),
    # scheme://user:pass@host, any scheme (https, postgresql, x-access-token, ...)
    re.compile(r"\b((?:[A-Za-z][A-Za-z0-9+.\-]*://)[^\s/:@'\"]*:)[^\s/@'\"]+(?=@)"),
    # -H 'X-API-Key: v', -H "Token: v", Authorization: Bearer v
    re.compile(
        r"((?:^|[\s\"'])(?:-H\s*)?[\"']?[A-Za-z0-9-]*(?:key|token|secret|auth|password|credential)"
        r"[A-Za-z0-9-]*\s*:\s*(?:(?:bearer|basic|token)\s+)?)[^\s\"']+",
        re.IGNORECASE,
    ),
    # aws configure set aws_secret_access_key VALUE (no equals sign)
    re.compile(
        r"(\baws\s+configure\s+set\s+\S*(?:secret|token|password)\S*\s+)\S+",
        re.IGNORECASE,
    ),
    # gh secret set NAME --body VALUE
    re.compile(
        r"(\bgh\s+secret\s+set\b[^|;&\n]*?\s(?:--body|-b)[=\s]+)" + _QUOTED_OR_BARE,
        re.IGNORECASE,
    ),
]


def redact_command_text(text: str) -> str:
    """``redact_secrets`` plus the command and header forms it misses (148).

    Used for the activity feed and for every performer event that reaches the
    dashboard. Callers redact the whole value before they clip it.

    Documented limit: a bare literal with no marker (``echo s3cr3tvalue123``)
    cannot be recognised by pattern. Streamed deltas are redacted one chunk at
    a time, so a token split across two chunks is not caught.
    """
    text = redact_secrets(text)
    # Every form above needs one of these, so text with none of them skips the scan.
    lowered = text.lower()
    if not any(marker in lowered for marker in _COMMAND_MARKERS):
        return text
    for pattern in _COMMAND_SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
    return text


_EVENT_TEXT_FIELDS = ("text", "detail", "arg")


def redact_performer_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of one performer event with its free-text fields redacted (148).

    Applied where the daemon merges a backend's events into state, so the
    snapshot, the session payloads and the SSE stream never hold the raw text.
    """
    redacted = dict(event)
    for field in _EVENT_TEXT_FIELDS:
        value = redacted.get(field)
        if isinstance(value, str) and value:
            redacted[field] = redact_command_text(value)
    return redacted


def _is_sensitive_key(key: str) -> bool:
    return key.lower() in SENSITIVE_KEYS


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
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



class RedactingFormatter(logging.Formatter):
    """Redact the final rendered record, including exception and stack text."""

    def __init__(self, delegate: logging.Formatter) -> None:
        super().__init__()
        self.delegate = delegate

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(self.delegate.format(record))
