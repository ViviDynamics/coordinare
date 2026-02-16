from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from coordinare.models.card import CardStatus  # noqa: TC001


class Notification(BaseModel):
    card_title: str
    card_status: CardStatus
    previous_status: CardStatus
    task_description: str
    open_questions: list[str] = Field(default_factory=list)
    commit_summary: str | None = None
    pr_url: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def as_text(self) -> str:
        lines = [
            f"Card: {self.card_title}",
            f"Transition: {self.previous_status} -> {self.card_status}",
            f"Task: {self.task_description}",
        ]
        if self.open_questions:
            lines.append("Open Questions:")
            lines.extend([f"- {q}" for q in self.open_questions])
        if self.commit_summary:
            lines.append(f"Commit Summary: {self.commit_summary}")
        if self.pr_url:
            lines.append(f"PR: {self.pr_url}")
        return "\n".join(lines)
