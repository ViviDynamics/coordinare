"""Scored QA scenario eval (spec 164 T046, FR-019/FR-022).

NOT A CI GATE. This calls a real model, takes minutes, and is nondeterministic
by nature. Constitution Principle II forbids flaky tests, so this is kept out of
the test suite entirely and run on demand as a measurement harness. See
``tests/eval/qa_scenarios/README.md`` and plan.md's Constitution Check.

Usage:
    python -m coordinare.eval.qa_scenarios --repeats 5
"""
from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _free_port() -> int:
    """An ephemeral port, so parallel scenarios never collide."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ScenarioOutcome:
    """One run of one scenario."""

    verdict: str
    named: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ScenarioScore:
    name: str
    expected: str
    runs: list[ScenarioOutcome] = field(default_factory=list)

    @property
    def correct(self) -> int:
        return sum(1 for r in self.runs if self.is_correct(r))

    def is_correct(self, run: ScenarioOutcome) -> bool:
        """Qualitative (FR-019): the verdict CLASS must be right, and a failure
        must name the correct artifact. Exact model wording is never asserted --
        the output is nondeterministic and pinning strings would make this a
        test of phrasing rather than of judgement."""
        return run.verdict == self.expected

    def named_correctly(self, must_name: list[str]) -> int:
        if not must_name:
            return self.correct
        return sum(
            1
            for r in self.runs
            if self.is_correct(r)
            and all(any(m.lower() in n.lower() for n in r.named) for m in must_name)
        )

    def line(self, must_name: list[str]) -> str:
        total = len(self.runs)
        reason = self.named_correctly(must_name)
        suffix = ""
        if must_name and reason < self.correct:
            suffix = f"   ({self.correct - reason} right verdict, wrong reason)"
        return (
            f"{self.name:<16}{reason}/{total}   expected={self.expected}"
            f"{suffix}"
        )


def classify(report: dict) -> ScenarioOutcome:
    """Map a QA report onto a verdict class."""
    if report.get("environment_error"):
        return ScenarioOutcome(verdict="environment_error")
    # must_name is matched ONLY against fields that name the artifact
    # positively -- `criterion` and `expected`, which our code builds from the
    # criterion text or the lost element. Never `actual`/`observed`: that is
    # model prose, and "no password issue found" contains "password". Round-two
    # review showed that substring letting a false pass score as a true one.
    named = [
        str(f.get("expected", "")) + " " + str(f.get("criterion", ""))
        for f in report.get("failures", [])
    ] + [
        str(f.get("expected", "")) + " " + str(f.get("criterion", ""))
        for f in report.get("qa_findings", [])
    ]
    return ScenarioOutcome(verdict="pass" if report.get("passed") else "fail", named=named)


async def run_scenario(fixture, workdir: Path, repeats: int) -> ScenarioScore:
    from tests.eval.qa_scenarios.generate import build

    score = ScenarioScore(name=fixture.name, expected=fixture.expected_verdict)
    for i in range(repeats):
        repo, base_sha, _head = build(fixture, workdir / f"run{i}")
        try:
            report = await _run_qa(fixture, repo, base_sha, port=_free_port())
            score.runs.append(classify(report))
        except Exception as exc:
            score.runs.append(
                ScenarioOutcome(verdict="error", error=f"{type(exc).__name__}: {exc}")
            )
    return score


async def _run_qa(fixture, repo: Path, base_sha: str, port: int) -> dict:
    """Drive the real QA workflow against a generated repository."""
    from performer.workflows.qa import QAWorkflow

    from coordinare.eval.gateway import gateway_toolkit  # thin LiteLLM binding

    class _Stand:
        path = str(repo)

    class _Score:
        acceptance_criteria: ClassVar[list[str]] = list(fixture.criteria)
        description = fixture.claimed_change
        base_branch = "main"
        role = "qa"
        pr_diff = ""

    from performer.workflows.qa.boot import AppBoot

    toolkit = gateway_toolkit(repo)

    def _boot(workspace):
        # The fixture apps are plain Python HTTP servers, which
        # infer_app_start_command deliberately does not recognise. An explicit
        # command is exactly the override QA_APP_START_COMMAND exists for.
        return AppBoot(
            env={
                "PORT": str(port),
                "QA_APP_START_COMMAND": f"{sys.executable} app.py",
                "PATH": os.environ.get("PATH", ""),
            },
            workspace=workspace,
            boot_timeout=20.0,
            poll_interval=0.5,
        )

    result = await QAWorkflow(boot_factory=_boot).run(_Stand(), _Score(), toolkit)
    return result.report | {"qa_findings": result.findings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--only", help="run a single scenario by name")
    args = parser.parse_args(argv)

    from tests.eval.qa_scenarios.fixtures import load_all

    fixtures = [f for f in load_all() if not args.only or f.name == args.only]
    if not fixtures:
        print(f"no scenario matching {args.only!r}")
        return 2

    with tempfile.TemporaryDirectory(prefix="qa-eval-") as tmp:
        workdir = Path(tmp)
        scores = [
            asyncio.run(run_scenario(f, workdir / f.name, args.repeats)) for f in fixtures
        ]

    print(f"\nQA scenario eval — {args.repeats} repeats per scenario\n")
    for fixture, score in zip(fixtures, scores, strict=True):
        print("  " + score.line(fixture.must_name))
    print(
        "\nThis is a measurement, not a gate. A scenario dropping below its "
        "recorded rate is the signal to investigate.\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
