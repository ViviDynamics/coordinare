"""074 T056 — Coordinare must ship no default path-class globs (SC-005 / FR-004).

Project owners define their own path-class taxonomy. Hard-coding globs like
``*.md`` or ``src/auth/**`` in coordinare source would leak project assumptions
into the tool — this static check prevents regressions.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "coordinare"

# Patterns that indicate a hard-coded path-class glob (deliberately narrow:
# we look for glob metacharacters anchored against extension or path-ish
# tokens, not arbitrary asterisks in docstrings).
GLOB_RE = re.compile(
    r"""(?x)
    (?<!\w)
    (?:
        \*\*/        |  # **/foo
        [A-Za-z0-9_./-]+/\*\*[A-Za-z0-9_./*-]*  |  # docs/**/*
        \*\.[a-zA-Z]{1,8}                          # *.md, *.py
    )
    """,
)

# Tokens whose presence in the matched glob makes it clearly a path-class
# glob (vs. e.g. a wildcard import or unrelated star expression).
PATHCLASS_HINTS = ("*.md", "*.py", "*.ts", "*.tsx", "*.yml", "*.yaml", "*.json", "**")

# Files allowed to mention globs (config schema doc strings, test fixtures,
# or the classifier prompt template that documents the wire format).
# - utils/doc_dedup.py uses ``*.md`` to walk workspace documentation; this is a
#   doc-dedup utility, not a path-class taxonomy.
ALLOWLIST_SUFFIXES = ("utils/doc_dedup.py",)


def _iter_py_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


@pytest.mark.parametrize("path", _iter_py_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_hardcoded_path_globs(path: Path) -> None:
    rel = path.relative_to(SRC)
    if any(str(rel).endswith(suf) for suf in ALLOWLIST_SUFFIXES):
        pytest.skip(f"allowlisted: {rel}")
    text = path.read_text(encoding="utf-8")
    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Skip comments/docstring-style lines that document examples.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for m in GLOB_RE.finditer(line):
            tok = m.group(0)
            if not any(h in tok for h in PATHCLASS_HINTS):
                continue
            # Allow string literals that are *example* globs inside docstrings
            # (heuristic: triple-quoted context). We approximate by looking
            # for a triple-quote on the same line or skipping if the file is
            # a __init__.py with no executable globs anywhere.
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Hard-coded path-class globs found in coordinare source — projects own "
        "their path-class taxonomy (FR-004):\n  " + "\n  ".join(offenders)
    )
