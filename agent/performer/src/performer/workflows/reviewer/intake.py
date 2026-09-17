"""Reviewer intake (spec 169 FR-002).

Parses the injected diff into changed files with new-side hunks, records
whether the diff was truncated, normalises the relayed open comments to id,
path, line and body, and carries the implementation brief. Pure: no model call,
no command.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from performer.workflows.reviewer.diffparse import detect_truncation, diff_lines, parse_unified_diff, unread_beyond_truncation, unread_overflow_from_note
from performer.workflows.reviewer.models import ChangedFile, PriorComment

__all__ = ["Intake", "build_intake", "normalise_prior_comments"]

_DIFF_EXCERPT_CHARS = 6000


@dataclass
class Intake:
    changed_files: list[ChangedFile]
    diff_text: str
    diff_truncated: bool
    prior_comments: list[PriorComment]
    brief: dict[str, Any]
    pr_diff_status: str = ""
    title: str = ""
    description: str = ""
    pr_url: str = ""
    unread_overflow: int = 0

    @property
    def brief_present(self) -> bool:
        return bool(self.brief)

    @property
    def changed_paths(self) -> list[str]:
        return [f.path for f in self.changed_files]

    def diff_line_list(self) -> list[str]:
        return diff_lines(self.diff_text)

    def as_text(self) -> str:
        """The survey's context: what the card is, which files changed, the diff head."""
        files = "\n".join(f"- {f.path} ({len(f.hunks)} hunk(s))" for f in self.changed_files) or "(no files parsed)"
        excerpt = self.diff_text[:_DIFF_EXCERPT_CHARS]
        note = " The diff was truncated; unread files must be opened." if self.diff_truncated else ""
        return (
            f"Pull request under review: {self.title}\n{self.description[:1500]}\n\n"
            f"Changed files:{note}\n{files}\n\nDiff (first {_DIFF_EXCERPT_CHARS} chars):\n{excerpt}"
        )


def normalise_prior_comments(raw: Any) -> list[PriorComment]:
    """Relayed comments arrive as loose dicts; give each an id, path, line and body.

    Entries without an id are numbered by position so dispositions can name them.
    Missing path or line become "" and 0 (the data-model's body-anchored shape).
    """
    out: list[PriorComment] = []
    if not isinstance(raw, list):
        return out
    for index, item in enumerate(raw):
        if isinstance(item, str):
            item = {"body": item}
        if not isinstance(item, dict):
            continue
        body = str(item.get("body") or "").strip()
        if not body:
            continue
        ident = item.get("id") or item.get("comment_id") or f"c{index + 1}"
        try:
            line = int(item.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        out.append(PriorComment(id=str(ident), path=str(item.get("path") or ""), line=max(line, 0), body=body[:4000]))
    return out


def build_intake(score: Any) -> Intake:
    diff_text = str(getattr(score, "pr_diff", "") or "")
    truncated = detect_truncation(diff_text)
    files = parse_unified_diff(diff_text)
    if truncated:
        files = unread_beyond_truncation(files, diff_text)
    overflow = unread_overflow_from_note(diff_text)
    brief = getattr(score, "implementation_brief", None)
    return Intake(
        changed_files=files,
        diff_text=diff_text,
        diff_truncated=truncated,
        unread_overflow=overflow,
        prior_comments=normalise_prior_comments(getattr(score, "relay_feedback", None)),
        pr_diff_status=str(getattr(score, "pr_diff_status", "") or ""),
        brief=dict(brief) if isinstance(brief, dict) and brief else {},
        title=str(getattr(score, "title", "") or ""),
        description=str(getattr(score, "description", "") or ""),
        pr_url=str(getattr(score, "pr_url", "") or ""),
    )
