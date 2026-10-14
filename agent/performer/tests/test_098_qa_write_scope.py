"""098: a QA turn writes only its report and the new test files it declares.

Issue #98: on card remove_vowels a QA turn wrote its prose notes into
``tests/test_sample.py``, replacing the whole module, and the performer
committed and pushed it. Two write paths made that possible: the
``new_test_files`` loop committed any model-named path with no check against
what already existed, and a free agent turn could edit a tracked file in the
clone that the next commit then carried.

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
from performer.workspace import WorkspaceSetupError
from performer.write_scope import dirty_paths, enforce_write_scope, parse_porcelain_z

ISSUE = 74
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
    _git(tmp_path, "init", "-q", "-b", "feat/remove-vowels")
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
        stand = Stand(path=repo, branch="feat/remove-vowels")

        discarded = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert discarded == ["tests/test_sample.py"]
        assert (repo / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert _status(repo) == ""

    async def test_a_stray_new_file_outside_scope_is_removed(self, repo: Path) -> None:
        (repo / "notes.txt").write_text(PROSE_NOTES)
        stand = Stand(path=repo, branch="feat/remove-vowels")

        discarded = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert discarded == ["notes.txt"]
        assert not (repo / "notes.txt").exists()
        assert _status(repo) == ""

    async def test_a_new_file_staged_into_the_index_is_unstaged_and_removed(
        self, repo: Path
    ) -> None:
        (repo / "tests" / "test_extra.py").write_text(PROSE_NOTES)
        _git(repo, "add", "--", "tests/test_extra.py")
        stand = Stand(path=repo, branch="feat/remove-vowels")

        discarded = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert discarded == ["tests/test_extra.py"]
        assert not (repo / "tests" / "test_extra.py").exists()
        assert _status(repo) == ""

    async def test_a_deleted_tracked_file_outside_scope_is_restored(self, repo: Path) -> None:
        (repo / "app.py").unlink()
        stand = Stand(path=repo, branch="feat/remove-vowels")

        discarded = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert discarded == ["app.py"]
        assert (repo / "app.py").exists()
        assert _status(repo) == ""

    async def test_the_allowed_report_is_kept(self, repo: Path) -> None:
        (repo / "docs" / "cards" / "74-remove-vowels").mkdir(parents=True)
        (repo / REPORT).write_text("# QA Report\n")
        stand = Stand(path=repo, branch="feat/remove-vowels")

        discarded = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert discarded == []
        assert (repo / REPORT).read_text() == "# QA Report\n"

    async def test_untracked_build_detritus_is_not_a_change(self, repo: Path) -> None:
        (repo / "tests" / "__pycache__").mkdir()
        (repo / "tests" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        stand = Stand(path=repo, branch="feat/remove-vowels")

        discarded = await enforce_write_scope(stand, {REPORT}, role="qa")

        assert discarded == []
        assert (repo / "tests" / "__pycache__" / "x.pyc").exists()

    async def test_a_clean_tree_discards_nothing(self, repo: Path) -> None:
        stand = Stand(path=repo, branch="feat/remove-vowels")

        assert await enforce_write_scope(stand, {REPORT}, role="qa") == []

    async def test_an_unreadable_tree_fails_closed(self, tmp_path: Path) -> None:
        """Not a repository: the check cannot tell what changed, so it must refuse."""
        stand = Stand(path=tmp_path, branch="feat/none")

        with pytest.raises(WorkspaceSetupError):
            await enforce_write_scope(stand, {REPORT}, role="qa")

    async def test_dirty_paths_reports_the_rename_source_too(self, repo: Path) -> None:
        _git(repo, "mv", "app.py", "engine.py")
        stand = Stand(path=repo, branch="feat/remove-vowels")

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
        stand = Stand(path=repo, branch="feat/remove-vowels")
        score = Score(
            title="remove_vowels",
            repo_url="https://github.com/acme/conductor-e2e-sample",
            branch="feat/remove-vowels",
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

    async def test_a_turn_that_edits_a_tracked_file_is_discarded_and_failed(
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
