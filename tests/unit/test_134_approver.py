"""Spec 134 — gates_green approval policy (US3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinare.bench.approver import gates_green
from coordinare.services.fake_github import FakeGitHubService


def test_gates_green_withholds_until_ci_green_and_in_review() -> None:
    assert gates_green({"ci_green": False, "card_status": "IN_REVIEW"}) is False
    assert gates_green({"ci_green": True, "card_status": "IN_PROGRESS"}) is False
    assert gates_green({"ci_green": True, "card_status": "TODO"}) is False
    assert gates_green({}) is False


def test_gates_green_approves_when_both_conditions_hold() -> None:
    assert gates_green({"ci_green": True, "card_status": "IN_REVIEW"}) is True


def _run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{' '.join(cmd)} failed: {r.stderr}"


@pytest.fixture
def bench_repo(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init", "-b", "main", "."], seed)
    (seed / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (seed / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _run(["git", "add", "-A"], seed)
    _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base"], seed)
    bare = tmp_path / "repo.git"
    _run(["git", "init", "--bare", str(bare)], tmp_path)
    _run(["git", "remote", "add", "origin", str(bare)], seed)
    _run(["git", "push", "origin", "main"], seed)
    _run(["git", "checkout", "-b", "feat/ok"], seed)
    (seed / "test_more.py").write_text("from calc import add\n\ndef test_more():\n    assert add(2, 2) == 4\n")
    _run(["git", "add", "-A"], seed)
    _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "more"], seed)
    _run(["git", "push", "origin", "feat/ok"], seed)
    return bare


async def test_gates_green_wired_into_fake_reaches_approved(bench_repo: Path, tmp_path: Path) -> None:
    fake = FakeGitHubService(
        bare_repo_path=bench_repo,
        human_reviewers=["human1"],
        approver=gates_green,
        work_dir=tmp_path / "work",
    )
    fake.seed_card("PVTI_1", title="t", body="b", status="IN_PROGRESS", issue_number=1)
    pr = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/ok")

    # Not in review yet → withheld.
    assert (await fake.check_mergeability(pr))["review_decision"] == "REVIEW_REQUIRED"

    # Reaches the review column → gates_green approves.
    await fake.move_card("PVTI_1", "IN_REVIEW")
    m = await fake.check_mergeability(pr)
    assert m["review_decision"] == "APPROVED"
    assert m["mergeable"] is True
