"""Transcript redaction, shared by the performer and the daemon (spec 129).

This file is kept byte-identical in two places:

* ``src/coordinare/lib/transcript_redaction.py`` (daemon)
* ``agent/performer/src/performer/transcript_redaction.py`` (performer)

The performer cannot import coordinare, so the module is copied rather than
imported. ``tests/unit/test_129_redaction_parity.py`` fails if the two copies
differ, which keeps the value patterns and the key stems from drifting apart.

Every scan here is linear in the input length. A pattern with a leading
character class that is retried at each start position is quadratic on a long
run of word characters, so assignments are found by tokenising words once and
then matching the separator and value at fixed positions. Keep it that way: a
new rule must not retry from every position of a long run.

Stdlib only. This module does not change ``coordinare.lib.redaction``, which
other callers rely on.
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

# Credential-name stems, matched case-insensitively against one whole word.
# The trailing ``(?![a-z])`` makes each stem end at a word boundary: ``tokenizer``,
# ``tokens`` (usage counts), ``passwordless`` and ``cookiejar`` are not stems.
# ``token`` still matches inside ``auth_token``, ``GITHUB_TOKEN`` and ``authToken``.
NAME_STEMS = (
    r"(?:token|secrets?|passw(?:or)?d|api[_-]?key|authorization"
    r"|credentials?|private[_-]?key|cookies?)(?![a-z])"
)

_KEY_RE = re.compile(NAME_STEMS, re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9_\-]+")
# Separator after a key: optional closing quote (JSON), optional escaped quote
# (a JSON string inside a string), then ``=`` or ``:``. Anchored with .match().
_SEP_RE = re.compile(r"(?:\\)?[\"']?[ \t]*([=:])[ \t]*")
# An unquoted ``:`` value runs to whitespace or a structural character.
_UNQUOTED_RE = re.compile(r"[^\s\"',;}\])]*")
# URL userinfo: ``scheme://user:password@host``. The password is redacted.
_USERINFO_RE = re.compile(r"(://[^/\s:@]+:)[^/\s@]+@")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization[\"']?\s*[=:]\s*)[^\r\n\"',}]+")
_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PEM_END = "-----END "

_VALUE_RES: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in VALUE_PATTERNS)


def is_sensitive_key(key: object) -> bool:
    """True when a structured key names a credential (whole-word stem match)."""
    return isinstance(key, str) and _KEY_RE.search(key) is not None


def secret_values_from_env(environ: Mapping[str, str]) -> list[str]:
    """Values of environment variables whose names look like credentials."""
    return [
        value
        for name, value in environ.items()
        if _KEY_RE.search(name) is not None and len(value) >= MIN_EXACT_SECRET_LENGTH
    ]


def _line_end(text: str, start: int) -> int:
    for index in range(start, len(text)):
        if text[index] in "\r\n":
            return index
    return len(text)


def _redact_pem(text: str) -> str:
    """Replace each PEM private key block, BEGIN line through END line, with the marker."""
    out: list[str] = []
    pos = 0
    while True:
        begin = _PEM_BEGIN_RE.search(text, pos)
        if begin is None:
            break
        end_marker = text.find(_PEM_END, begin.end())
        if end_marker == -1:
            stop = len(text)
        else:
            close = text.find("-----", end_marker + len(_PEM_END))
            stop = len(text) if close == -1 else close + len("-----")
        out.append(text[pos : begin.start()])
        out.append(REDACTED)
        pos = stop
    out.append(text[pos:])
    return "".join(out)


def _value_span(text: str, value_start: int, separator: str) -> tuple[int, str, str]:
    """End offset, opening quote and closing quote of the value starting at ``value_start``.

    Quotes are part of the replaced span, so they are written back around the marker.
    """
    if text.startswith('\\"', value_start):
        close = text.find('\\"', value_start + 2)
        if close == -1:
            return _line_end(text, value_start), "", ""
        return close + 2, '\\"', '\\"'
    if value_start < len(text) and text[value_start] in "\"'":
        quote = text[value_start]
        close = text.find(quote, value_start + 1)
        if close == -1:
            return _line_end(text, value_start + 1), quote, ""
        return close + 1, quote, quote
    if separator == "=":
        return _line_end(text, value_start), "", ""
    unquoted = _UNQUOTED_RE.match(text, value_start)
    return (unquoted.end() if unquoted is not None else value_start), "", ""


def _redact_assignments(text: str) -> str:
    """Redact the value of each ``NAME=value`` / ``NAME: value`` whose NAME is a credential stem.

    Linear: words are tokenised once, and each value is found by a scan that
    starts after the previous replacement, so no region is scanned twice.

    Value rules:
    * a JSON-escaped string (``\\"...\\"``) and a quoted string keep their quotes;
    * an unquoted ``=`` value runs to the end of the line, so ``FOO_SECRET=two words``
      is fully redacted;
    * an unquoted ``:`` value is a single run of non-space characters. Prose such as
      ``token: the cache is stale`` therefore redacts only ``the``. Redacting to the
      end of the line would hide the value of ``Authorization: Basic xyz`` and of
      ``token: abc def``, so the single-run rule is kept on purpose.
    """
    out: list[str] = []
    cursor = 0
    for word in _WORD_RE.finditer(text):
        if word.start() < cursor or _KEY_RE.search(word.group()) is None:
            continue
        sep = _SEP_RE.match(text, word.end())
        if sep is None:
            continue
        value_start = sep.end()
        value_end, open_q, close_q = _value_span(text, value_start, sep.group(1))
        if value_end == value_start and not open_q:
            continue
        if text.startswith("[REDACTED", value_start):
            continue
        out.append(text[cursor:value_start])
        out.append(open_q + REDACTED + close_q)
        cursor = value_end
    out.append(text[cursor:])
    return "".join(out)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact one string: exact secret values, PEM keys, token shapes, URL userinfo,
    auth headers and credential assignments. Linear in ``len(text)``."""
    for secret in sorted(
        (s for s in secrets if len(s) >= MIN_EXACT_SECRET_LENGTH),
        key=len,
        reverse=True,
    ):
        text = text.replace(secret, REDACTED)
    text = _redact_pem(text)
    for pattern in _VALUE_RES:
        text = pattern.sub(REDACTED, text)
    text = _USERINFO_RE.sub(lambda m: m.group(1) + REDACTED + "@", text)
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _redact_assignments(text)


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
