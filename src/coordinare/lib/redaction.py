from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from coordinare.lib.transcript_redaction import redact_text as transcript_redact_text

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


# 148: the command forms the transcript redactor (lib/transcript_redaction.py,
# spec 129) does not cover. Run after it, so anything it already handles is
# never matched twice. Each pattern keeps its prefix in group 1 and replaces the
# secret span. The performer keeps an identical list (performer/models.py); a
# parity test pins the two together.
_COMMAND_MARKERS = ("-", "configure")
_QUOTED_OR_BARE = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_COMMAND_SECRET_PATTERNS: list[re.Pattern[str]] = [
    # docker login -p x, sshpass -p x (mysql -p, env assignments, --password and
    # --token flags, curl -u, URL userinfo and auth headers are all transcript rules)
    re.compile(
        r"(\b(?:docker\s+login|sshpass)\b[^|;&\n]*?\s-p)\s*" + _QUOTED_OR_BARE,
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
    """Redact one command, tool argument or event text (148, 129).

    In order: the value patterns (``redact_secrets``), the transcript redactor
    (``transcript_redaction.redact_text``), then the few command forms it does
    not cover. Used for the activity feed and for every performer event that
    reaches the dashboard. Callers redact the whole value before they clip it.

    Documented limit: a bare literal with no marker (``echo s3cr3tvalue123``)
    cannot be recognised by pattern. Streamed deltas are redacted one chunk at
    a time, so a token split across two chunks is not caught.
    """
    text = redact_secrets(text)
    text = transcript_redact_text(text)
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
