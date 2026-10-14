"""Transcript redaction, shared by the performer and the daemon (spec 129).

This file is kept byte-identical in two places:

* ``src/coordinare/lib/transcript_redaction.py`` (daemon)
* ``agent/performer/src/performer/transcript_redaction.py`` (performer)

The performer cannot import coordinare, so the module is copied rather than
imported. ``tests/unit/test_129_redaction_parity.py`` fails if the copies differ.

Every scan is linear in the input length. The rules that matter for that:

* A pattern never retries from every position of a long run. Tokens (maximal runs
  of word characters) are found once, and prefixes and JWT segments are located
  inside each token with ``str.find``, which advances monotonically.
* Assignments are found by tokenising words once, then the separator and value are
  matched at fixed positions.
* PEM blocks are paired with their END markers in one pass, and line ends are
  cached, so repeated markers on one line cost no more than one scan.
* The regular expressions that remain start at a literal and do not nest
  quantifiers over a shared character class.

Recursion is bounded by ``MAX_DEPTH``. Redaction never raises: a structure that
cannot be redacted structurally falls back to redaction of its text, and a non-text
value is replaced by the marker. Lone surrogates become U+FFFD and ANSI and OSC
escape sequences are removed before matching. Redaction changes string values only,
so the caller's serialisation is always valid JSON.

Stdlib only. ``coordinare.lib.redaction`` is not changed.
"""

from __future__ import annotations

import bisect
import functools
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Secrets shorter than this are not used for exact-value redaction. A short
# exact value would mangle ordinary text ("true", "main").
MIN_EXACT_SECRET_LENGTH = 8

# A bare number is a credential under a credential stem only with this many digits.
MIN_CREDENTIAL_DIGITS = 4

# The shortest JWT segment, and the number of segments in one JWT.
MIN_JWT_SEGMENT = 8
JWT_SEGMENTS = 3

# A netrc ``password`` follows ``machine <host>`` or ``login <user>``: two tokens back.
NETRC_KEYWORD_DISTANCE = 2

# Containers nested deeper than this are replaced by the marker, not recursed.
MAX_DEPTH = 64

# Token shapes matched by one anchored literal each. Source strings, so the parity
# test can compare them. Prefix-led shapes (sk-, ghp_, github_pat_) and JWTs are
# handled by token scans instead, see ``_redact_prefixed`` and ``_redact_jwts``.
VALUE_PATTERNS: tuple[str, ...] = (
    r"https://hooks\.slack(?:-gov)?\.com/services/[A-Za-z0-9/_\-]+",
    r"AKIA[0-9A-Z]{16}",
    r"(?i)bearer\s+[A-Za-z0-9\-._~+/=]{20,}",
    r"[sr]k_(?:live|test)_[A-Za-z0-9]{16,}",
)

# Credential shapes led by a literal prefix: (prefix, minimum characters after it).
PREFIX_SHAPES: tuple[tuple[str, int], ...] = (
    ("sk-", 20),
    ("github_pat_", 20),
    ("ghp_", 20),
    ("gho_", 20),
    ("ghs_", 20),
    ("ghu_", 20),
    ("ghr_", 20),
)

# Name stems that mark a key or variable as holding a credential, matched
# case-insensitively against one whole word. ``(?![a-z])`` ends each stem at a word
# boundary, so ``tokenizer``, ``tokens`` and ``passwordless`` are not stems.
NAME_STEMS = (
    r"(?:token|secrets?|passw(?:or)?d|pass_?word|passphrase|api[_-]?key|authorization"
    r"|credentials?|private[_-]?key|cookies?)(?![a-z])"
)

