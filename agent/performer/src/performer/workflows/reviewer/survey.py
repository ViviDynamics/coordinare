"""Reviewer survey (spec 169 FR-003, FR-004).

Reuses the spec-165 survey step: the model proposes read-only commands, the
allow-list admits or refuses each, and every command and refusal is recorded.
The reviewer adds coverage tracking: a changed file counts as opened when an
admitted command names it. When the diff was truncated or a changed file was
neither fully in the diff nor opened, exactly one more survey turn runs,
naming the unread files.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import structlog

from performer.workflows.architect.survey import Survey, SurveyBudget, run_survey_step
from performer.workflows.reviewer.intake import Intake
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.reviewer.personas import SURVEY_COVERAGE_PERSONA

log = structlog.get_logger(__name__)

__all__ = ["SurveyOutcome", "command_names_path", "mark_opened", "unread_files", "survey_output_lines", "run_reviewer_survey"]


@dataclass
class SurveyOutcome:
    survey: Survey
    coverage_pass_ran: bool = False
    coverage_pass_output: str = ""
    coverage_survey: Survey | None = None

    def records(self) -> list:
        out = list(self.survey.records)
        if self.coverage_survey is not None:
            out.extend(self.coverage_survey.records)
        return out

    def as_text(self) -> str:
        text = self.survey.as_text()
        if self.coverage_survey is not None:
            text += "\n\n## Coverage pass\n" + self.coverage_survey.as_text()
        return text


_TOKEN_SPLIT = re.compile(r"""[\s;|&()'"`<>]+""")


def command_names_path(command: str, path: str) -> bool:
    """True when *path* is a whole argument of the command: exact, ``./``-prefixed,
    or the object of a ``git show REV:path``. A substring is not enough:
    ``tests/test.py`` must not count as opening ``test.py`` (FR-009)."""
    for token in _TOKEN_SPLIT.split(command):
        if not token:
            continue
        if token == path or token == f"./{path}" or token.endswith(f":{path}"):
            return True
    return False


def mark_opened(changed_files: list[ChangedFile], survey: Survey) -> list[ChangedFile]:
    """A changed file is opened when an admitted command that ran names its path as an argument."""
    commands = [r.command for r in survey.records if r.allowed and r.exit_code == 0]
    out = []
    for f in changed_files:
        opened = f.opened_by_survey or any(command_names_path(cmd, f.path) for cmd in commands)
        out.append(f.model_copy(update={"opened_by_survey": opened}) if opened != f.opened_by_survey else f)
    return out


def unread_files(changed_files: list[ChangedFile]) -> list[str]:
    return [f.path for f in changed_files if not (f.fully_in_diff or f.opened_by_survey)]


def survey_output_lines(records) -> list[str]:
    lines: list[str] = []
    for r in records:
        if r.allowed and r.output:
            lines.extend(r.output.splitlines())
    return lines


async def run_reviewer_survey(toolkit, intake: Intake, workspace: Path, budget: SurveyBudget) -> tuple[SurveyOutcome, list[ChangedFile]]:
    """Survey, mark opened files, and run the single coverage pass when needed (FR-004)."""
    survey = await run_survey_step(toolkit, intake.as_text(), workspace, budget)
    files = mark_opened(intake.changed_files, survey)
    outcome = SurveyOutcome(survey=survey)
    unread = unread_files(files)
    if intake.diff_truncated or unread:
        names = "\n".join(f"- {p}" for p in unread) or "(the truncated tail of the diff)"
        prompt = SURVEY_COVERAGE_PERSONA.format(unread_files=names) + "\n\n" + intake.as_text()
        log.info("reviewer.coverage_pass", unread=len(unread), truncated=intake.diff_truncated)
        coverage = await run_survey_step(toolkit, prompt, workspace, budget)
        files = mark_opened(files, coverage)
        outcome.coverage_pass_ran = True
        outcome.coverage_survey = coverage
        outcome.coverage_pass_output = coverage.as_text()[-4000:]
    return outcome, files


def opened_paths(records, paths) -> set[str]:
    """Verify explicit content reads; listings/status and command chains are insufficient."""
    opened: set[str] = set()
    for record in records:
        if not record.allowed or record.exit_code != 0:
            continue
        try:
            lexer = shlex.shlex(record.command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            argv = list(lexer)
        except ValueError:
            continue
        if not argv or any(token and set(token) <= set(";&|") for token in argv):
            continue
        program = argv[0].rsplit("/", 1)[-1]
        for path in paths:
            if program in {"cat", "head", "tail", "sed"}:
                reads = path in argv[1:] or f"./{path}" in argv[1:]
            elif program == "git":
                git_args = argv[1:]
                if git_args and git_args[0] == "--no-pager":
                    git_args = git_args[1:]
                safe_flags = {"--no-pager", "--no-color", "--color=never", "--no-ext-diff", "--no-textconv"}
                show_args = [arg for arg in git_args[1:] if arg not in safe_flags]
                reads = bool(git_args and git_args[0] == "show" and len(show_args) == 1
                             and not show_args[0].startswith("-")
                             and show_args[0].endswith(f":{path}"))
            else:
                reads = False
            if reads:
                opened.add(path)
    return opened
