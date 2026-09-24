"""Survey (spec 165 FR-004): the model proposes, the allow-list decides.

One model call returns a short list of read-only commands with reasons. Each
is checked by :mod:`allowlist` before it runs; refused commands are recorded
but do not consume the command budget (417), and never execute. Outputs are
truncated per command, and the survey stops at the command budget however
many the model asked for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict, Field

from performer.workflows.architect.allowlist import is_allowed
from performer.workflows.architect.personas import SURVEY
from performer.workflows.budget import Budget

log = structlog.get_logger(__name__)

DEFAULT_MAX_COMMANDS = 12
DEFAULT_MAX_OUTPUT_CHARS = 4000
_COMMAND_TIMEOUT_S = 60


class ProposedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str = Field(..., max_length=400)
    reason: str = Field(..., max_length=200)


class SurveyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    commands: list[ProposedCommand] = Field(default_factory=list, max_length=24)


@dataclass
class SurveyRecord:
    command: str
    allowed: bool
    refusal_reason: str = ""
    exit_code: int | None = None
    output: str = ""
    truncated: bool = False


@dataclass
class SurveyBudget:
    max_commands: int = DEFAULT_MAX_COMMANDS
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS

    @classmethod
    def from_env(cls, env: dict[str, str] | None) -> "SurveyBudget":
        env = env or {}
        return cls(
            max_commands=_positive_int(env.get("ARCHITECT_SURVEY_MAX_COMMANDS"), DEFAULT_MAX_COMMANDS),
            max_output_chars=_positive_int(env.get("ARCHITECT_SURVEY_MAX_OUTPUT_CHARS"), DEFAULT_MAX_OUTPUT_CHARS),
        )


def _positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class Survey:
    records: list[SurveyRecord] = field(default_factory=list)

    @property
    def refused(self) -> int:
        return sum(1 for r in self.records if not r.allowed)

    def as_text(self) -> str:
        parts = []
        for r in self.records:
            if not r.allowed:
                parts.append(f"$ {r.command}\n(refused: {r.refusal_reason})")
                continue
            tail = " [truncated]" if r.truncated else ""
            parts.append(f"$ {r.command}\n(exit {r.exit_code}){tail}\n{r.output}")
        return "\n\n".join(parts) if parts else "(no survey output)"


async def run_survey_step(toolkit, intake_text: str, workspace: Path, budget: SurveyBudget) -> Survey:
    """Run the survey in *workspace* — the repository root, or the card's
    package root for a monorepo (417)."""
    proposal = await toolkit.call_model(
        persona=SURVEY,
        schema=SurveyProposal,
        content=[{"type": "text", "text": (
            f"{intake_text}\n\nPropose at most {budget.max_commands} read-only commands, "
            f"most useful first. The working directory is {workspace}."
        )}],
        budget=Budget.for_step("survey"),
    )
    survey = Survey()
    executed = 0
    for proposed in proposal.commands:
        if executed >= budget.max_commands:
            break
        ok, reason = is_allowed(proposed.command)
        if not ok:
            # 417: a refusal is recorded for the record but costs no budget —
            # otherwise one bad proposal batch starves the survey entirely.
            log.info("architect.survey", command=proposed.command[:120], allowed=False, reason=reason)
            survey.records.append(SurveyRecord(command=proposed.command, allowed=False, refusal_reason=reason))
            continue
        result = await toolkit.run_command(proposed.command, cwd=workspace, timeout_s=_COMMAND_TIMEOUT_S)
        output = result.output_excerpt or ""
        truncated = len(output) > budget.max_output_chars
        log.info(
            "architect.survey", command=proposed.command[:120], allowed=True,
            exit_code=result.exit_code, chars=min(len(output), budget.max_output_chars), truncated=truncated,
        )
        survey.records.append(SurveyRecord(
            command=proposed.command, allowed=True, exit_code=result.exit_code,
            output=output[: budget.max_output_chars], truncated=truncated,
        ))
        executed += 1
    return survey