# A name ending in one of these describes a secret rather than holding it:
# ``token_count``, ``token_limit``, ``token_ttl``, ``password_reset_sent``. The
# exemption does not apply when the name also carries a strong stem
# (``db_password_name`` is still redacted, see ``_non_secret_shape``).
DESCRIPTIVE_SUFFIX = (
    r"_(?:counts?|limits?|budgets?|usage|types?|names?|caches?|ttls?|expir(?:y|ies)"
    r"|expires|expires_in|timeouts?|timeout_ms|resets?|reset_sent|files?|paths?|dirs?"
    r"|directories|urls?|uris?|endpoints?|headers?|fields?|key_names?|enabled|required"
    r"|lengths?|len|sizes?|max|min|prefix(?:es)?|suffix(?:es)?|formats?|regex(?:es)?"
    r"|patterns?)$"
)

# Stems that always mean a credential, whatever suffix follows them.
STRONG_STEMS = r"passw(?:or)?d|pass_?word|passphrase|secrets?|private[_-]?key"

# Bare values (numbers, booleans) are credentials only under these names.
BARE_CREDENTIAL_STEMS = (
    r"(?:passw(?:or)?d|pass_?word|passphrase|secrets?|token(?!s)|api[_-]?key|pin|otp"
    r"|private[_-]?key|credentials?|auth)(?![a-z])"
)

# Python and TypeScript type names. ``def f(token: str)`` declares a type.
TYPE_NAMES = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "dict",
        "list",
        "tuple",
        "set",
        "any",
        "none",
        "optional",
        "union",
        "path",
        "datetime",
        "string",
        "number",
    },
)

_SURROGATE_RE = re.compile("[\ud800-\udfff]")
# CSI sequences (ESC [ ... final byte) and OSC sequences (ESC ] ... BEL or ESC \).
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_KEY_RE = re.compile(NAME_STEMS, re.IGNORECASE)
_STRONG_RE = re.compile(STRONG_STEMS, re.IGNORECASE)
_DESCRIPTIVE_RE = re.compile(DESCRIPTIVE_SUFFIX, re.IGNORECASE)
_BARE_CREDENTIAL_RE = re.compile(BARE_CREDENTIAL_STEMS, re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9_\-]+")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+")
_JWT_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-.]+")
_NETRC_TOKEN_RE = re.compile(r"\S+")
_PASSWORD_WORD_RE = re.compile(r"password", re.IGNORECASE)
_EOL_RE = re.compile(r"[\r\n]")
# Separator after a key: optional closing quote (JSON), optional escaped quote,
# then ``=`` or ``:``. Anchored with .match(); bounded, so linear.
_SEP_RE = re.compile(r"(?:\\)?[\"']?[ \t]*([=:])[ \t]*")
_CMP_RE = re.compile(r"=+[ \t]*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
_ASSIGN_AFTER_RE = re.compile(r"[ \t]*=(?!=)[ \t]*")
# An unquoted ``:`` value runs to whitespace or a structural character.
_UNQUOTED_RE = re.compile(r"[^\s\"',;}\])]*")
_BARE_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?|true|false|null|none)", re.IGNORECASE)
# Values that cannot be credentials by their shape (see ``_non_secret_shape``).
_NON_SECRET_LITERALS = frozenset({"{}", "[]", "null", "none", "~"})
_BOOLEAN_RE = re.compile(r"true|false", re.IGNORECASE)
_NUMBER_RE = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")
# A path starts ``/``, ``~/``, ``./``, ``../`` or a Windows drive. Matched with .match().
_PATH_RE = re.compile(r"(?:~|\.{1,2})?/|[A-Za-z]:[\\/]")
# A plain identifier or dotted name with no digits (``path``, ``self.cache``).
_IDENTIFIER_VALUE_RE = re.compile(r"[A-Za-z_]+(?:\.[A-Za-z_]+)*")
# A key that names an authorization header or credential. Searched, not matched.
_AUTH_KEY_RE = re.compile(r"auth", re.IGNORECASE)
# ``Bearer `` at the start of a value, optionally quoted. Matched with .match().
_BEARER_PREFIX_RE = re.compile(r"[\"']?bearer[ \t]+", re.IGNORECASE)
# A value that starts with a URL scheme. Matched with .match().
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
# URL userinfo: ``scheme://user:password@host``. The password is redacted. The user may
# be empty (``redis://:password@host``).
_USERINFO_RE = re.compile(r"(://[^/\s:@]*:)[^/\s@]+@")
# ``--token VALUE`` and ``--password VALUE``: a secret flag followed by a space and value.
# A value that is itself a flag (``--token --verbose``) is not a credential.
_FLAG_VALUE_RE = re.compile(
    r"(?P<head>(?<![\w-])--(?:token|passw(?:or)?d|secret|api[_-]?key)[ \t]+)(?!-)\S+",
    re.IGNORECASE,
)
# Clients that take ``-pVALUE`` (no space) for the password.
_MYSQL_CLIENTS = frozenset({"mysql", "mysqldump", "mysqladmin", "mariadb", "mariadb-dump"})
# A YAML block value: the next line holds one token and nothing else.
_BLOCK_VALUE_RE = re.compile(r"[ \t]*([^\s:=\"',;}\])]+)[ \t]*(?=\r?\n|\Z)")
# ``curl -u user:password`` and ``--user user:password``.
_CURL_USER_RE = re.compile(r"(-u\s+|--user[ =]|--user\s+)([^\s:'\"]+):([^\s'\"]+)")
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization(?:_[a-z]+)?[\"']?\s*[=:]\s*)[^\r\n\"',}]+",
)
_PEM_BEGIN_RE = re.compile(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----")
_PEM_END_RE = re.compile(r"-----END ([A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----")
_BASE64_LINE_RE = re.compile(r"[A-Za-z0-9+/=]{16,}")

_VALUE_RES: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in VALUE_PATTERNS)


