"""Personas for the documenter workflow (spec 171 FR-017).

One WRITE persona per page kind. Code fills the placeholders; the writing rules
the research converged on ride in every persona (see specs/171-documenter-workflow/research.md).
"""
from __future__ import annotations

from performer.workflows.documenter.models import REQUIRED_HEADINGS

__all__ = ["WRITING_RULES", "KIND_GUIDANCE", "render_write_persona"]

WRITING_RULES = """## Writing rules (non-negotiable)
- One kind of page. This page is `{kind}`; write only that kind, with exactly the required headings and nothing that belongs to another kind.
- Ground every claim in a path you were shown: write repository paths in backticks (`src/module/file.py`). A path that does not exist drops the whole page, so cite only what the evidence shows.
- Lead with what a newcomer needs to act; put background after it.
- Prefer explicit negative rules ("never call X from Y") and one correct example over paragraphs of prose.
- Plain, repeated sentence shapes: subject, verb, object. No idioms, no filler, no marketing.
- Do not narrate the code file by file; the code already says what it does. Say why it is this way and where to change it.
- Record why, not what changed. Never write a changelog, a card reference, or a date-stamped entry.
- Link to the canonical wiki page for a concept instead of repeating it: `[Title](other-page.md)`.
- Markdown: exactly one `# Title`; headings unique, in sentence case, without links or code; fenced code blocks with a language; link text says what the target is (never "here").
- Keep the page between 400 and {max_chars} characters.
- If the evidence shows the page is already correct, answer `unchanged` with a one-line reason instead of rewriting it.
"""

KIND_GUIDANCE = {
    "explanation": "An explanation page answers how this part fits and why it is the way it is. Required headings, in order: {headings}. Under `Where to change it`, name the files to start from and the tests that matter.",
    "how-to": "A how-to page gets a competent reader from a known start to a done state. Required headings, in order: {headings}. Steps are numbered, each with the exact command in a fenced block; `Verify` names the check that proves it worked.",
    "reference": "A reference page states facts, free of interpretation and advice. Use a table or list where every entry names its path in backticks with one line of what it is. No steps, no rationale.",
    "decision": "A decision page is an architecture decision record. Required headings, in order: {headings}. Consequences must include the costs, not only the benefits. Status is one of proposed, accepted, superseded.",
}


def render_write_persona(*, path: str, kind: str, current_content: str, say: list[str], evidence: str, changed_hunks: str, max_chars: int, allow_retire: bool = False, docs_root: str = "docs/wiki") -> str:
    headings = ", ".join(f"`## {h}`" for h in REQUIRED_HEADINGS.get(kind, ())) or "(none required)"
    guidance = KIND_GUIDANCE.get(kind, KIND_GUIDANCE["reference"]).format(headings=headings)
    say_text = "\n".join(f"- {s}" for s in say if s) or "(the brief says nothing specific; write from the evidence)"
    frontmatter = f"---\nkind: {kind}\n---"
    status = "The page does not exist yet: write it." if not current_content else "The page exists: rewrite it, or answer unchanged when it is already right."
    return (
        f"You maintain the project documentation under `{docs_root}/`. Write the page `{path}`. {status}\n\n"
        f"## Kind\n{guidance}\n\nStart the page with this frontmatter exactly:\n```\n{frontmatter}\n```\n\n"
        + WRITING_RULES.format(kind=kind, max_chars=max_chars)
        + f"\n## What the brief asks this page to say\n{say_text}\n\n"
        f"## Current page\n{current_content or '(the page does not exist yet)'}\n\n"
        f"## Evidence (the only paths you may cite)\n{evidence or '(no evidence gathered)'}\n\n"
        f"## Changed hunks relevant to this page\n{changed_hunks or '(none)'}\n\n"
        "## Output\nReturn JSON: {\"action\": \"write\", \"content\": \"<the whole page>\", \"reason\": \"\"} or "
        "{\"action\": \"unchanged\", \"content\": \"\", \"reason\": \"<why the page is already right>\"}"
        + (" or {\"action\": \"retire\", \"content\": \"\", \"reason\": \"<why nothing it cites exists any more>\"}" if allow_retire else "")
        + ". No other keys."
        + ("" if allow_retire else " The page is to be written or left unchanged; do not answer retire.")
    )
