"""Transcript redaction, shared by the performer and the daemon (spec 129).

This file is kept byte-identical in two places:

* ``src/coordinare/lib/transcript_redaction.py`` (daemon)
* ``agent/performer/src/performer/transcript_redaction.py`` (performer)

The performer cannot import coordinare, so the module is copied rather than
imported. ``tests/unit/test_129_redaction_parity.py`` fails if the two copies
differ, which keeps the value patterns and the key stems from drifting apart.

The module is stdlib only. It does not change ``coordinare.lib.redaction``,
which other callers rely on. Transcripts need a wider net: structured keys with
substring stems, ``NAME=value`` assignments, embedded JSON, and exact values
the job was handed.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Secrets shorter than this are not used for exact-value redaction. A short
# exact value would mangle ordinary text ("true", "main").
MIN_EXACT_SECRET_LENGTH = 8

# Token shapes. Source strings, so the parity test can compare them. Each one
# matches a value by its shape, whatever key or position it appears under.
VALUE_PATTERNS: tuple[str, ...] = (
    r"https://hooks\.slack(?:-gov)?\.com/services/[^\s\"'<>]+",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"sk-ant-[A-Za-z0-9_\-]{20,}",
    r"sk-litellm-[A-Za-z0-9_\-]{20,}",
    r"sk-proj-[A-Za-z0-9_\-]{20,}",
    r"sk-[A-Za-z0-9_\-]{20,}",
    r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]{20,}",
    r"AKIA[0-9A-Z]{16}",
)

# Name stems that mark a key or variable as holding a credential, matched
# case-insensitively as substrings. ``token(?!s)`` keeps usage counts
# (input_tokens, max_tokens, tokens_processed) out of scope while still
# catching auth_token, github_token and ANTHROPIC_AUTH_TOKEN.
NAME_STEMS = (
    r"(?:token(?!s)|secret|passw(?:or)?d|api[_-]?key|authorization"
    r"|credential|private[_-]?key|cookie)"
)

_VALUE_RES: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in VALUE_PATTERNS)
_KEY_RE = re.compile(NAME_STEMS, re.IGNORECASE)
# NAME=value, NAME: value, "NAME": "value". The value is either quoted or runs
# to whitespace or a structural character.
_ASSIGN_RE = re.compile(
    r"(?P<name>[A-Za-z0-9_\-]*" + NAME_STEMS + r"[A-Za-z0-9_\-]*)"
    r"(?P<sep>[\"']?\s*[=:]\s*)"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"',;}\])]+)",
    re.IGNORECASE,
)
# An Authorization header takes the rest of its value, whatever scheme it uses.
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization[\"']?\s*[=:]\s*)[^\r\n\"',}]+",
)


def is_sensitive_key(key: object) -> bool:
    """True when a structured key names a credential (case-insensitive stem match)."""
    return isinstance(key, str) and _KEY_RE.search(key) is not None


def secret_values_from_env(environ: Mapping[str, str]) -> list[str]:
    """Values of environment variables whose names look like credentials."""
    return [
        value
        for name, value in environ.items()
        if _KEY_RE.search(name) is not None and len(value) >= MIN_EXACT_SECRET_LENGTH
    ]


def _assign_repl(match: re.Match[str]) -> str:
    value = match.group("value")
    if value.startswith("[REDACTED"):
        return match.group(0)
    head = match.group("name") + match.group("sep")
    if value[:1] in ("'", '"'):
        quote = value[0]
        return head + quote + REDACTED + quote
    return head + REDACTED


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact one string: exact secret values, token shapes, auth headers, assignments."""
    for secret in sorted(
        (s for s in secrets if len(s) >= MIN_EXACT_SECRET_LENGTH),
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, REDACTED)
    for pattern in _VALUE_RES:
        text = pattern.sub(REDACTED, text)
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _ASSIGN_RE.sub(_assign_repl, text)


def _redact_string(text: str, secrets: tuple[str, ...]) -> str:
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            parsed: object = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict | list):
            return json.dumps(_redact(parsed, secrets), ensure_ascii=False)
    return redact_text(text, secrets)


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return _redact_string(value, secrets)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            clean_key = redact_text(key_text, secrets)
            if is_sensitive_key(key_text) and item is not None:
                out[clean_key] = REDACTED
            else:
                out[clean_key] = _redact(item, secrets)
        return out
    if isinstance(value, list | tuple):
        return [_redact(item, secrets) for item in value]
    return value


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact a structured value recursively.

    Dict values under credential-named keys are replaced whole. Strings that
    hold JSON are parsed, redacted inside, and re-encoded. Every other string is
    redacted with :func:`redact_text`.
    """
    return _redact(value, tuple(secrets))