@functools.lru_cache(maxsize=4096)
def _is_secret_name(name: str) -> bool:
    """A name that holds a credential: it carries a credential stem, strong or weak.

    The suffix is not considered here. A descriptive suffix (``token_url``,
    ``api_key_file``) does not stop the name from being a credential name, because the
    value may still be one. The relief for descriptive names is applied to the value, in
    ``_value_is_credential``.
    """
    return _KEY_RE.search(name) is not None


def _bare_credential_value(name: str, value: str) -> bool:
    """True when a bare number or boolean under ``name`` is a credential.

    A number is a credential when it has at least four digits and the name is a
    credential stem (``PASSWORD=12345678``, ``pin=1234``). A boolean under a
    credential stem is treated the same way (``PASSWORD=true``). ``token=5`` is not.
    """
    if _BARE_CREDENTIAL_RE.search(name) is None:
        return False
    lowered = value.lower()
    if lowered in ("true", "false", "null", "none"):
        return True
    return len(re.sub(r"\D", "", value)) >= MIN_CREDENTIAL_DIGITS


def is_sensitive_key(key: object) -> bool:
    """True when a structured key names a credential."""
    return isinstance(key, str) and _is_secret_name(key)


def secret_values_from_env(environ: Mapping[str, str]) -> list[str]:
    """Values of environment variables whose names look like credentials."""
    return [
        value
        for name, value in environ.items()
        if _is_secret_name(name)
        and len(value) >= MIN_EXACT_SECRET_LENGTH
        and _value_is_credential(name, value, bare_only=False, quoted=False)
    ]


