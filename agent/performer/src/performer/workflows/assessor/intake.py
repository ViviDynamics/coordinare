"""Assessor intake step (spec 166 FR-001, US1): assembles the card context.

No model call. Reads the card, acceptance criteria, and clarification history
from the score. Logs the answered-round count and returns the intake text.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from performer.backends._clarifications import clarification_comment_lines
from performer.workflows._text import normalize_tokens


@dataclass
class Intake:
    """The card context for the assessor workflow."""

    title: str
    description: str
    criteria: list[str] = field(default_factory=list)
    clarifications: list[dict] = field(default_factory=list)
    answered_rounds: int = 0
    human_comments: list[dict] = field(default_factory=list)

    def as_text(self) -> str:
        """Render intake as text for the model prompt."""
        parts = [f"# Card: {self.title}", self.description.strip() or "(no description)"]
        if self.criteria:
            parts.append("## Acceptance criteria\n" + "\n".join(f"- {c}" for c in self.criteria))
        if self.clarifications:
            qa = "\n".join(
                f"- Q: {c.get('question', '')}\n  A: {c.get('answer', '') or '(awaiting answer)'}"
                for c in self.clarifications
                if isinstance(c, dict)
            )
            if qa:
                parts.append("## Clarifications\n" + qa)
        parts.extend("\n".join(clarification_comment_lines(comment)) for comment in self.human_comments)
        return "\n\n".join(parts)


def build_intake(score) -> Intake:
    """Assemble intake from score.

    Merges clarifications and prior_clarifications, expands plural questions
    entries into separate rounds, deduplicates by normalized question (latest
    non-blank answer wins), counts answered rounds (both question and answer
    non-blank).

    Args:
        score: The dispatch Score.

    Returns:
        Intake with title, description, criteria, clarifications, answered_rounds.
    """
    title = str(getattr(score, "title", "") or "")
    description = str(getattr(score, "description", "") or "")
    criteria = [str(c) for c in (getattr(score, "acceptance_criteria", None) or [])]

    clarifications = getattr(score, "clarifications", None) or []
    prior_clarifications = getattr(score, "prior_clarifications", None) or []

    seen_questions = {}
    merged = []
    human_comments = []

    for c in list(clarifications) + list(prior_clarifications):
        if not isinstance(c, dict):
            continue
        if clarification_comment_lines(c):
            human_comments.append(dict(c))

        # Handle plural questions entry: expand into separate rounds
        questions_list = c.get("questions")
        if questions_list and isinstance(questions_list, list):
            # Expand each question into a separate round with empty answer
            for q in questions_list:
                if isinstance(q, str) and q.strip():
                    norm_q = normalize_tokens(q)
                    if norm_q not in seen_questions:
                        seen_questions[norm_q] = len(merged)
                        merged.append({"question": q, "answer": ""})
            continue

        # Handle single question entry
        q = c.get("question", "")
        if not q:
            continue

        norm_q = normalize_tokens(q)
        if norm_q not in seen_questions:
            seen_questions[norm_q] = len(merged)
            merged.append(dict(c))
        else:
            idx = seen_questions[norm_q]
            new_answer = c.get("answer", "")
            # Latest non-blank answer wins
            if new_answer:
                merged[idx]["answer"] = new_answer

    answered_rounds = sum(
        1 for c in merged
        if c.get("answer", "").strip()
    )

    return Intake(
        title=title,
        description=description,
        criteria=criteria,
        clarifications=merged,
        answered_rounds=answered_rounds,
        human_comments=human_comments,
    )
