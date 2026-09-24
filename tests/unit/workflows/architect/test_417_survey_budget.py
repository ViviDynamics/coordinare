"""417: refused commands must not consume the survey's command budget, and the
survey/intake accept a package root from the card (workflow_env) so monorepos
are surveyed and read from the right subtree."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.architect.intake import build_intake, resolve_package_root
from performer.workflows.architect.survey import (
    ProposedCommand,
    SurveyBudget,
    SurveyProposal,
    run_survey_step,
)


class _Toolkit:
    def __init__(self, commands: list[str]):
        self._commands = commands
        self.ran: list[tuple[str, Path]] = []

    async def call_model(self, **kwargs):
        return SurveyProposal(
            commands=[
                ProposedCommand(command=c, reason="r") for c in self._commands
            ],
        )

    async def run_command(self, command: str, cwd: Path, timeout_s: int):
        self.ran.append((command, Path(cwd)))
        return SimpleNamespace(exit_code=0, output_excerpt="ok")


def _score(**env) -> SimpleNamespace:
    return SimpleNamespace(workflow_env=dict(env))


@pytest.mark.asyncio
async def test_refused_commands_do_not_consume_budget():
    toolkit = _Toolkit(["rm -rf x", "cat README.md", "ls docs"])
    budget = SurveyBudget(max_commands=2, max_output_chars=100)
    survey = await run_survey_step(toolkit, "intake", Path("/ws"), budget)
    executed = [r for r in survey.records if r.allowed]
    refused = [r for r in survey.records if not r.allowed]
    assert len(executed) == budget.max_commands
    assert len(refused) == 1
    assert executed[0].command == "cat README.md"


@pytest.mark.asyncio
async def test_all_refused_proposals_still_leave_budget_for_later_ones():
    toolkit = _Toolkit(["bundle install", "npm install", "curl x", "wc -l f"])
    budget = SurveyBudget(max_commands=1, max_output_chars=100)
    survey = await run_survey_step(toolkit, "intake", Path("/ws"), budget)
    executed = [r for r in survey.records if r.allowed]
    refused = [r for r in survey.records if not r.allowed]
    proposed_count = 4
    assert len(refused) == proposed_count - budget.max_commands
    assert [r.command for r in executed] == ["wc -l f"]


def test_resolve_package_root_from_the_card():
    root = resolve_package_root(_score(ARCHITECT_PACKAGE_ROOT="services/api"), Path("/ws"))
    assert root == Path("/ws/services/api")


def test_resolve_package_root_defaults_to_workspace():
    assert resolve_package_root(_score(), Path("/ws")) == Path("/ws")


def test_resolve_package_root_refuses_to_escape_the_workspace():
    assert resolve_package_root(_score(ARCHITECT_PACKAGE_ROOT="../elsewhere"), Path("/ws")) == Path("/ws")


def test_intake_reads_agent_files_from_the_package_root(tmp_path):
    (tmp_path / "services" / "api").mkdir(parents=True)
    (tmp_path / "services" / "api" / "AGENTS.md").write_text("package rules", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("root rules", encoding="utf-8")
    intake = build_intake(_score(ARCHITECT_PACKAGE_ROOT="services/api"), tmp_path)
    assert intake.agent_instructions == "package rules"


def test_intake_reads_card_docs_from_the_package_root(tmp_path):
    (tmp_path / "services" / "api" / "docs" / "cards" / "12-thing").mkdir(parents=True)
    (tmp_path / "services" / "api" / "docs" / "cards" / "12-thing" / "assessment.md").write_text(
        "assessment body", encoding="utf-8",
    )
    score = SimpleNamespace(
        workflow_env={"ARCHITECT_PACKAGE_ROOT": "services/api"},
        issue_number=12,
        doc_folder=None,
        assessment=None,
    )
    intake = build_intake(score, tmp_path)
    assert "assessment body" in (intake.assessment or "")


@pytest.mark.asyncio
async def test_survey_commands_run_in_the_package_root():
    toolkit = _Toolkit(["ls"])
    budget = SurveyBudget(max_commands=1, max_output_chars=100)
    await run_survey_step(toolkit, "intake", Path("/ws/services/api"), budget)
    assert toolkit.ran[0][1] == Path("/ws/services/api")
