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


def qa_findings_prompt_section(score: Score) -> list[str]:
    """Build the ``## QA Findings`` prompt block (spec 164 FR-012).

    The repair brief from the previous QA round. It was registered in the
    payload contract, carried into card_context, and declared on Score -- and
    then never rendered into any prompt, so the implementer received it and
    ignored it. The contract's Change Protocol step 4 ("add the field to the
    relevant _build_task_prompt if the AI needs it") is the step that was
    skipped; this is that step, shared so all backends render it identically.

    Only the implementer gets it: handing QA its own prior findings would have
    it grade its own round. Duplicates (one per surface plus the judge's note)
    collapse on (category, criterion, expected). Reports; never prescribes a
    fix (FR-014) -- the section renders what failed and how to reproduce it,
    and nothing else.
    """
    findings = getattr(score, "qa_findings", None) or []
    if getattr(score, "role", "") != "implementing" or not findings:
        return []

    seen: set[tuple[str, str, str]] = set()
    lines: list[str] = [
        "",
        "## QA Findings (from the previous QA round — address ALL of these)",
        "",
        "Each entry says what failed and how to reproduce it. Fix the cause; the "
        "reproducing command is how you confirm the fix before handing back.",
        "",
    ]
    for f in findings:
        if not isinstance(f, dict):
            continue
        key = (
            str(f.get("category", "")),
            str(f.get("criterion", "")),
            str(f.get("expected", "")),
        )
        if key in seen:
            continue
        seen.add(key)

        head = f"- [{f.get('severity', 'high')}] {f.get('category', 'finding')}"
        if f.get("criterion"):
            head += f" — criterion: {f['criterion']}"
        lines.append(head)
        if f.get("expected"):
            lines.append(f"  - expected: {f['expected']}")
        if f.get("observed"):
            lines.append(f"  - observed: {f['observed']}")
        loc = f.get("file")
        if loc:
            lines.append(f"  - where: `{loc}`" + (f":{f['line']}" if f.get("line") else ""))
        ev = f.get("evidence")
        if isinstance(ev, dict) and ev.get("command"):
            lines.append(
                f"  - evidence: `{ev['command']}` exited {ev.get('exit_code')}"
            )
        if f.get("repro_command"):
            lines.append(f"  - reproduce: `{f['repro_command']}`")
    return lines
