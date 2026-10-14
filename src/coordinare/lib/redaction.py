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


# 148: the client secrets the transcript redactor (lib/transcript_redaction.py,
# spec 129) does not cover: a password given as a flag or an argument to a client
# program, and `pass:` in openssl. They are found by one linear pass over the words
# of a text. A pattern that scanned from each program name to the end of its line
# was quadratic on repeated program names. The performer keeps an identical copy;
# a parity test compares the two.
_WORD_RE = re.compile(r"\S+")
_BREAK_CHARS = ";|&"
_MAX_QUOTED_WORDS = 8
_OPENSSL_LABEL = "pass:"
_CLIENTS = {"sshpass": "sshpass", "redis-cli": "redis", "sqlcmd": "sqlcmd", "htpasswd": "htpasswd"}
# (client, word) pairs whose next word is the secret.
_SECRET_FLAGS = frozenset({
    ("docker", "-p"), ("sshpass", "-p"), ("redis", "-a"), ("sqlcmd", "-P"), ("gh", "--body"), ("gh", "-b"),
})
_AWS_SECRET_STEMS = ("secret", "token", "password")
# htpasswd -b FILE USER PASSWORD: the password is the third word after the program.
_HTPASSWD_PASSWORD_POSITION = 3


class _ClientSecrets:
    """One left-to-right pass over the words of a text (148)."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.spans: list[tuple[int, int]] = []
        self._reset()

    def _reset(self) -> None:
        self.recent: list[str] = []
        self.client = ""
        self.secret_next = False
        self.batch = False
        self.positional = 0

    def scan(self) -> list[tuple[int, int]]:
        words = list(_WORD_RE.finditer(self.text))
        previous_end = 0
        index = 0
        while index < len(words):
            word = words[index]
            if "\n" in self.text[previous_end:word.start()]:
                self._reset()
            if self.secret_next:
                index = self._secret(words, index)
                previous_end = words[index - 1].end()
                continue
            previous_end = word.end()
            self._word(word.group(), word.start(), word.end())
            if any(char in word.group() for char in _BREAK_CHARS):
                self._reset()
            index += 1
        return self.spans

    def _secret(self, words: list[re.Match[str]], index: int) -> int:
        """Record the secret at *index* (a quoted run is taken whole); return the next index."""
        self.secret_next = False
        first = words[index].group()
        last = index
        if first[:1] in ("'", '"') and not (len(first) > 1 and first.endswith(first[0])):
            for offset in range(index + 1, min(len(words), index + _MAX_QUOTED_WORDS)):
                if words[offset].group().endswith(first[0]):
                    last = offset
                    break
        self.spans.append((words[index].start(), words[last].end()))
        return last + 1

    def _word(self, value: str, start: int, end: int) -> None:
        base = value.rsplit("/", 1)[-1].lower()
        self.recent = [*self.recent, base][-3:]
        if value.startswith(_OPENSSL_LABEL):
            self.spans.append((start + len(_OPENSSL_LABEL), end))
        elif self.client == "htpasswd":
            self._htpasswd_word(value, start, end)
        elif self.recent[-2:] == ["docker", "login"]:
            self.client = "docker"
        elif self.recent[-2:] == ["gh", "secret"]:
            self.client = "gh"
        elif self.recent[-3:] == ["aws", "configure", "set"]:
            self.client = "aws"
        elif base in _CLIENTS:
            self.client = _CLIENTS[base]
            self.batch = False
            self.positional = 0
        elif (self.client == "aws" and any(stem in base for stem in _AWS_SECRET_STEMS)) or (
            self.client,
            value,
        ) in _SECRET_FLAGS:
            self.secret_next = True

    def _htpasswd_word(self, value: str, start: int, end: int) -> None:
        """htpasswd -b FILE USER PASSWORD: the third positional word is the password."""
        if value.startswith("-"):
            self.batch = self.batch or value == "-b"
            return
        self.positional += 1
        if self.batch and self.positional == _HTPASSWD_PASSWORD_POSITION:
            self.spans.append((start, end))


def redact_client_forms(text: str) -> str:
    """Replace each client secret found by ``_ClientSecrets`` (148)."""
    pieces: list[str] = []
    last = 0
    for start, end in _ClientSecrets(text).scan():
        pieces.append(text[last:start])
        pieces.append("[REDACTED]")
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def redact_command_text(text: str) -> str:
    """Redact one command, tool argument or event text (148, 129).

    In order: the value patterns (``redact_secrets``), the transcript redactor
    (``transcript_redaction.redact_text``), then the client-secret forms the
    transcript redactor does not cover. Used for the activity feed and for every
    performer event that reaches the dashboard. Callers redact the whole value
    before they clip it.

    Documented limits: a bare literal with no marker (``echo s3cr3tvalue123``)
    cannot be recognised by pattern. Streamed deltas are redacted one chunk at a
    time, so a token split across two chunks is not caught.
    """
    text = redact_secrets(text)
    text = transcript_redact_text(text)
    return redact_client_forms(text)


def redact_state_value(value: Any) -> Any:
    """A value about to enter coordinare state, with its free text redacted (203).

    Strings get ``redact_command_text``. Lists and dicts are walked, so a list of
    questions or a dict of block details is covered in one call. Anything else
    passes through. Every writer of a dashboard-visible text field calls this, so
    the board comment, notifications, the snapshot and MCP all see the redacted form.
    The walk is linear in the text.
    """
    if isinstance(value, str):
        return redact_command_text(value)
    if isinstance(value, Mapping):
        return {key: redact_state_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_state_value(item) for item in value]
    return value


_EVENT_TEXT_FIELDS = ("text", "detail", "arg", "tool")


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
