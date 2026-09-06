"""Intake (spec 165 FR-003): assemble everything the architect must read.

No model call. The card, its acceptance criteria, the assessor's assessment
when it exists in the workspace, clarifications humans answered, and the
repository's own agent instructions when present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_MAX_DOC_CHARS = 6000
_AGENT_FILES = ("AGENTS.md", "CLAUDE.md")


@dataclass
class Intake:
    title: str
    description: str
    criteria: list[str] = field(default_factory=list)
    assessment: str = ""
    clarifications: list[dict] = field(default_factory=list)
    agent_instructions: str = ""

    def as_text(self) -> str:
        parts = [f"# Card: {self.title}", self.description.strip() or "(no description)"]
        if self.criteria:
            parts.append("## Acceptance criteria\n" + "\n".join(f"- {c}" for c in self.criteria))
        if self.clarifications:
            qa = "\n".join(
                f"- Q: {c.get('question', '')}\n  A: {c.get('answer', '')}"
                for c in self.clarifications
                if isinstance(c, dict) and c.get("answer")
            )
            if qa:
                parts.append("## Clarifications answered by humans\n" + qa)
        if self.assessment:
            parts.append("## Assessment\n" + self.assessment)
        if self.agent_instructions:
            parts.append("## Repository agent instructions\n" + self.agent_instructions)
        return "\n\n".join(parts)


def _read_capped(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:_MAX_DOC_CHARS]


def build_intake(score, workspace: Path) -> Intake:
    """Pure assembly from *score* and files already in *workspace*."""
    folder = getattr(score, "doc_folder", None)
    assessment = ""
    candidates = []
    if folder:
        candidates.append(Path(workspace) / str(folder) / "assessment.md")
    issue = getattr(score, "issue_number", None)
    cards_dir = Path(workspace) / "docs" / "cards"
    if issue and cards_dir.is_dir():
        candidates.extend(sorted(cards_dir.glob(f"{issue}-*/assessment.md")))
    for cand in candidates:
        if cand.is_file():
            assessment = _read_capped(cand)
            break
    agent_text = ""
    for name in _AGENT_FILES:
        p = Path(workspace) / name
        if p.is_file():
            agent_text = _read_capped(p)
            break
    return Intake(
        title=str(getattr(score, "title", "") or ""),
        description=str(getattr(score, "description", "") or ""),
        criteria=[str(c) for c in (getattr(score, "acceptance_criteria", None) or [])],
        assessment=assessment,
        clarifications=[c for c in (getattr(score, "clarifications", None) or []) if isinstance(c, dict)],
        agent_instructions=agent_text,
    )
