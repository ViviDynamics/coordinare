"""098: the write-scope guard, against real git repositories.

Issue #98: a QA turn wrote prose into ``tests/test_sample.py`` and the performer
published it. The guard's rules, as these tests pin them:

* Commits the QA agent made during the turn are checked against the turn's
  starting commit. Out-of-scope commits are removed from the branch.
* A tracked file changed in the working tree and not declared is restored from
  HEAD and reported as information. It does not fail the verdict.
* An untracked path is never committed, is left on disk, and is information.
* A declared new-file path is checked before anything is written. Glob and magic
  pathspecs, traversal, configuration and hook files, symlinked ancestors,
  directories, extensions, and case collisions are all refused by name.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from performer.models import Stand
from performer.workspace import WorkspaceSetupError
from performer.write_scope import (
    TrackedPaths,
    declared_path_refusal,
    dirty_paths,
    display_path,
    enforce_write_scope,
    head_paths,
    parse_porcelain_z,
)

BRANCH = "feat/remove-vowels"
REPORT = "docs/cards/74-remove-vowels/qa.md"
ORIGINAL_TEST = "def test_remove_vowels():\n    assert remove_vowels('abc') == 'bc'\n"
PROSE_NOTES = "QA verification notes: checked the vowel rule on three inputs.\n"
_GIT = shutil.which("git") or "git"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [_GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A clone with one commit: a source file, a test module, and a card folder."""
    _git(tmp_path, "init", "-q", "-b", BRANCH)
    _git(tmp_path, "config", "user.name", "fixture")
    _git(tmp_path, "config", "user.email", "fixture@example.invalid")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_sample.py").write_text(ORIGINAL_TEST)
    (tmp_path / "app.py").write_text("def remove_vowels(s):\n    return s\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain", "--untracked-files=all")


# ---------------------------------------------------------------------------
# Working tree and index
# ---------------------------------------------------------------------------


