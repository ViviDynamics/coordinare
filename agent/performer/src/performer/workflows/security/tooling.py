"""What to scan this repository with, and what the scanners said (#366).

Both questions were answered by constants: a fixed `semgrep + bandit` pair, and
one hand-written normalizer per tool. Both were human judgement in 2016 -- a
security-minded pair opening an unfamiliar codebase decides what to reach for,
and then reads what it printed.

The constants produced a false pass. bandit is Python-only; on a Ruby repository
it exits 0, finds nothing, and reports:

    {"results": [], "errors": [{"filename": "./spec/week_spec.rb",
                                "reason": "syntax error while parsing AST from file"}]}

`run_scan` read only ``results`` and discarded ``errors``, so "examined nothing"
and "examined everything and found nothing" were the same answer. The website
symphony is Ruby, so every security verdict it produced had that property.

There is no stack-agnostic shortcut for this. bandit states its abstention in
``errors``; semgrep, given a file it cannot handle, reports ``errors: 0`` and
``skipped: 0`` -- nothing at all. Any check for "did this tool actually apply"
therefore needs per-tool knowledge, which is the constant again. Reading the
output is the only answer that generalises, so tool selection and output
reading are one change rather than two.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

__all__ = ["ScanTool", "ScanPlan", "ExaminedFile", "ScanReading", "plan_persona", "read_persona"]


class ScanTool(BaseModel):
    """One scanner the model chose to run here.

    ``argv`` is executed. That is the point of #366 and it is worth being
    explicit about, because it looks alarming out of context and the obvious
    "fix" would undo the change:

    - It is exec'd as a list (``create_subprocess_exec(*argv)``), never through
      a shell, so there is no quoting or injection surface. ``cwd`` is the
      repository and the timeout is ``budgets.scan_timeout_s``.
    - An allow-list of permitted scanners is precisely the constant this issue
      removed. It is what made bandit run against Ruby and report a clean
      result. Constraining the model to tools coordinare already knows about
      means coordinare deciding what can be scanned, which is the thing #364
      forbids.
    - The performer is an ephemeral container that already runs model-authored
      code by design: the implementer writes source and tests and executes
      them. Naming a scanner's argv is strictly less capability than that, so
      this adds no new class of risk to the sandbox -- it inherits the one the
      sandbox already exists to contain.

    The containment is the container, not a list of approved binaries.
    """

    model_config = {"extra": "forbid"}

    name: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    argv: list[str] = Field(min_length=1)
    why: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class ScanPlan(BaseModel):
    """The scanning the model judges appropriate for this repository."""

    model_config = {"extra": "forbid"}

    tools: list[ScanTool] = Field(default_factory=list)
    #: Required when ``tools`` is empty. An empty plan holds the card rather
    #: than passing it, so the reason reaches the operator.
    nothing_applies: Annotated[str, StringConstraints(max_length=500)] = ""


class ExaminedFile(BaseModel):
    """A file a tool did or did not manage to read."""

    model_config = {"extra": "forbid"}

    path: str
    examined: bool
    #: Why not, in the tool's own words, when ``examined`` is false.
    reason: Annotated[str, StringConstraints(max_length=300)] = ""


class ScanReading(BaseModel):
    """What one scanner actually reported.

    ``coverage`` is the load-bearing field and the reason this schema exists: a
    tool that examined none of the files it was given has abstained, and an
    abstention must never read as a clean result.
    """

    model_config = {"extra": "forbid"}

    findings: list[dict] = Field(default_factory=list)
    coverage: list[ExaminedFile] = Field(default_factory=list)
    summary: Annotated[str, StringConstraints(max_length=500)] = ""

    def examined_paths(self) -> list[str]:
        return [c.path for c in self.coverage if c.examined]

    def unexamined(self) -> list[ExaminedFile]:
        return [c for c in self.coverage if not c.examined]


def plan_persona() -> str:
    """Ask what scanning applies here. Names no tool and no language."""
    return (
        "You are a security engineer opening an unfamiliar repository.\n\n"
        "Decide what static security scanning is appropriate for THIS codebase, "
        "given its languages, frameworks and layout. Name each tool and the exact "
        "argv to run it with, preferring machine-readable output (JSON) so the "
        "results can be read back.\n\n"
        "Only name tools you have reason to believe are installed in a general "
        "purpose CI image. Do not name a tool that cannot read this codebase's "
        "languages -- a scanner that examines nothing is worse than no scanner, "
        "because its silence looks like a clean result.\n\n"
        "If no static security scanning meaningfully applies here, return an "
        "empty tools list and say why in 'nothing_applies'. That holds the card "
        "for a human, which is the correct outcome; it is never a pass."
    )


def read_persona() -> str:
    """Ask what a scanner reported, including what it could not read."""
    return (
        "You are reading the raw output of a security scanner.\n\n"
        "Report two things.\n\n"
        "1. findings: the security issues the tool reported. For each, give "
        "path, line (0 if unknown), category, problem, why_blocking, evidence "
        "(the offending code, may be empty), severity (critical|high|medium|low) "
        "and routing (implementer|architect).\n\n"
        "2. coverage: for EVERY file the tool was given, whether it actually "
        "examined that file. Many scanners report files they could not parse in "
        "an errors block, and some report nothing at all about files they "
        "skipped -- if the output gives you no evidence the tool read a file, "
        "mark it examined=false and say so.\n\n"
        "This second part matters more than the first. A tool that examined "
        "nothing and a tool that examined everything and found nothing produce "
        "similar-looking output, and treating them the same way is how an "
        "unscanned repository gets a passing security verdict."
    )
