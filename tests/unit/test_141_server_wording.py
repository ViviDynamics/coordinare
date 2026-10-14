"""#141: the server text a user reads says run or try, never attempt or session.

Every prose string in the modules that write user-facing text is scanned. A string is prose when
it has a space in it. Identifiers, dict keys, log event names and machine prefixes are one word,
or are listed below with the reason they stay.
"""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "coordinare"

_MODULES = (
    "graph/nodes/monitor/verdict.py",
    "graph/nodes/monitor/body.py",
    "graph/nodes/monitor/gates.py",
    "graph/nodes/monitor/repair.py",
    "graph/nodes/handle_system_error.py",
    "graph/nodes/dispatch_performer.py",
    "services/wiki_init.py",
    "services/env_cache.py",
    "cancel.py",
)

_RETIRED = re.compile(r"\b(attempts?|sessions?|attempted)\b", re.IGNORECASE)

# Prose that keeps the word because it is not the performer run a user sees. Each entry says why.
_ALLOWED = {
    # The Git verb in a push rejection: the branch "attempted to modify" a protected path.
    "Git push was rejected because the branch attempted to modify ",
}


def _prose_strings(path: Path) -> list[tuple[int, str]]:
    """Prose literals: a string with a space, and the text pieces of an f-string with a space or a count.

    Docstrings are skipped (they are not shown to anyone), and a single word is a machine key.
    """
    found: list[tuple[int, str]] = []
    source = path.read_text(encoding="utf-8")
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        text = token.string
        if token.type == tokenize.STRING:
            if text.startswith(('"""', "'''")) or " " not in text or not _RETIRED.search(text):
                continue
        elif token.type == tokenize.FSTRING_MIDDLE:
            if not _RETIRED.search(text) or not (" " in text or "(" in text):
                continue
        else:
            continue
        found.append((token.start[0], text))
    return found


def test_no_user_facing_string_says_attempt_or_session() -> None:
    offenders: list[str] = []
    for relative in _MODULES:
        for line, text in _prose_strings(_SRC / relative):
            if any(allowed in text for allowed in _ALLOWED):
                continue
            offenders.append(f"{relative}:{line}: {text[:90]}")
    assert offenders == []