class TestWorkingTreeScope:
    async def test_a_tracked_change_not_declared_is_restored_as_information(
        self, repo: Path
    ) -> None:
        base = _head(repo)
        (repo / "tests" / "test_sample.py").write_text(PROSE_NOTES)
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.restored == ["tests/test_sample.py"]
        assert report.committed_out_of_scope == []
        assert report.reset_to is None
        assert (repo / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert _status(repo) == ""

    async def test_a_deleted_tracked_file_is_restored(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "app.py").unlink()
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.restored == ["app.py"]
        assert (repo / "app.py").exists()
        assert _status(repo) == ""

    async def test_an_untracked_stray_is_left_on_disk_and_not_committed(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "notes.txt").write_text(PROSE_NOTES)
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.restored == []
        assert report.left_out == ["notes.txt"]
        assert (repo / "notes.txt").read_text() == PROSE_NOTES
        assert _status(repo) == "?? notes.txt\n"

    async def test_a_staged_new_file_is_unstaged_and_kept_on_disk(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "tests" / "test_extra.py").write_text(PROSE_NOTES)
        _git(repo, "add", "--", "tests/test_extra.py")
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.left_out == ["tests/test_extra.py"]
        assert (repo / "tests" / "test_extra.py").exists()
        assert _status(repo) == "?? tests/test_extra.py\n"

    async def test_untracked_artifact_directories_are_left_alone(self, repo: Path) -> None:
        """Test runs leave coverage and build output behind. None of it is deleted."""
        base = _head(repo)
        (repo / "coverage").mkdir()
        (repo / "coverage" / "index.html").write_text("<html></html>\n")
        (repo / "dist").mkdir()
        (repo / "dist" / "bundle.js").write_text("console.log(1);\n")
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.restored == []
        assert set(report.left_out) == {"coverage/index.html", "dist/bundle.js"}
        assert (repo / "coverage" / "index.html").exists()
        assert (repo / "dist" / "bundle.js").exists()

    async def test_untracked_build_detritus_is_not_reported(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "tests" / "__pycache__").mkdir()
        (repo / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.left_out == []
        assert (repo / "tests" / "__pycache__" / "x.pyc").exists()

    async def test_the_allowed_report_is_kept(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "docs" / "cards" / "74-remove-vowels").mkdir(parents=True)
        (repo / REPORT).write_text("# QA Report\n")
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.restored == []
        assert report.left_out == []
        assert (repo / REPORT).read_text() == "# QA Report\n"

    async def test_a_clean_tree_changes_nothing(self, repo: Path) -> None:
        base = _head(repo)
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.restored == []
        assert report.left_out == []
        assert report.committed_out_of_scope == []

    async def test_an_unreadable_tree_fails_closed(self, tmp_path: Path) -> None:
        stand = Stand(path=tmp_path, branch="feat/none")

        with pytest.raises(WorkspaceSetupError):
            await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start="0" * 40)

    async def test_dirty_paths_reports_the_rename_source_too(self, repo: Path) -> None:
        _git(repo, "mv", "app.py", "engine.py")
        stand = Stand(path=repo, branch=BRANCH)

        paths = {entry.path for entry in await dirty_paths(stand)}

        assert paths == {"app.py", "engine.py"}

    def test_porcelain_parser_reads_rename_source_after_the_target(self) -> None:
        output = "R  engine.py\0app.py\0 M tests/test_sample.py\0"

        entries = parse_porcelain_z(output)

        assert [(e.status, e.path) for e in entries] == [
            ("R ", "engine.py"),
            ("R ", "app.py"),
            (" M", "tests/test_sample.py"),
        ]


# ---------------------------------------------------------------------------
# Commits made by the agent during the turn
# ---------------------------------------------------------------------------


class TestAgentCommits:
    async def test_an_agent_commit_outside_scope_is_rewound_off_the_branch(
        self, repo: Path
    ) -> None:
        base = _head(repo)
        (repo / "tests" / "test_sample.py").write_text(PROSE_NOTES)
        _git(repo, "commit", "-qam", "agent: edit the test module")
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.committed_out_of_scope == ["tests/test_sample.py"]
        assert report.reset_to == base
        assert _head(repo) == base
        assert (repo / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST

    async def test_an_agent_commit_of_only_allowed_paths_is_kept(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "docs" / "cards" / "74-remove-vowels").mkdir(parents=True)
        (repo / REPORT).write_text("# QA Report\n")
        _git(repo, "add", "--", REPORT)
        _git(repo, "commit", "-qm", "agent: report")
        stand = Stand(path=repo, branch=BRANCH)

        report = await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

        assert report.committed_out_of_scope == []
        assert report.reset_to is None
        assert _head(repo) != base

    async def test_head_no_longer_descending_from_the_start_fails_closed(self, repo: Path) -> None:
        base = _head(repo)
        _git(repo, "checkout", "-q", "--orphan", "elsewhere")
        _git(repo, "commit", "-q", "--allow-empty", "-m", "unrelated history")
        stand = Stand(path=repo, branch=BRANCH)

        with pytest.raises(WorkspaceSetupError, match="descend"):
            await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=base)

    async def test_an_unknown_starting_commit_fails_closed(self, repo: Path) -> None:
        stand = Stand(path=repo, branch=BRANCH)

        with pytest.raises(WorkspaceSetupError, match="starting commit"):
            await enforce_write_scope(stand, {REPORT}, role="qa", head_at_start=None)

    async def test_head_paths_reads_a_named_revision(self, repo: Path) -> None:
        base = _head(repo)
        (repo / "extra.py").write_text("x = 1\n")
        _git(repo, "add", "--", "extra.py")
        _git(repo, "commit", "-qm", "add extra")
        stand = Stand(path=repo, branch=BRANCH)

        assert "extra.py" in await head_paths(stand)
        assert "extra.py" not in await head_paths(stand, base)


# ---------------------------------------------------------------------------
# Declared new-file paths
# ---------------------------------------------------------------------------


async def _refusal(repo: Path, path: object) -> str | None:
    tracked = TrackedPaths.of(await head_paths(Stand(path=repo, branch=BRANCH)))
    return declared_path_refusal(path, root=repo, tracked=tracked)


class TestDisplaySanitiser:
    """188 review: model text reaches the report and the log only through display_path."""

    def test_characters_outside_the_allowlist_are_replaced(self) -> None:
        shown = display_path("tests/test_`<img src=x>`.py")

        assert "`" not in shown
        assert "<" not in shown
        assert ">" not in shown
        assert shown.startswith("tests/test_")

    def test_a_non_string_is_reported_as_not_a_path_and_never_dumped(self) -> None:
        assert display_path(["tests/test_x.py"]) == "(not a path)"
        assert display_path(None) == "(not a path)"

    def test_a_long_path_is_capped_and_marked(self) -> None:
        shown = display_path("tests/" + "a" * 500 + ".py")

        assert len(shown) <= 200
        assert shown.endswith("...")

    def test_a_non_ascii_letter_is_kept(self) -> None:
        assert display_path("tests/test_café.py") == "tests/test_café.py"


class TestDeclaredPathAllowlist:
    @pytest.mark.parametrize(
        "path", ["tests/test_a(1).py", "tests/test_{x}.py", "tests/test_$x.py"]
    )
    async def test_characters_outside_the_allowlist_are_refused(
        self, repo: Path, path: str
    ) -> None:
        reason = await _refusal(repo, path)

        assert reason is not None
        assert "characters outside" in reason

    async def test_a_non_ascii_test_name_is_allowed(self, repo: Path) -> None:
        assert await _refusal(repo, "tests/test_café.py") is None

    async def test_a_name_too_long_for_the_filesystem_is_refused_not_raised(
        self, repo: Path
    ) -> None:
        reason = await _refusal(repo, "tests/test_" + "a" * 300 + ".py")

        assert reason is not None
        assert "too long" in reason


class TestDeclaredPathRules:
    @pytest.mark.parametrize(
        ("path", "keyword"),
        [
            ("tests/test_*.py", "glob or magic"),
            ("tests/test_?.py", "glob or magic"),
            ("tests/[ab]_test.py", "glob or magic"),
            (":(glob)tests/test_x.py", "glob or magic"),
            ("tests\\test_x.py", "backslash"),
            ("tests/test_x\n.py", "control"),
            ("tests/test_x\x00.py", "control"),
            ("/etc/test_x.py", "absolute"),
            ("../escape/test_x.py", "traversing"),
            ("tests//test_x.py", "traversing"),
            (".github/workflows/test_ci.yml", "dot"),
            (".hidden/test_x.py", "dot"),
            ("tests/conftest.py", "configuration or hook"),
            ("tests/__init__.py", "configuration or hook"),
            ("tests/pytest.ini", "configuration or hook"),
            ("setup.cfg", "configuration or hook"),
            ("tests/setup.py", "configuration or hook"),
            ("__tests__/jest.config.js", "configuration or hook"),
            ("__tests__/vitest.config.ts", "configuration or hook"),
            ("package.json", "configuration or hook"),
            ("hooks/test_pre.py", "configuration or hook"),
            ("tests/test_ci.yml", "configuration or hook"),
            ("tests/test_x.sh", "test source extension"),
            ("tests/helpers.py", "named as a test"),
            ("tests", "directory"),
            ("tests/TEST_SAMPLE.PY", "case collision"),
            ("tests/Test_Sample.py", "case collision"),
            ("tests/test_sample.py", "exists at HEAD"),
            (None, "not a path"),
            (["tests/test_x.py"], "not a path"),
        ],
    )
    async def test_a_refused_declared_path_is_named_with_its_reason(
        self, repo: Path, path: object, keyword: str
    ) -> None:
        reason = await _refusal(repo, path)

        assert reason is not None
        assert keyword in reason

    @pytest.mark.parametrize(
        "path",
        [
            "tests/test_new.py",
            "tests/unit/test_deep.py",
            "src/widget.test.ts",
            "spec/user_spec.rb",
            "internal/x/x_test.go",
            "tests/suite.bats",
            "tests/test_widget.jsx",
        ],
    )
    async def test_a_legitimate_new_test_path_is_allowed(self, repo: Path, path: str) -> None:
        assert await _refusal(repo, path) is None

    async def test_a_symlinked_ancestor_is_refused_before_anything_is_written(
        self, repo: Path
    ) -> None:
        _git(repo, "rm", "-r", "-q", "tests")
        (repo / "src").mkdir()
        (repo / "src" / "app_old.py").write_text("x = 1\n")
        (repo / "tests").symlink_to("src")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "tests becomes a symlink")

        reason = await _refusal(repo, "tests/test_new.py")

        assert reason is not None
        assert "symlink" in reason
        assert not (repo / "src" / "test_new.py").exists()

    async def test_a_symlink_at_the_declared_path_is_refused(self, repo: Path) -> None:
        (repo / "src").mkdir()
        (repo / "src" / "real.py").write_text("x = 1\n")
        (repo / "tests" / "test_link.py").symlink_to(repo / "src" / "real.py")

        reason = await _refusal(repo, "tests/test_link.py")

        assert reason is not None
        assert "symlink" in reason
