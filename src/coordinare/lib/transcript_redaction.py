"""Transcript redaction, shared by the performer and the daemon (spec 129).

This file is kept byte-identical in two places:

* ``src/coordinare/lib/transcript_redaction.py`` (daemon)
* ``agent/performer/src/performer/transcript_redaction.py`` (performer)

The performer cannot import coordinare, so the module is copied rather than
imported. ``tests/unit/test_129_redaction_parity.py`` fails if the two copies
differ.

Guarantees:

* Every scan is linear in the input length. Assignments are found by tokenising
  words once, and the separator and value are matched at fixed positions.
* Recursion is bounded by ``MAX_DEPTH``. A structure that cannot be redacted
  structurally falls back to pattern redaction of its text, never to the
  unredacted value.
* Redaction never raises for any input. Lone surrogates become U+FFFD and ANSI
  escape sequences are stripped before matching, so output is always valid text.
* Redaction changes string values only. Callers serialise the structure
  afterwards, so the result is always valid JSON.

Stdlib only. ``coordinare.lib.redaction`` is not changed.
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

# Containers nested deeper than this are replaced by the marker, not recursed.
MAX_DEPTH = 64

# Token shapes. Source strings, so the parity test can compare them.
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
    r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",
    r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}",
)

# Credential-name stems, matched case-insensitively against one whole word. The
# trailing ``(?![a-z])`` makes each stem end at a word boundary, so ``tokenizer``,
# ``tokens`` (usage counts) and ``passwordless`` are not stems. ``token`` still
# matches inside ``auth_token``, ``GITHUB_TOKEN`` and ``authToken``.
NAME_STEMS = (
    r"(?:token|secrets?|passw(?:or)?d|pass_?word|passphrase|api[_-]?key|authorization"
    r"|credentials?|private[_-]?key|cookies?)(?![a-z])"
)

# A name ending in one of these is a description of a secret, not the secret:
# ``token_count``, ``max_budget``, ``session_type``. ``_id`` is not on the list.
DESCRIPTIVE_SUFFIX = r"_(?:counts?|limits?|budgets?|usage|types?|names?)$"

# Python and TypeScript type names. ``def f(token: str)`` declares a type; it
# does not carry a credential.
TYPE_NAMES = frozenset({
    "str", "int", "float", "bool", "bytes", "dict", "list", "tuple", "set",
    "any", "none", "optional", "union", "path", "datetime", "string", "number",
})

_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_KEY_RE = re.compile(NAME_STEMS, re.IGNORECASE)
_DESCRIPTIVE_RE = re.compile(DESCRIPTIVE_SUFFIX, re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9_\-]+")
# Separator after a key: optional closing quote (JSON), optional escaped quote,
# then ``=`` or ``:``. Anchored with .match(); bounded, so linear.
_SEP_RE = re.compile(r"(?:\\)?[\"']?[ \t]*([=:])[ \t]*")
# An unquoted ``:`` value runs to whitespace or a structural character.
_UNQUOTED_RE = re.compile(r"[^\s\"',;}\])]*")
_BARE_VALUE_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|true|false|null|none)", re.IGNORECASE)
# URL userinfo: ``scheme://user:password@host``. The password is redacted.
_USERINFO_RE = re.compile(r"(://[^/\s:@]+:)[^/\s@]+@")
# ``curl -u user:password`` and ``--user user:password``.
_CURL_USER_RE = re.compile(r"(-u\s+|--user[ =]|--user\s+)([^\s:'\"]+):([^\s'\"]+)")
# netrc: ``machine h login u password p`` on one line, or ``password p`` alone.
_NETRC_RE = re.compile(
    r"((?:^|\s)(?:machine\s+\S+\s+|login\s+\S+\s+)+password\s+)([^\s\"']+)",
    re.MULTILINE,
)
# A netrc ``password`` line holds exactly one token. A prose line such as
# ``Password is wrong`` has more words and is left alone.
_NETRC_LINE_RE = re.compile(
    r"(^[ \t]*password[ \t]+)([^\s\"']+)([ \t]*$)", re.MULTILINE | re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization[\"']?\s*[=:]\s*)[^\r\n\"',}]+")
_PEM_BEGIN_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----")
_BASE64_LINE_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")

_VALUE_RES: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in VALUE_PATTERNS)


def _is_secret_name(name: str) -> bool:
    """A name that holds a credential: a stem match, and not a non-secret suffix."""
    return _KEY_RE.search(name) is not None and _DESCRIPTIVE_RE.search(name) is None


def is_sensitive_key(key: object) -> bool:
    """True when a structured key names a credential."""
    return isinstance(key, str) and _is_secret_name(key)


def secret_values_from_env(environ: Mapping[str, str]) -> list[str]:
    """Values of environment variables whose names look like credentials."""
    return [
        value
        for name, value in environ.items()
        if _is_secret_name(name) and len(value) >= MIN_EXACT_SECRET_LENGTH
    ]


def _line_end(text: str, start: int) -> int:
    for index in range(start, len(text)):
        if text[index] in "\r\n":
            return index
    return len(text)


def _pem_block_end(text: str, begin_end: int, kind: str) -> int:
    """Index just past the matching END line, or -1 when there is none."""
    end_marker = f"-----END {kind}-----"
    index = text.find(end_marker, begin_end)
    return -1 if index == -1 else index + len(end_marker)


def _base64_run_end(text: str, start: int) -> int:
    """End offset of the base64-looking lines that follow ``start``, or ``start``."""
    stop = start
    scan = start
    while scan < len(text):
        line_start = scan + 1 if text[scan] in "\r\n" else scan
        if line_start >= len(text):
            break
        eol = _line_end(text, line_start)
        candidate = text[line_start:eol].strip()
        if not candidate or _BASE64_LINE_RE.fullmatch(candidate) is None:
            break
        stop = eol
        scan = eol
    return stop


def _redact_pem(text: str) -> str:
    """Redact PEM private key blocks from BEGIN to the matching END.

    A BEGIN marker with no matching END is redacted only when base64-looking lines
    follow it, so a marker quoted in prose does not swallow the text after it.
    """
    out: list[str] = []
    pos = 0
    no_end_for: set[str] = set()
    while True:
        begin = _PEM_BEGIN_RE.search(text, pos)
        if begin is None:
            break
        kind = begin.group(1)
        out.append(text[pos : begin.start()])
        stop = -1
        if kind not in no_end_for:
            stop = _pem_block_end(text, begin.end(), kind)
            if stop == -1:
                no_end_for.add(kind)
        if stop != -1:
            out.append(REDACTED)
            pos = stop
            continue
        body_end = _base64_run_end(text, _line_end(text, begin.end()))
        if body_end > _line_end(text, begin.end()):
            out.append(REDACTED)
            pos = body_end
        else:
            out.append(text[begin.start() : begin.end()])
            pos = begin.end()
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


def _is_code_context(text: str, value_start: int, value_end: int, separator: str,
                     open_q: str) -> bool:
    """True when the match is code or a description, not a credential value.

    * a bare number or boolean (``token=5``, ``token: true``);
    * a type annotation (``def f(token: str)``): an unquoted ``:`` value that names a
      type, or is a generic such as ``list[str]``;
    * ``==`` and ``===`` comparisons are excluded by the caller.
    """
    if open_q:
        return False
    value = text[value_start:value_end].strip()
    if _BARE_VALUE_RE.fullmatch(value):
        return True
    if separator == ":":
        bare = value.rstrip("]|").lower()
        if bare in TYPE_NAMES or "[" in value or "|" in value:
            return True
    return False


def _redact_assignments(text: str) -> str:
    """Redact the value of each ``NAME=value`` / ``NAME: value`` whose NAME is a credential.

    Linear: words are tokenised once, and each value is found by a scan that starts
    after the previous replacement, so no region is scanned twice.

    Rules:
    * ``==`` and ``===`` are comparisons, not assignments (``if token == x``);
    * a name ending in ``_count``, ``_limit``, ``_budget``, ``_usage``, ``_type`` or
      ``_name`` describes a secret rather than holding one;
    * a bare number or boolean value is not a credential;
    * a type annotation (``token: str``) is not a credential;
    * a JSON-escaped string and a quoted string keep their quotes;
    * an unquoted ``=`` value runs to the end of the line, so ``FOO_SECRET=two words``
      is fully redacted;
    * an unquoted ``:`` value is a single run of non-space characters. Prose such as
      ``token: the cache is stale`` therefore redacts only ``the``. Redacting to the
      end of the line would hide ``Authorization: Basic xyz``, so this single-run rule
      is kept on purpose.
    """
    out: list[str] = []
    cursor = 0
    for word in _WORD_RE.finditer(text):
        if word.start() < cursor or not _is_secret_name(word.group()):
            continue
        sep = _SEP_RE.match(text, word.end())
        if sep is None:
            continue
        if text[sep.end() : sep.end() + 1] == "=":
            continue  # ``==`` or ``===``: a comparison
        separator = sep.group(1)
        value_start = sep.end()
        value_end, open_q, close_q = _value_span(text, value_start, separator)
        if value_end == value_start and not open_q:
            continue
        if text.startswith("[REDACTED", value_start):
            continue
        if _is_code_context(text, value_start, value_end, separator, open_q):
            continue
        out.append(text[cursor:value_start])
        out.append(open_q + REDACTED + close_q)
        cursor = value_end
    out.append(text[cursor:])
    return "".join(out)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact one string. Linear in ``len(text)``; never raises.

    Lone surrogates become U+FFFD and ANSI escape sequences are removed first, so
    matching sees the visible text and the output is valid.
    """
    text = _SURROGATE_RE.sub("�", text)
    text = _ANSI_RE.sub("", text)
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
    text = _CURL_USER_RE.sub(lambda m: m.group(1) + m.group(2) + ":" + REDACTED, text)
    text = _NETRC_RE.sub(lambda m: m.group(1) + REDACTED, text)
    text = _NETRC_LINE_RE.sub(lambda m: m.group(1) + REDACTED + m.group(3), text)
    text = _AUTH_HEADER_RE.sub(lambda m: m.group(1) + REDACTED, text)
    return _redact_assignments(text)