def _line_end(text: str, start: int) -> int:
    match = _EOL_RE.search(text, start)
    return len(text) if match is None else match.start()


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

    All END markers are located in one pass. Each BEGIN is paired with the first END
    of the same kind after it, by bisection. A BEGIN with no END is redacted only when
    base64-looking lines follow it, so a marker quoted in prose does not swallow the
    text after it.
    """
    ends: dict[str, list[int]] = {}
    for match in _PEM_END_RE.finditer(text):
        ends.setdefault(match.group(1), []).append(match.end())
    out: list[str] = []
    pos = 0
    eol_cache = -1
    line_end_cache = -1
    for begin in _PEM_BEGIN_RE.finditer(text):
        if begin.start() < pos:
            continue
        out.append(text[pos : begin.start()])
        kind_ends = ends.get(begin.group(1), [])
        index = bisect.bisect_left(kind_ends, begin.end())
        if index < len(kind_ends):
            out.append(REDACTED)
            pos = kind_ends[index]
            continue
        # Line ends are monotonic over the BEGIN markers, so one scan serves them all.
        if begin.end() > eol_cache:
            eol_cache = _line_end(text, begin.end())
            line_end_cache = eol_cache
        body_end = _base64_run_end(text, line_end_cache)
        if body_end > line_end_cache:
            out.append(REDACTED)
            pos = body_end
        else:
            out.append(text[begin.start() : begin.end()])
            pos = begin.end()
    out.append(text[pos:])
    return "".join(out)


def _first_secret_offset(token: str) -> int | None:
    """Offset in ``token`` where a prefix-led credential starts, or None.

    Each prefix is searched with ``str.find``, which only moves forward, so the scan
    is linear in the token. An occurrence whose tail is too short is skipped.
    """
    best: int | None = None
    for prefix, minimum in PREFIX_SHAPES:
        start = 0
        while True:
            index = token.find(prefix, start)
            if index == -1:
                break
            if len(token) - (index + len(prefix)) >= minimum:
                if best is None or index < best:
                    best = index
                break
            start = index + 1
    return best


def _redact_prefixed(text: str) -> str:
    """Redact prefix-led credentials: from the prefix to the end of its token."""
    out: list[str] = []
    cursor = 0
    for token in _TOKEN_RE.finditer(text):
        offset = _first_secret_offset(token.group())
        if offset is None:
            continue
        out.append(text[cursor : token.start() + offset])
        out.append(REDACTED)
        cursor = token.end()
    out.append(text[cursor:])
    return "".join(out)


def _redact_jwts(text: str) -> str:
    """Redact JSON Web Tokens: three dot-separated segments, the first starting ``eyJ``.

    Each dotted run is split once. A JWT is three consecutive segments of at least
    eight characters, and the first begins ``eyJ``. Its span is replaced by the marker.
    """
    out: list[str] = []
    cursor = 0
    for run in _JWT_TOKEN_RE.finditer(text):
        body = run.group()
        if "." not in body or "eyJ" not in body:
            continue
        parts = body.split(".")
        offsets: list[int] = []
        position = 0
        for part in parts:
            offsets.append(position)
            position += len(part) + 1
        index = 0
        while index + 2 < len(parts):
            first, second, third = parts[index], parts[index + 1], parts[index + 2]
            if (
                first.startswith("eyJ")
                and len(first) >= MIN_JWT_SEGMENT
                and len(second) >= MIN_JWT_SEGMENT
                and len(third) >= MIN_JWT_SEGMENT
            ):
                start = run.start() + offsets[index]
                end = run.start() + offsets[index + 2] + len(third)
                out.append(text[cursor:start])
                out.append(REDACTED)
                cursor = end
                index += 3
            else:
                index += 1
    out.append(text[cursor:])
    return "".join(out)


def _redact_netrc(text: str) -> str:
    """Redact netrc passwords, in one pass over the whitespace-separated tokens.

    A ``password`` token is redacted when it follows ``machine <host>`` or
    ``login <user>``, or when it is the only other token on its line
    (``password hunter2``). A prose line such as ``Password is wrong`` has more words
    and is left alone. Text without the word ``password`` is returned unchanged.
    """
    if _PASSWORD_WORD_RE.search(text) is None:
        return text
    tokens = list(_NETRC_TOKEN_RE.finditer(text))
    spans: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token.group().lower() != "password" or index + 1 >= len(tokens):
            continue
        value = tokens[index + 1]
        after_keyword = index >= NETRC_KEYWORD_DISTANCE and tokens[
            index - NETRC_KEYWORD_DISTANCE
        ].group().lower() in ("machine", "login")
        starts_line = index == 0 or "\n" in text[tokens[index - 1].end() : token.start()]
        ends_line = (
            index + 2 >= len(tokens) or "\n" in text[value.end() : tokens[index + 2].start()]
        )
        if after_keyword or (starts_line and ends_line):
            spans.append((value.start(), value.end()))
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(text[cursor:start])
        out.append(REDACTED)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _value_span(text: str, value_start: int, operator: str) -> tuple[int, str, str]:
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
    if operator == "=":
        return _line_end(text, value_start), "", ""
    unquoted = _UNQUOTED_RE.match(text, value_start)
    return (unquoted.end() if unquoted is not None else value_start), "", ""


def _is_annotation_word(value: str) -> bool:
    """True when an unquoted ``:`` value names a type (``str``, ``list[str]``, ``A | B``)."""
    word = value.strip()
    if "[" in word or "|" in word:
        return True
    return word.lower() in TYPE_NAMES


def _comparison_value_start(text: str, equals_index: int) -> int | None:
    """Where the right side of a ``==`` comparison starts, or None when it is code.

    An unquoted identifier on the right (``if token == x``) is code. A quoted or
    numeric right side is a value and is redacted.
    """
    rhs = _CMP_RE.match(text, equals_index)
    rhs_start = rhs.end() if rhs is not None else equals_index
    identifier = _IDENT_RE.match(text, rhs_start)
    if identifier is None:
        return rhs_start
    following = text[identifier.end() : identifier.end() + 1]
    return rhs_start if following in ('"', "'") else None


def _annotation_assignment(text: str, annotation_end: int) -> int | None:
    """Where the value assigned after a type annotation starts, or None if nothing is assigned.

    ``password: str = "x"`` assigns ``"x"``. ``token: str)`` or ``token: str,`` assigns
    nothing: it is a declaration.
    """
    assigned = _ASSIGN_AFTER_RE.match(text, annotation_end)
    return None if assigned is None else assigned.end()


def _non_secret_shape(value: str, *, strong: bool, descriptive: bool) -> bool:
    """True when ``value`` cannot be a credential, judged by its shape alone.

    * ``{}``, ``[]``, ``null``, ``None`` and an empty value are never credentials.
    * A boolean is not a credential unless the name is a strong stem that does not
      describe something else (``PASSWORD=true`` is redacted; ``password_reset_sent``
      and ``bearer_auth = True`` are not).
    * Under a descriptive suffix that is not a strong stem, a number, a path or a plain
      identifier is not a credential (``token_ttl: 3600``, ``credentials_file: path``).
      A strong stem keeps these: ``db_password_name=hunter2hunter2`` stays redacted,
      because its value has digits and is not a plain identifier.
    """
    if _looks_like_credential(value):
        return False
    if value == "" or value.lower() in _NON_SECRET_LITERALS:
        return True
    if _BOOLEAN_RE.fullmatch(value) is not None:
        return not (strong and not descriptive)
    if not descriptive or strong:
        return False
    return _descriptive_value_shape(value)


def _descriptive_value_shape(value: str) -> bool:
    """A value that is not a credential under a descriptive, non-strong name.

    A number, a path, a URL without userinfo, or a short plain identifier. A longer
    identifier, or a value with digits, may be a credential and is not relieved.
    """
    if _NUMBER_RE.fullmatch(value) is not None:
        return True
    if _is_path_like(value) or _is_url_without_userinfo(value):
        return True
    return (
        len(value) < MIN_EXACT_SECRET_LENGTH and _IDENTIFIER_VALUE_RE.fullmatch(value) is not None
    )


def _looks_like_credential(value: str) -> bool:
    """True when the value itself has a known credential shape, whatever its name.

    A value with a credential prefix, a token pattern, a JWT, or a URL with userinfo is
    a credential, so the descriptive relief never applies to it.
    """
    if any(pattern.search(value) for pattern in _VALUE_RES):
        return True
    if _first_secret_offset(value) is not None:
        return True
    if "eyJ" in value and value.count(".") >= JWT_SEGMENTS - 1:
        return True
    return _USERINFO_RE.search(value) is not None


def _is_path_like(value: str) -> bool:
    """A path: starts ``/``, ``./``, ``../``, ``~/`` or a drive, or has a ``/`` and no ``=``."""
    if _PATH_RE.match(value) is not None:
        return True
    return "/" in value and "=" not in value


def _is_url_without_userinfo(value: str) -> bool:
    """A URL with a scheme and no ``user:password@`` part (checked by ``_looks_like_credential``)."""
    return _URL_RE.match(value) is not None


def _value_is_credential(name: str, value: str, *, bare_only: bool, quoted: bool) -> bool:
    """False for a value that is not a credential under ``name``.

    The shape of the value comes first (``_non_secret_shape``). Then quoted and
    non-bare values are credentials. A bare value is one only under a credential stem,
    by the bare-value rule. ``bare_only`` names (``pin``, ``otp``, ``auth``) are
    credentials only as bare values.
    """
    inner = value.strip("\"'\\")
    strong = _STRONG_RE.search(name) is not None
    descriptive = _DESCRIPTIVE_RE.search(name) is not None
    if _non_secret_shape(inner, strong=strong, descriptive=descriptive):
        return False
    if bare_only:
        return _BARE_RE.fullmatch(inner) is not None and _bare_credential_value(name, inner)
    if quoted or _BARE_RE.fullmatch(value) is None:
        return True
    return _bare_credential_value(name, value)


def _yaml_block_value(text: str, after_colon: int) -> tuple[int, int] | None:
    """Span of a YAML block value on the line after a bare ``key:``, or None.

    The key's line must end after the colon. The next line must hold one token and
    nothing else, so ``password:`` followed by ``  hunter2hunter2`` is a value, and
    ``password:`` followed by another key is not.
    """
    eol = _line_end(text, after_colon)
    if text[after_colon:eol].strip():
        return None
    if eol >= len(text):
        return None
    start = eol + 2 if text.startswith("\r\n", eol) else eol + 1
    match = _BLOCK_VALUE_RE.match(text, start)
    if match is None:
        return None
    return match.start(1), match.end(1)


def _flag_and_client_values(text: str) -> str:
    """Redact ``mysql -pVALUE`` passwords and ``--token VALUE`` style flags.

    ``--token VALUE`` and ``--password VALUE`` are one regular expression with fixed
    literals, so each match is local. ``-pVALUE`` is a token pass: a ``-p`` token is a
    password only on a line that names a MySQL-family client before it.
    """
    text = _FLAG_VALUE_RE.sub(lambda m: m.group("head") + REDACTED, text)
    if "-p" not in text:
        return text
    out: list[str] = []
    cursor = 0
    in_client_line = False
    previous_end = 0
    for token in _NETRC_TOKEN_RE.finditer(text):
        word = token.group()
        if "\n" in text[previous_end : token.start()]:
            in_client_line = False
        previous_end = token.end()
        if word.startswith("-p") and word[2:3] not in ("", "-") and in_client_line:
            out.append(text[cursor : token.start() + 2])
            out.append(REDACTED)
            cursor = token.end()
        elif word.rsplit("/", 1)[-1] in _MYSQL_CLIENTS:
            in_client_line = True
    out.append(text[cursor:])
    return "".join(out)


def _is_bare_only_name(name: str) -> bool:
    """``pin``, ``otp`` and ``auth`` are credentials only as bare values.

    A credential value under them is not redacted unless the value is bare.
    """
    return (
        not _is_secret_name(name)
        and _BARE_CREDENTIAL_RE.search(name) is not None
        and _DESCRIPTIVE_RE.search(name) is None
    )


def _assignment_value_start(text: str, sep: re.Match[str], operator: str) -> int | None:
    """Where the value after a separator starts, or None when the match is code (``==``)."""
    value_start = sep.end()
    if operator == "=" and text[value_start : value_start + 1] == "=":
        return _comparison_value_start(text, sep.start(1))
    return value_start


def _annotated_value(text: str, annotation_end: int) -> tuple[int, int, str, str] | None:
    """The value assigned after a type annotation, or None when nothing is assigned."""
    assigned = _annotation_assignment(text, annotation_end)
    if assigned is None:
        return None  # a pure annotation: nothing is assigned
    value_end, open_q, close_q = _value_span(text, assigned, "=")
    if (value_end == assigned and not open_q) or text.startswith("[REDACTED", assigned):
        return None
    return assigned, value_end, open_q, close_q


def _locate_assignment(text: str, separator_at: int) -> tuple[int, int, str, str] | None:
    """Where the value after a key ends at ``separator_at`` lies, or None when it is code.

    Returns ``(value_start, value_end, opening_quote, closing_quote)``. The separator
    is ``=`` or ``:``. A ``==`` comparison, a pure type annotation and a value that is
    already redacted are not assignments and give None.
    """
    sep = _SEP_RE.match(text, separator_at)
    if sep is None:
        return None
    operator = sep.group(1)
    value_start = _assignment_value_start(text, sep, operator)
    if value_start is None:
        return None
    value_end, open_q, close_q = _value_span(text, value_start, operator)
    if value_end == value_start and not open_q:
        # ``password:`` alone on its line may hold its value on the next line.
        block = _yaml_block_value(text, value_start) if operator == ":" else None
        if block is None:
            return None
        value_start, value_end = block
    if operator == ":" and not open_q and _is_annotation_word(text[value_start:value_end]):
        return _annotated_value(text, value_end)
    if text.startswith("[REDACTED", value_start):
        return None
    return value_start, value_end, open_q, close_q


def _is_auth_key(name: str) -> bool:
    """A key that names an authorization header or credential (``auth_header``, ``Authorization``)."""
    return _AUTH_KEY_RE.search(name) is not None


def _replace_bearer(text: str) -> str:
    """Replace the token of a ``Bearer <token>`` string, whatever its length."""
    span = _bearer_token_span(text, 0)
    if span is None:
        return text
    return text[: span[0]] + REDACTED + text[span[1] :]


def _bearer_token_span(text: str, value_start: int) -> tuple[int, int] | None:
    """Span of the token in a ``Bearer <token>`` value starting at ``value_start``, or None."""
    prefix = _BEARER_PREFIX_RE.match(text, value_start)
    if prefix is None:
        return None
    token = _UNQUOTED_RE.match(text, prefix.end())
    if token is None or token.end() == prefix.end():
        return None
    return prefix.end(), token.end()


def _redact_assignments(text: str) -> str:
    """Redact the value of each credential ``NAME=value`` or ``NAME: value``.

    Linear: words are tokenised once, and each value is found by a scan that starts
    after the previous replacement, so no region is scanned twice.

    Rules, in order:

    * ``==`` and ``===`` compare. When the right side is an unquoted identifier
      (``if token == x``) the match is code and is left alone. A quoted or numeric
      right side is a value and is redacted (``token == "hunter2hunter2"``).
    * A type annotation (``token: str``) with nothing assigned is code. When an
      assignment follows it, that assigned value is redacted
      (``password: str = "hunter2hunter2"``).
    * A bare number or boolean is redacted only under a credential stem, and a number
      needs four or more digits (``PASSWORD=12345678``, ``pin=1234``). ``token=5``
      is left alone.
    * A JSON-escaped string and a quoted string keep their quotes.
    * An unquoted ``=`` value runs to the end of the line, so ``FOO_SECRET=two words``
      is fully redacted.
    * An unquoted ``:`` value is one run of non-space characters. Prose such as
      ``token: the cache is stale`` therefore redacts only ``the``. Redacting to the
      end of the line would hide ``Authorization: Basic xyz``, so this rule is kept on
      purpose: the relief for prose must not reopen a leak.
    """
    out: list[str] = []
    cursor = 0
    for word in _WORD_RE.finditer(text):
        name = word.group()
        if word.start() < cursor:
            continue
        bare_only = _is_bare_only_name(name)
        secret = _is_secret_name(name)
        auth = _is_auth_key(name)
        if not (bare_only or secret or auth):
            continue
        located = _locate_assignment(text, word.end())
        if located is None:
            continue
        value_start, value_end, open_q, close_q = located
        bearer = _bearer_token_span(text, value_start) if (secret or auth) else None
        if bearer is not None:
            # ``auth_token_header: Bearer abc.def.ghi``: the token is a credential whatever its
            # length, so only the token after ``Bearer`` is replaced.
            value_start, value_end = bearer
            open_q = close_q = ""
        else:
            if not (bare_only or secret):
                continue
            value = text[value_start:value_end].strip()
            if not _value_is_credential(name, value, bare_only=bare_only, quoted=bool(open_q)):
                continue
        out.append(text[cursor:value_start])
        out.append(open_q + REDACTED + close_q)
        cursor = value_end
    out.append(text[cursor:])
    return "".join(out)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Redact one string. Linear in ``len(text)``; never raises.

    Lone surrogates and terminal escape sequences are removed first, so matching
    sees the visible text and the output is valid.
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
    text = _redact_jwts(text)
    text = _redact_prefixed(text)
    for pattern in _VALUE_RES:
        text = pattern.sub(REDACTED, text)
    text = _USERINFO_RE.sub(lambda m: m.group(1) + REDACTED + "@", text)
    text = _CURL_USER_RE.sub(lambda m: m.group(1) + m.group(2) + ":" + REDACTED, text)
    text = _redact_netrc(text)
    text = _flag_and_client_values(text)
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


def _sensitive_item(key: str, item: Any, secrets: tuple[str, ...], depth: int) -> Any:
    """The value under a credential key: kept when its shape is not a credential, else redacted.

    Scalars (numbers, booleans, strings) are judged by ``_value_is_credential``, so the
    descriptive relief applies to them too. A kept string still goes through the normal
    text redaction, so a known secret inside it is removed. An empty container is kept.
    Any other container under a credential key is replaced whole.
    """
    if isinstance(item, Mapping | list | tuple):
        if len(item) == 0:
            return {} if isinstance(item, Mapping) else []
        return REDACTED
    if isinstance(item, bool | int | float):
        return (
            REDACTED
            if _value_is_credential(key, str(item), bare_only=False, quoted=False)
            else item
        )
    if _value_is_credential(key, str(item), bare_only=False, quoted=True):
        return REDACTED
    return _redact(item, secrets, depth + 1)


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
            if is_sensitive_key(key_text) and item is not None:
                out[clean_key] = _sensitive_item(key_text, item, secrets, depth)
            elif isinstance(item, str) and _is_auth_key(key_text):
                out[clean_key] = _redact(_replace_bearer(item), secrets, depth + 1)
            else:
                out[clean_key] = _redact(item, secrets, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_redact(item, secrets, depth + 1) for item in value]
    return value


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Redact a structured value recursively. Never raises.

    Dict values under credential names are replaced whole. Numbers and booleans under
    a credential name are replaced only by the bare-value rule. Strings that hold JSON
    are parsed, redacted inside, and re-encoded. Every other string is redacted with
    :func:`redact_text`. If structural redaction fails, the fallback redacts the text
    of the value, and a non-text value is replaced by the marker. The unredacted
    value is never returned.
    """
    secret_tuple = tuple(secrets)
    try:
        return _redact(value, secret_tuple, 0)
    except (RecursionError, ValueError, TypeError, OverflowError):
        if isinstance(value, str):
            return redact_text(value, secret_tuple)
        return REDACTED
