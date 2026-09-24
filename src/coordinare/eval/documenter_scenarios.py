"""Documenter workflow scenario eval (spec 171 SC-006).

Default mode runs the workflow against fixture repositories with a stubbed model
and a local committer: deterministic, also exercised by pytest. ``--live`` swaps
in the LiteLLM gateway model and keeps the local committer (commits land in the
temporary repository only; nothing is pushed).

    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.documenter_scenarios
    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.documenter_scenarios --live --only feature
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from performer.workflows.budget import ModelReply

from performer.models import Stand
from performer.workflows.base import WorkflowMetrics
from performer.workflows.documenter import DocumenterWorkflow
from performer.workflows.toolkit import Toolkit
from tests.eval.documenter_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.documenter_scenarios.scoring import Score, score_run
from tests.eval.documenter_scenarios.stub_model import stub_model_for
from tests.unit.workflows.documenter._repo import (
    add_payments,
    head,
    local_committer,
    make_repo,
    sh,
    trivial_change,
)

_GIT = shutil.which("git") or "git"


def _mkdocs_repo(repo: Path) -> Path:
    (repo / "mkdocs.yml").write_text("site_name: demo\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "index.md").write_text("# demo\n\nWelcome. See [usage](usage.md).\n")
    (repo / "docs" / "usage.md").write_text("---\nkind: how-to\n---\n# Usage\n\nRun the app; navigation is configured in `mkdocs.yml`.\n")
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "add", "-A"], repo)
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "commit", "-qm", "mkdocs site"], repo)
    return repo


def _sphinx_repo(repo: Path) -> Path:
    (repo / "doc" / "source").mkdir(parents=True)
    (repo / "doc" / "source" / "conf.py").write_text("project = 'demo'\n")
    (repo / "doc" / "source" / "index.md").write_text("# demo\n\nWelcome.\n")
    (repo / "doc" / "source" / "usage.md").write_text("---\nkind: how-to\n---\n# Usage\n\nRun the app.\n")
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "add", "-A"], repo)
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "commit", "-qm", "sphinx tree"], repo)
    return repo


def _monorepo_change(repo: Path) -> str:
    sh(["git", "checkout", "-q", "-b", "feat/services"], repo)
    for name in ("web", "api"):
        (repo / "apps" / name).mkdir(parents=True)
    (repo / "apps" / "web" / "main.py").write_text("def serve() -> str:\n    return 'web'\n")
    (repo / "apps" / "web" / "package.json").write_text('{"name": "web"}\n')
    (repo / "apps" / "api" / "api.py").write_text("def route() -> str:\n    return 'api'\n")
    (repo / "apps" / "api" / "package.json").write_text('{"name": "api"}\n')
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "add", "-A"], repo)
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "commit", "-qm", "feat: services"], repo)
    return sh(["git", "diff", "main...HEAD"], repo)


def _build(fixture: Fixture, root: Path) -> tuple[Path, str]:
    repo = make_repo(root, with_wiki=fixture.with_wiki, agents_md=fixture.agents_md)
    if fixture.change == "payments":
        return repo, add_payments(repo)
    if fixture.change == "trivial":
        return repo, trivial_change(repo)
    if fixture.change == "mkdocs":
        return _mkdocs_repo(repo), add_payments(repo)
    if fixture.change == "sphinx":
        return _sphinx_repo(repo), add_payments(repo)
    if fixture.change == "monorepo":
        return repo, _monorepo_change(repo)
    for i in range(6):
        (repo / f"pkg{i}").mkdir()
        (repo / f"pkg{i}" / "__init__.py").write_text("x = 1\n" * (i + 1) * 10)
        (repo / f"pkg{i}" / "tests").mkdir()
        (repo / f"pkg{i}" / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "add", "-A"], repo)
    sh([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "commit", "-qm", "packages"], repo)
    return repo, ""


async def _run_command(cmd: str, cwd: Path | str | None, timeout_s: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout_s}s"
    return proc.returncode or 0, out.decode(errors="replace")


async def run_fixture(fixture: Fixture, *, live: bool, root: Path | None = None) -> tuple[Score, dict[str, Any]]:
    root = root or Path(tempfile.mkdtemp(prefix="documenter-eval-"))
    repo, diff = _build(fixture, root)
    before = head(repo)
    calls = {"i": 0}
    if live:
        from coordinare.eval.gateway import _call_model

        async def model_call(persona: str, content: list[dict[str, Any]], max_tokens: int) -> ModelReply:
            calls["i"] += 1
            return await _call_model(persona, content, max_tokens)
    else:
        stub = stub_model_for(fixture)

        async def model_call(persona: str, content: list[dict[str, Any]], max_tokens: int) -> ModelReply:
            calls["i"] += 1
            return await stub(persona, content, max_tokens)
    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_run_command, call_limit=20)
    score = SimpleNamespace(pr_diff=diff, documentation_brief=dict(fixture.brief), doc_mode=fixture.mode, issue_number=7, title=f"eval {fixture.name}",
                            description="Documenter eval fixture", workflow_env=dict(fixture.workflow_env), owner_repo=("eval", "repo"),
                            effective_github_token="unused", documenting_side_run=False)  # 415: evals run final reconciliation
    result = await DocumenterWorkflow(committer=local_committer).run(Stand(path=repo, branch="feat/eval"), score, toolkit)
    return score_run(fixture, result.report, repo, before, calls["i"], live=live), result.report


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    scores: list[Score] = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue
        score, report = await run_fixture(fixture, live=live)
        scores.append(score)
        docs = report.get("docs", {})
        durations = (report.get("workflow_metrics") or {}).get("step_durations_ms", {})
        print(f"{fixture.name:22s} {'PASS' if score.passed else 'FAIL'}  verdict={docs.get('verdict')}  planned={len(docs.get('plan') or [])}  written={len(docs.get('files_written') or [])}  "
              f"dropped={sum(1 for r in (docs.get('results') or []) if r.get('dropped'))}  steps_ms={json.dumps(durations)}")
        for note in score.notes:
            print(f"         - {note}")
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="use the LiteLLM gateway instead of the stub model")
    parser.add_argument("--only", choices=[f.name for f in FIXTURES])
    args = parser.parse_args(argv)
    scores = asyncio.run(run_all(live=args.live, only=args.only))
    passed = sum(1 for s in scores if s.passed)
    print(f"\n{passed}/{len(scores)} fixtures passed")
    return 0 if passed == len(scores) else 1


if __name__ == "__main__":
    sys.exit(main())
