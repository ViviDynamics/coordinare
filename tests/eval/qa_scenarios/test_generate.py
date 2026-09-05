"""Deterministic tests for the fixture generator itself (T044).

The generator is ordinary code, so it belongs in the normal suite. Only the
SCORING run against a real model is nondeterministic, and that lives in the
eval runner, which CI never invokes.
"""
from __future__ import annotations

import ast
import subprocess

from tests.eval.qa_scenarios.fixtures import FIXTURE_DIR, load, load_all
from tests.eval.qa_scenarios.generate import build


def _sh(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


def test_all_six_scenarios_are_present_and_cover_every_verdict_class():
    fixtures = {f.name: f for f in load_all()}
    assert set(fixtures) == {
        "healthy", "regression", "misplaced", "incomplete", "cosmetic_noop", "env_broken",
    }
    verdicts = {f.expected_verdict for f in fixtures.values()}
    assert verdicts == {"pass", "fail", "environment_error"}


def test_a_scenario_builds_two_commits(tmp_path):
    fixture = load(FIXTURE_DIR / "healthy.yaml")
    repo, base_sha, head_sha = build(fixture, tmp_path)

    assert base_sha != head_sha
    assert (repo / ".git").is_dir()
    assert len(_sh(repo, "log", "--oneline").strip().splitlines()) == 2


def test_the_regression_scenario_actually_removes_the_password_field(tmp_path):
    """The fixture must contain the defect it claims, or the scenario proves
    nothing about QA."""
    fixture = load(FIXTURE_DIR / "regression.yaml")
    repo, base_sha, head_sha = build(fixture, tmp_path)

    diff = _sh(repo, "diff", base_sha, head_sha)
    assert "Password" in diff, "the diff must show the password field changing"
    assert "Workspace" in (repo / "app.py").read_text()
    assert "'Password'" not in (repo / "app.py").read_text()


def test_the_cosmetic_scenario_changes_nothing_functional(tmp_path):
    fixture = load(FIXTURE_DIR / "cosmetic_noop.yaml")
    repo, base_sha, head_sha = build(fixture, tmp_path)

    diff = _sh(repo, "diff", base_sha, head_sha)
    assert diff.strip(), "there must be a diff, or it is not a claim at all"
    assert "Workspace" not in diff, "a cosmetic no-op must not add the feature"


def test_the_env_broken_scenario_does_not_import(tmp_path):
    fixture = load(FIXTURE_DIR / "env_broken.yaml")
    repo, _, _ = build(fixture, tmp_path)

    result = subprocess.run(
        ["python3", "-c", "import app"], cwd=repo, capture_output=True, text=True
    )
    assert result.returncode != 0, "the app must genuinely fail to boot"


def test_a_file_absent_from_head_is_deleted_not_carried_over(tmp_path):
    """How the regression scenarios express a silently removed file."""
    from tests.eval.qa_scenarios.fixtures import ScenarioFixture

    fixture = ScenarioFixture(
        name="deletion_probe",
        base_files={"keep.py": "x = 1\n", "gone.py": "y = 2\n"},
        head_files={"keep.py": "x = 1\n"},
        expected_verdict="fail",
    )
    repo, _, _ = build(fixture, tmp_path)

    assert (repo / "keep.py").exists()
    assert not (repo / "gone.py").exists()


def test_no_fixture_ships_a_committed_git_directory():
    """R8: a git repository nested inside this repository is a hazard for
    clones, tooling and CI checkout. Fixtures are generated, never committed."""
    assert not list(FIXTURE_DIR.rglob(".git")), "fixtures must not contain a .git directory"
    assert all(p.suffix == ".yaml" for p in FIXTURE_DIR.iterdir())


def test_head_is_on_a_branch_so_a_merge_base_actually_exists(tmp_path):
    """Found by the first full six-scenario eval run.

    The generator committed base AND head to main, so `git merge-base HEAD main`
    returned HEAD itself. The baseline worktree was then byte-identical to head,
    the delta was always empty, and the regression scenario reported a FALSE
    PASS on a change that deletes the password field.

    A real pull request has a feature branch while the base branch stays behind.
    The fixtures must model that or they cannot exercise regression detection.
    """
    fixture = load(FIXTURE_DIR / "regression.yaml")
    repo, base_sha, head_sha = build(fixture, tmp_path)

    head = _sh(repo, "rev-parse", "HEAD").strip()
    main = _sh(repo, "rev-parse", "main").strip()

    assert head == head_sha
    assert main == base_sha, "main must stay at the base commit"
    assert head != main, "head and base must diverge or there is no delta"

    merge_base = _sh(repo, "merge-base", "HEAD", "main").strip()
    assert merge_base == base_sha, "the merge-base must be the base commit"


def test_the_base_worktree_differs_from_head(tmp_path):
    """The end-to-end property that actually matters: checking out the
    merge-base must yield DIFFERENT source than head."""
    fixture = load(FIXTURE_DIR / "regression.yaml")
    repo, base_sha, _head = build(fixture, tmp_path)

    def _fields(source: str) -> list[str]:
        """The rendered FIELDS list, not raw source text.

        Matching on source text is fragile: the renderer names field types in
        its own branches, so 'Password' appears in every version of the file
        whether or not the form actually has that field.
        """
        line = next(ln for ln in source.splitlines() if ln.startswith("FIELDS = "))
        return ast.literal_eval(line.split("=", 1)[1].strip())

    base_fields = _fields(_sh(repo, "show", f"{base_sha}:app.py"))
    head_fields = _fields((repo / "app.py").read_text())

    assert "Password" in base_fields, "the base form has the field"
    assert "Password" not in head_fields, "the head form dropped it"
    assert "Workspace" in head_fields, "and gained the claimed one"
