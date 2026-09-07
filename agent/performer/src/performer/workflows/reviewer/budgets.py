"""Reviewer budgets (spec 169 FR-003, FR-005).

Read from the role's ``workflow_env``; every value is a positive integer with
a default, so a bad value falls back rather than failing the round.
"""
from __future__ import annotations

from dataclasses import dataclass

from performer.workflows.architect.survey import SurveyBudget

__all__ = ["ReviewerBudgets"]

DEFAULT_SURVEY_MAX_COMMANDS = 12
DEFAULT_SURVEY_MAX_OUTPUT_CHARS = 4000
DEFAULT_MAX_FINDINGS = 30


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class ReviewerBudgets:
    survey_max_commands: int = DEFAULT_SURVEY_MAX_COMMANDS
    survey_max_output_chars: int = DEFAULT_SURVEY_MAX_OUTPUT_CHARS
    max_findings: int = DEFAULT_MAX_FINDINGS

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> "ReviewerBudgets":
        env = env or {}
        return cls(
            survey_max_commands=_positive_int(env.get("REVIEWER_SURVEY_MAX_COMMANDS"), DEFAULT_SURVEY_MAX_COMMANDS),
            survey_max_output_chars=_positive_int(env.get("REVIEWER_SURVEY_MAX_OUTPUT_CHARS"), DEFAULT_SURVEY_MAX_OUTPUT_CHARS),
            max_findings=min(_positive_int(env.get("REVIEWER_MAX_FINDINGS"), DEFAULT_MAX_FINDINGS), DEFAULT_MAX_FINDINGS),
        )

    def survey_budget(self) -> SurveyBudget:
        return SurveyBudget(max_commands=self.survey_max_commands, max_output_chars=self.survey_max_output_chars)
