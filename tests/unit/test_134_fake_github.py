"""Spec 134 — FakeGitHubService behaviour, exercised in isolation (no daemon).

Backed by a real local bare git repo with a passing branch (``feat/ok``) and a
failing branch (``feat/bad``) so the real-pytest CI path is genuinely run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinare.services.fake_github import FakeGitHubService


def _run(cmd: list[str], cwd: Path) -> None:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{' '.join(cmd)} failed: {r.stderr}"


def _commit_all(work: Path, message: str) -> None:
    _run(["git", "add", "-A"], work)
    _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", message], work)


@pytest.fixture
def bench_repo(tmp_path: Path) -> Path:
    """A bare repo: main (passing), feat/ok (passing), feat/bad (failing)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(["git", "init", "-b", "main", "."], seed)
    (seed / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (seed / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _commit_all(seed, "base")

    bare = tmp_path / "repo.git"
    _run(["git", "init", "--bare", str(bare)], tmp_path)
    _run(["git", "remote", "add", "origin", str(bare)], seed)
    _run(["git", "push", "origin", "main"], seed)

    # feat/ok — adds a function + a passing test.
    _run(["git", "checkout", "-b", "feat/ok"], seed)
    (seed / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    (seed / "test_mul.py").write_text("from calc import mul\n\ndef test_mul():\n    assert mul(2, 3) == 6\n")
    _commit_all(seed, "add mul")
    _run(["git", "push", "origin", "feat/ok"], seed)

    # feat/bad — a failing test.
    _run(["git", "checkout", "main"], seed)
    _run(["git", "checkout", "-b", "feat/bad"], seed)
    (seed / "test_bad.py").write_text("from calc import add\n\ndef test_bad():\n    assert add(1, 1) == 3\n")
    _commit_all(seed, "add failing test")
    _run(["git", "push", "origin", "feat/bad"], seed)

    return bare


def _fake(bench_repo: Path, tmp_path: Path, **kw) -> FakeGitHubService:
    return FakeGitHubService(
        bare_repo_path=bench_repo,
        human_reviewers=["human1"],
        work_dir=tmp_path / "work",
        **kw,
    )


# ---- board -----------------------------------------------------------------

async def test_poll_board_returns_real_snapshot_shape(bench_repo: Path, tmp_path: Path) -> None:
    fake = _fake(bench_repo, tmp_path)
    fake.seed_card("PVTI_1", title="Add coupons", body="do it", status="TODO", issue_number=1)
    fake.seed_card("PVTI_2", title="Fix bug", body="fix", status="IN_PROGRESS", issue_number=2)

    board = await fake.poll_board()
    assert set(board) == {
        "snapshot", "titles", "descriptions", "issue_numbers", "issue_urls",
        "item_labels", "item_assignees", "content_node_ids", "pr_urls",
    }
    assert board["snapshot"]["TODO"] == ["PVTI_1"]
    assert board["snapshot"]["IN_PROGRESS"] == ["PVTI_2"]
    assert board["titles"]["PVTI_1"] == "Add coupons"
    assert board["issue_numbers"]["PVTI_2"] == 2


async def test_move_card_mutates_column(bench_repo: Path, tmp_path: Path) -> None:
    fake = _fake(bench_repo, tmp_path)
    fake.seed_card("PVTI_1", title="t", body="b", status="TODO", issue_number=1)
    await fake.move_card("PVTI_1", "DONE")
    board = await fake.poll_board()
    assert board["snapshot"]["DONE"] == ["PVTI_1"]
    assert board["snapshot"]["TODO"] == []


# ---- git-backed reads ------------------------------------------------------

async def test_git_reads_against_bare_repo(bench_repo: Path, tmp_path: Path) -> None:
    fake = _fake(bench_repo, tmp_path)
    fake.seed_card("PVTI_1", title="t", body="b", issue_number=1)
    pr_id = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/ok")
    url = fake._prs[pr_id]["url"]

    assert await fake.branch_exists("feat/ok") is True
    assert await fake.branch_exists("no/such") is False

    diff, files = await fake.get_pr_diff(url)
    assert "mul" in diff
    assert "test_mul.py" in files

    assert set(await fake.compare_changed_files(url, "main", "feat/ok")) >= {"test_mul.py"}

    content = await fake.get_file_content("o", "r", "calc.py", ref="feat/ok")
    assert content is not None and "def mul" in content
    assert await fake.get_file_content("o", "r", "missing.py", ref="main") is None
    assert await fake.get_file_blob_sha("o", "r", "calc.py", ref="main")


# ---- reviews / mergeability / CI-gated approval ----------------------------

async def test_ci_rollup_reflects_real_pytest(bench_repo: Path, tmp_path: Path) -> None:
    fake = _fake(bench_repo, tmp_path)
    fake.seed_card("PVTI_1", title="t", body="b", issue_number=1)
    ok = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/ok")
    bad = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/bad")

    checks = fake._pr_checks_service_cache[("o", "r")]  # our fake PrChecksService
    ok_rollup = await checks.get_pr_check_rollup(fake._prs[ok]["pr_number"])
    bad_rollup = await checks.get_pr_check_rollup(fake._prs[bad]["pr_number"])

    assert ok_rollup.checks[0].name == "pytest"
    assert ok_rollup.checks[0].is_required is True
    assert ok_rollup.checks[0].conclusion == "success"
    assert bad_rollup.checks[0].conclusion == "failure"
    assert await fake.get_required_status_checks("o", "r", "main") == {"pytest"}


async def test_pr_checks_cache_always_wins_over_real_service(bench_repo: Path, tmp_path: Path) -> None:
    # Reproduces the node's guard: `getattr(...) or {}` then `key not in cache`.
    fake = _fake(bench_repo, tmp_path)
    cache = getattr(fake, "_pr_checks_service_cache", None) or {}
    assert cache is fake._pr_checks_service_cache  # not discarded by `or {}`
    assert ("any", "key") in cache  # node will NOT overwrite with a real service


async def test_approver_withholds_until_ci_green_and_in_review(bench_repo: Path, tmp_path: Path) -> None:
    approver = lambda s: s["ci_green"] and s["card_status"] == "IN_REVIEW"  # noqa: E731
    fake = _fake(bench_repo, tmp_path, approver=approver)
    fake.seed_card("PVTI_1", title="t", body="b", status="TODO", issue_number=1)
    pr = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/ok")

    # CI green but not yet IN_REVIEW → withhold.
    m1 = await fake.check_mergeability(pr)
    assert m1["review_decision"] == "REVIEW_REQUIRED"
    assert m1["mergeable"] is False

    # Advance to IN_REVIEW → approve, and a human review appears.
    await fake.move_card("PVTI_1", "IN_REVIEW")
    m2 = await fake.check_mergeability(pr)
    assert m2["review_decision"] == "APPROVED"
    assert m2["mergeable"] is True
    assert m2["mergeable_raw"] == "MERGEABLE"
    reviews = await fake.get_pr_reviews(pr)
    assert reviews and reviews[0]["author_login"] == "human1"
    assert reviews[0]["state"] == "APPROVED"
    # review dict shape matches the real _parse_review_node output
    assert set(reviews[0]) == {"id", "author_login", "state", "body", "submitted_at", "comments", "commit_oid"}


async def test_failing_ci_blocks_approval(bench_repo: Path, tmp_path: Path) -> None:
    approver = lambda s: s["ci_green"]  # noqa: E731
    fake = _fake(bench_repo, tmp_path, approver=approver)
    fake.seed_card("PVTI_1", title="t", body="b", status="IN_REVIEW", issue_number=1)
    pr = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/bad")
    m = await fake.check_mergeability(pr)
    assert m["review_decision"] == "REVIEW_REQUIRED"
    assert m["mergeable"] is False


# ---- merge -----------------------------------------------------------------

async def test_squash_merge_is_a_real_local_merge(bench_repo: Path, tmp_path: Path) -> None:
    fake = _fake(bench_repo, tmp_path, approver=lambda s: True)
    fake.seed_card("PVTI_1", title="t", body="b", status="IN_REVIEW", issue_number=1)
    pr = fake.open_pr(issue_item_id="PVTI_1", head_ref="feat/ok")

    result = await fake.squash_merge(pr)
    assert result["merged"] is True
    assert result["merge_commit"]["oid"]

    # main now contains the merged change; the PR is no longer open.
    merged_calc = await fake.get_file_content("o", "r", "calc.py", ref="main")
    assert merged_calc is not None and "def mul" in merged_calc
    assert await fake.branch_has_open_pr("feat/ok") is False


# ---- never-raise invariant -------------------------------------------------

async def test_fake_never_raises_on_unknown_or_degraded_input(bench_repo: Path, tmp_path: Path) -> None:
    fake = _fake(bench_repo, tmp_path)
    assert await fake.check_mergeability("nope") == {"mergeable": False, "reason": "missing_pr"}
    assert await fake.get_pr_review_context("nope") == {
        "reviews": [], "review_threads": [], "head_oid": "", "review_decision": ""
    }
    assert await fake.get_pr_reviews("nope") == []
    assert await fake.get_pr_diff("https://fake/o/r/pull/999") == ("", [])
    assert await fake.squash_merge("nope") == {"merged": False}
    assert await fake.branch_exists("does/not/exist") is False
    assert await fake.find_pr_for_issue("no-issue") is None
    assert await fake.get_file_content("o", "r", "nope.py", ref="main") is None
    # post_comment (latent swallowed call on the real service) is tolerated + recorded
    await fake.post_comment(1, "hi")
    assert any(e["kind"] == "post_comment" for e in fake.events)


@pytest.mark.parametrize("kwargs,expected", [({}, "fake-token"), ({"git_auth_enabled": True}, "fake-token"),
                                             ({"git_auth_enabled": False}, "")])
async def test_git_auth_can_be_disabled_without_changing_real_mode_default(
    tmp_path: Path, kwargs: dict[str, bool], expected: str,
) -> None:
    fake = FakeGitHubService(bare_repo_path=tmp_path / "repo.git", work_dir=tmp_path / "work", **kwargs)
    assert await fake.current_token() == expected
    assert await fake._current_token() == expected
