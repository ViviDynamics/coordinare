"""Security intake (spec 170 FR-002).

The injected diff parsed into changed files with new-side hunks (the reviewer's
parser), truncation recorded, the implementation brief carried. No prior-comment
dispositions: every security round scans and analyses fresh. Pure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from performer.workflows.reviewer.diffparse import detect_truncation, diff_lines, parse_unified_diff, unread_beyond_truncation, unread_overflow_from_note
from performer.workflows.reviewer.models import ChangedFile

__all__ = ["SecurityIntake", "build_intake"]

_DIFF_EXCERPT_CHARS = 6000


@dataclass
class SecurityIntake:
    changed_files: list[ChangedFile]
    diff_text: str
    diff_truncated: bool
    brief: dict[str, Any]
    pr_diff_status: str = ""
    pr_changed_paths: list[str] = field(default_factory=list)
    pr_changed_paths_overflow: bool = False
    title: str = ""
    description: str = ""
    pr_url: str = ""
    unread_overflow: int = 0

    @property
    def changed_paths(self) -> list[str]:
        return [f.path for f in self.changed_files]

    def diff_line_list(self) -> list[str]:
        return diff_lines(self.diff_text)

    def as_text(self, scan_summary: str = "") -> str:
        """The survey's context: the card, the changed files, the scan findings, the diff head."""
        files = "\n".join(f"- {f.path} ({len(f.hunks)} hunk(s))" for f in self.changed_files) or "(no files parsed)"
        note = " The diff was truncated; unread files must be opened." if self.diff_truncated else ""
        scan = f"\n\nStatic scan findings:\n{scan_summary}" if scan_summary else ""
        return (
            f"Pull request under security review: {self.title}\n{self.description[:1500]}\n\n"
            f"Changed files:{note}\n{files}{scan}\n\nDiff (first {_DIFF_EXCERPT_CHARS} chars):\n{self.diff_text[:_DIFF_EXCERPT_CHARS]}"
        )


def build_intake(score: Any) -> SecurityIntake:
    diff_text = str(getattr(score, "pr_diff", "") or "")
    truncated = detect_truncation(diff_text)
    files = parse_unified_diff(diff_text)
    if truncated:
        files = unread_beyond_truncation(files, diff_text)
    overflow = unread_overflow_from_note(diff_text)
    brief = getattr(score, "implementation_brief", None)
    return SecurityIntake(
        changed_files=files, diff_text=diff_text, diff_truncated=truncated, unread_overflow=overflow,
        brief=dict(brief) if isinstance(brief, dict) and brief else {},
        pr_diff_status=str(getattr(score, "pr_diff_status", "") or ""),
        pr_changed_paths=[str(p) for p in (getattr(score, "pr_changed_paths", None) or [])],
        pr_changed_paths_overflow=bool(getattr(score, "pr_changed_paths_overflow", False)),
        title=str(getattr(score, "title", "") or ""), description=str(getattr(score, "description", "") or ""),
        pr_url=str(getattr(score, "pr_url", "") or ""),
    )
