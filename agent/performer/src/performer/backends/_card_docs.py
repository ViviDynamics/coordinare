"""Shared helpers for surfacing per-card documentation paths in prompts.

Every role benefits from knowing where the card's working docs live
(``docs/cards/{issue}-{slug}/``). The architect writes ``plan.md`` there;
later roles may add ``tasks.md``, ``research.md``, ``contracts/``, prior
QA notes, etc. Smarter cloud models tend to discover this folder on
their own; smaller local models often do not. This module produces a
single ``## Card Documentation`` prompt section that points any backend
at the folder, with the section omitted when the folder is absent so we
do not lie to first-touch roles.

The path convention matches ``performer.main._doc_folder`` and is
re-derived from ``score`` so it works even when the coordinare snapshot
lost ``card["plan_path"]``.
"""

from __future__ import annotations

import re
from pathlib import Path

from performer.models import Score


def card_doc_folder(score: Score) -> str | None:
    """Return ``docs/cards/{issue}-{slug}`` for *score*, or None.

    Mirrors ``performer.main._doc_folder``: lowercases the title,
    collapses non-alphanumeric runs to hyphens, trims to 20 chars,
    falls back to ``untitled``. Prefixes with ``{issue_number}-``
    when an issue number is present.
    """
    title_slug = (
        re.sub(r"[^a-z0-9]+", "-", (score.title or "").lower()).strip("-")[:20]
        or "untitled"
    )
    issue_num = getattr(score, "issue_number", None)
    if issue_num:
        return f"docs/cards/{issue_num}-{title_slug}"
    return f"docs/cards/{title_slug}"


def card_docs_prompt_section(
    score: Score, stand_path: Path | None
) -> list[str]:
    """Build the ``## Card Documentation`` prompt block.

    Returns an empty list when ``stand_path`` is not provided or the
    convention folder does not exist on disk — callers append the
    returned list to their parts buffer unconditionally.
    """
    folder = card_doc_folder(score)
    if not folder or stand_path is None:
        return []
    abs_folder = stand_path / folder
    if not abs_folder.is_dir():
        return []
    return [
        "",
        "## Card Documentation",
        "",
        f"Working docs for this card live under `{folder}/` on this branch.",
        "Read any files present there before acting. Typical contents:",
        "- `plan.md` (architecture plan)",
        "- `tasks.md` (work breakdown)",
        "- `research.md` / `contracts/` (when produced)",
        "- prior role outputs (assessment, QA notes, etc.)",
        "",
    ]