def _redact_string(text: str, secrets: tuple[str, ...], depth: int) -> str:
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            parsed: object = json.loads(text)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, dict | list):
            return json.dumps(_redact(parsed, secrets, depth + 1), ensure_ascii=False)
    return redact_text(text, secrets)


def _redact(value: Any, secrets: tuple[str, ...], depth: int) -> Any:
    if isinstance(value, str):
        return _redact_string(value, secrets, depth)
    if depth >= MAX_DEPTH and isinstance(value, Mapping | list | tuple):
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            clean_key = redact_text(key_text, secrets)
            if is_sensitive_key(key_text) and item is not None and not isinstance(
                item, bool | int | float,
            ):
                out[clean_key] = REDACTED
            else:
                out[clean_key] = _redact(item, secrets, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_redact(item, secrets, depth + 1) for item in value]
    return value


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact a structured value recursively. Never raises.

    Dict values under credential names are replaced whole, except bare numbers and
    booleans. Strings that hold JSON are parsed, redacted inside, and re-encoded.
    Every other string is redacted with :func:`redact_text`. If structural
    redaction fails (for example on an extreme nesting), the fallback redacts the
    text of the value, and a non-text value is replaced by the marker. The
    unredacted value is never returned.
    """
    secret_tuple = tuple(secrets)
    try:
        return _redact(value, secret_tuple, 0)
    except (RecursionError, ValueError, TypeError, OverflowError):
        if isinstance(value, str):
            return redact_text(value, secret_tuple)
        return REDACTED
