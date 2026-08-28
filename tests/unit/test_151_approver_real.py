"""Spec 151 (US4, T026) — in a real run the approver keys on real gate outcomes:
gates_green approves only on ci_green + IN_REVIEW, and withholds when CI is red
(no APPROVED review, check_mergeability.mergeable == False) even at IN_REVIEW.
"""
from __future__ import annotations

from pathlib import Path

from coordinare.bench.approver import gates_green
from coordinare.bench.fixtures import apply_solution_branch, materialize_repo, tiny_fixture
from coordinare.services.fake_github import FakeGitHubService


def test_gates_green_proxy_requires_in_review() -> None:
    # A failed upstream gate keeps the card out of IN_REVIEW → gates_green withholds.
    assert gates_green({"ci_green": True, "card_status": "IN_PROGRESS"}) is False
    assert gates_green({"ci_green": True, "card_status": "IN_REVIEW"}) is True
    # Red CI withholds even at IN_REVIEW.
    assert gates_green({"ci_green": False, "card_status": "IN_REVIEW"}) is False


async def test_red_ci_withholds_green_ci_approves(tmp_path: Path) -> None:
    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    # main already ships the FAILING acceptance test (multiply unimplemented). The
    # red branch adds a distinct no-op change but leaves multiply unimplemented →
    # red CI; solution_files implement it → green CI.
    apply_solution_branch(bare, "bench/red", {"NOTES.md": "wip\n"}, tmp_path / "sol")
    apply_solution_branch(bare, "bench/green", fx.solution_files, tmp_path / "sol")
    fake = FakeGitHubService(
        bare_repo_path=bare, human_reviewers=["reviewer1"],
        approver=gates_green, work_dir=tmp_path / "ci",
    )
    fake.seed_card("PVTI_R", title="red", body="b", status="IN_REVIEW", issue_number=1)
    fake.seed_card("PVTI_G", title="green", body="b", status="IN_REVIEW", issue_number=2)
    red = fake.open_pr(issue_item_id="PVTI_R", head_ref="bench/red")
    green = fake.open_pr(issue_item_id="PVTI_G", head_ref="bench/green")

    m_red = await fake.check_mergeability(red)
    assert m_red["mergeable"] is False           # withheld: CI red
    assert m_red["review_decision"] != "APPROVED"

    m_green = await fake.check_mergeability(green)
    assert m_green["mergeable"] is True           # approved: CI green + IN_REVIEW
    assert m_green["review_decision"] == "APPROVED"
