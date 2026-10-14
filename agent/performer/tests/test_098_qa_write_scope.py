"""098: a QA turn writes only its report and the new test files it declares.

Issue #98: on card remove_vowels a QA turn wrote its prose notes into
``tests/test_sample.py``, replacing the whole module, and the performer
committed and pushed it. Two write paths made that possible: the
``new_test_files`` loop committed any model-named path with no check against
what already existed, and a free agent turn could edit a tracked file in the
clone that the next commit then carried.

The rules these tests pin:

* A tracked file changed outside the allowlist is restored from HEAD, and the
  verdict fails naming it.
* An untracked path the turn did not declare is never committed, is left on
  disk, and does not fail the verdict. Test runs leave such files behind
  (``.coverage``, ``dist/``), and failing on them would be a false verdict.
* A staged-but-untracked stray is unstaged, not committed, and not failed.
* A declared new-file path that exists at HEAD, or is not test-shaped, is never
  written, and fails the verdict.
* The commit step stages and commits only the path it was given.

These tests drive real git repositories (the guard's whole job is git state),
so the assertions are about what is on disk and in the index, not about mocks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from performer.backends.base import BackendStatus
from performer.config import Settings
from performer.main import handle_status
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage
from performer.workspace import WorkspaceSetupError, commit_file
from performer.write_scope import dirty_paths, enforce_write_scope, parse_porcelain_z

ISSUE = 74
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
# The guard itself (performer.write_scope)
# ---------------------------------------------------------------------------


class TestWriteScopeGuard:
    async def test_a_tracked_file_rewritten_outside_scope_is_restored(self, repo: Path) -> None:
        (repo / "tests" / "test_sample.py").write_text(PROSE_NOTES)
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == ["tests/test_sample.py"]
        assert result.left_out == []
        assert (repo / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert _status(repo) == ""

    async def test_a_deleted_tracked_file_outside_scope_is_restored(self, repo: Path) -> None:
        (repo / "app.py").unlink()
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == ["app.py"]
        assert (repo / "app.py").exists()
        assert _status(repo) == ""

    async def test_an_untracked_stray_is_left_on_disk_not_restored(self, repo: Path) -> None:
        (repo / "notes.txt").write_text(PROSE_NOTES)
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == []
        assert result.left_out == ["notes.txt"]
        assert (repo / "notes.txt").read_text() == PROSE_NOTES
        assert _status(repo) == "?? notes.txt\n"

    async def test_a_staged_new_file_is_unstaged_and_kept_on_disk(self, repo: Path) -> None:
        (repo / "tests" / "test_extra.py").write_text(PROSE_NOTES)
        _git(repo, "add", "--", "tests/test_extra.py")
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == []
        assert result.left_out == ["tests/test_extra.py"]
        assert (repo / "tests" / "test_extra.py").exists()
        assert _status(repo) == "?? tests/test_extra.py\n"

    async def test_untracked_artifact_directories_are_left_alone(self, repo: Path) -> None:
        """Test runs leave coverage and build output behind. None of it is deleted."""
        (repo / "coverage").mkdir()
        (repo / "coverage" / "index.html").write_text("<html></html>\n")
        (repo / "dist").mkdir()
        (repo / "dist" / "bundle.js").write_text("console.log(1);\n")
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == []
        assert set(result.left_out) == {"coverage/index.html", "dist/bundle.js"}
        assert (repo / "coverage" / "index.html").exists()
        assert (repo / "dist" / "bundle.js").exists()

    async def test_the_allowed_report_is_kept(self, repo: Path) -> None:
        (repo / "docs" / "cards" / "74-remove-vowels").mkdir(parents=True)
        (repo / REPORT).write_text("# QA Report\n")
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == []
        assert result.left_out == []
        assert (repo / REPORT).read_text() == "# QA Report\n"

    async def test_untracked_build_detritus_is_not_a_change(self, repo: Path) -> None:
        (repo / "tests" / "__pycache__").mkdir()
        (repo / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == []
        assert result.left_out == []
        assert (repo / "tests" / "__pycache__" / "x.pyc").exists()

    async def test_a_clean_tree_changes_nothing(self, repo: Path) -> None:
        stand = Stand(path=repo, branch=BRANCH)

        result = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert result.restored == []
        assert result.left_out == []

    async def test_an_unreadable_tree_fails_closed(self, tmp_path: Path) -> None:
        """Not a repository: the check cannot tell what changed, so it must refuse."""
        stand = Stand(path=tmp_path, branch="feat/none")

        with pytest.raises(WorkspaceSetupError):
            await enforce_write_scope(stand, {REPORT}, role="qa")

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
# The commit step stages and commits only what it is given
# ---------------------------------------------------------------------------


class TestCommitStaysOnItsPath:
    async def test_commit_file_never_commits_another_staged_path(self, repo: Path) -> None:
        """The commit step alone: a stray staged elsewhere stays staged and out of HEAD.

        The push is stubbed so this isolates the commit. The push step separately
        refuses to publish while unrelated paths are staged, which is a second guard.
        """
        (repo / "notes.txt").write_text(PROSE_NOTES)
        _git(repo, "add", "--", "notes.txt")
        stand = Stand(path=repo, branch=BRANCH)

        with patch("performer.workspace._push_head_without_clobbering", new=AsyncMock()):
            await commit_file(stand, REPORT, "# QA Report\n", "chore: add QA report")

        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == [REPORT]
        assert "notes.txt" in _git(repo, "diff", "--cached", "--name-only").split()

    async def test_a_guarded_turn_publishes_only_its_report(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """The real order: the guard unstages strays first, then the commit and push run."""
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        _git(repo, "remote", "add", "origin", str(origin))
        _git(repo, "push", "-q", "origin", BRANCH)
        (repo / "notes.txt").write_text(PROSE_NOTES)
        _git(repo, "add", "--", "notes.txt")
        stand = Stand(path=repo, branch=BRANCH)

        await enforce_write_scope(stand, {REPORT}, role="qa")
        await commit_file(stand, REPORT, "# QA Report\n", "chore: add QA report")

        committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == [REPORT]
        assert (repo / "notes.txt").read_text() == PROSE_NOTES
        assert _git(repo, "ls-remote", "origin", BRANCH).strip() != ""


# ---------------------------------------------------------------------------
# The QA verdict (performer.qa_postprocess.finalize_qa via handle_status)
# ---------------------------------------------------------------------------


def _qa_json(**over) -> str:
    base = {
        "passed": True,
        "criteria_checked": 1,
        "criteria_passed": 1,
        "failures": [],
        "executed_checks": [{"command": "pytest -q", "exit_code": 0, "output": "1 passed"}],
        "verification_steps": ["Run: pytest -q"],
    }
    base.update(over)
    return json.dumps(base)


class TestQAWriteScopeVerdict:
    def _make_perf(self, repo: Path) -> Performance:
        stand = Stand(path=repo, branch=BRANCH)
        score = Score(
            title="remove_vowels",
            repo_url="https://github.com/acme/conductor-e2e-sample",
            branch=BRANCH,
            issue_number=ISSUE,
        )
        return Performance(
            session_id="sid",
            stand=stand,
            score=score,
            backend=MagicMock(),
            role="qa",
        )

    async def _handle(self, repo: Path, output: str, commit: AsyncMock) -> object:
        perf = self._make_perf(repo)
        perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
        with (
            patch("performer.main.commit_file", new=commit),
            patch("performer.main.post_issue_comment", new=AsyncMock()),
        ):
            return await handle_status(
                PerformerMessage(action="status", session_id="sid", payload={}),
                perf,
                Settings(AGENT_BACKEND="opencode"),
            )

    async def test_incident_a_qa_turn_cannot_replace_an_existing_test_module(
        self, repo: Path
    ) -> None:
        """Reproduction of #98: the QA JSON names the existing test module and
        carries prose as its content. The module must keep its content, nothing
        may be committed under that path, and the verdict must name it."""
        committed: list[str] = []

        async def fake_commit(stand, path, content, message, **_kw):
            committed.append(path)

        resp = await self._handle(
            repo,
            _qa_json(new_test_files=[{"path": "tests/test_sample.py", "content": PROSE_NOTES}]),
            AsyncMock(side_effect=fake_commit),
        )

        assert (repo / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert "tests/test_sample.py" not in committed
        assert resp.status == "qa_failed"
        scope = [f for f in resp.failures if f.get("type") == "write_scope_violation"]
        assert scope and "tests/test_sample.py" in scope[0]["actual"]

    async def test_a_turn_that_edits_a_tracked_file_is_restored_and_failed(
        self, repo: Path
    ) -> None:
        (repo / "tests" / "test_sample.py").write_text(PROSE_NOTES)
        committed: list[str] = []

        async def fake_commit(stand, path, content, message, **_kw):
            committed.append(path)

        resp = await self._handle(repo, _qa_json(), AsyncMock(side_effect=fake_commit))

        assert (repo / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert committed == [REPORT]
        assert resp.status == "qa_failed"
        assert any(
            f.get("type") == "write_scope_violation" and "tests/test_sample.py" in f["actual"]
            for f in resp.failures
        )

    async def test_a_new_source_file_declared_as_a_test_is_refused(self, repo: Path) -> None:
        """A brand-new file is not a clobber, but a QA turn creating source is still
        outside its scope. Only test-shaped paths may be declared as new files."""
        committed: list[str] = []

        async def fake_commit(stand, path, content, message, **_kw):
            committed.append(path)

        resp = await self._handle(
            repo,
            _qa_json(new_test_files=[{"path": "src/helper.py", "content": PROSE_NOTES}]),
            AsyncMock(side_effect=fake_commit),
        )

        assert committed == [REPORT]
        assert not (repo / "src" / "helper.py").exists()
        assert resp.status == "qa_failed"
        assert any(
            f.get("type") == "write_scope_violation" and "src/helper.py" in f["actual"]
            for f in resp.failures
        )

    async def test_a_legitimate_new_test_file_and_report_still_pass(self, repo: Path) -> None:
        """A QA turn that creates a new acceptance test and its report is the
        normal case; neither may be discarded or failed by the guard."""
        new_test = "tests/test_acceptance_remove_vowels.py"
        new_body = "def test_acceptance():\n    assert True\n"
        written: list[str] = []

        async def fake_commit(stand, path, content, message, **_kw):
            target = stand.path / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            written.append(path)

        resp = await self._handle(
            repo,
            _qa_json(new_test_files=[{"path": new_test, "content": new_body}]),
            AsyncMock(side_effect=fake_commit),
        )

        assert written == [new_test, REPORT]
        assert (repo / new_test).read_text() == new_body
        assert resp.status == "qa_passed"
        assert not any(f.get("type") == "write_scope_violation" for f in resp.failures)

    async def test_untracked_test_artifacts_do_not_fail_the_verdict(self, repo: Path) -> None:
        """A QA run that executes the project's tests leaves coverage output and a
        .coverage file behind. That is not a write to the repository's content: it
        must not fail the verdict, and it must not be committed."""
        (repo / "coverage").mkdir()
        (repo / "coverage" / "index.html").write_text("<html></html>\n")
        (repo / ".coverage").write_bytes(b"\x00")
        committed: list[str] = []

        async def fake_commit(stand, path, content, message, **_kw):
            committed.append(path)

        resp = await self._handle(repo, _qa_json(), AsyncMock(side_effect=fake_commit))

        assert committed == [REPORT]
        assert (repo / "coverage" / "index.html").exists()
        assert (repo / ".coverage").exists()
        assert resp.status == "qa_passed"
        assert not any(f.get("type") == "write_scope_violation" for f in resp.failures)
