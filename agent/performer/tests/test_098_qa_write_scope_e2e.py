"""098: end to end, through the real ``commit_file`` and a real bare origin.

Nothing here mocks ``commit_file``. Each case runs the QA verdict path
(``handle_status``), which writes, commits and pushes through the performer's
own git code, and then inspects the origin. The only stub is the issue comment,
which is a network call unrelated to what is published.
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
from performer.workspace import commit_file

BRANCH = "feat/remove-vowels"
REPORT = "docs/cards/74-remove-vowels/qa.md"
ORIGINAL_TEST = "def test_remove_vowels():\n    assert remove_vowels('abc') == 'bc'\n"
PROSE_NOTES = "QA verification notes: checked the vowel rule on three inputs.\n"
_GIT = shutil.which("git") or "git"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [_GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _head(work: Path) -> str:
    return _git(work, "rev-parse", "HEAD").strip()


def _published_files(origin: Path, base: str) -> list[str]:
    """Every file the branch on origin changes relative to the base commit."""
    return sorted(
        _git(origin, "diff", "--name-only", "--no-renames", base, BRANCH).split(),
    )


def _published_text(origin: Path, path: str) -> str:
    return _git(origin, "show", f"{BRANCH}:{path}")


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path]:
    """A working clone and a bare origin, with the base commit already pushed."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    work.mkdir()
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(work, "init", "-q", "-b", BRANCH)
    _git(work, "config", "user.name", "fixture")
    _git(work, "config", "user.email", "fixture@example.invalid")
    (work / "tests").mkdir()
    (work / "tests" / "test_sample.py").write_text(ORIGINAL_TEST)
    (work / "app.py").write_text("def remove_vowels(s):\n    return s\n")
    (work / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", BRANCH)
    return work, origin


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


def _perf(work: Path, base: str) -> Performance:
    score = Score(
        title="remove_vowels",
        repo_url="https://github.com/acme/conductor-e2e-sample",
        branch=BRANCH,
        issue_number=74,
    )
    perf = Performance(
        session_id="sid",
        stand=Stand(path=work, branch=BRANCH),
        score=score,
        backend=MagicMock(),
        role="qa",
    )
    perf.head_at_start = base
    return perf


async def _verdict(perf: Performance, output: str, settings: Settings | None = None):
    perf.backend.get_status.return_value = BackendStatus(state="done", output=output)
    with patch("performer.main.post_issue_comment", new=AsyncMock()):
        return await handle_status(
            PerformerMessage(action="status", session_id="sid", payload={}),
            perf,
            settings or Settings(AGENT_BACKEND="opencode"),
        )


def _scope_failures(resp) -> list[dict]:
    return [f for f in resp.failures if f.get("type") == "write_scope_violation"]


class TestDeclaredPathsOnTheRealCommitPath:
    async def test_a_glob_declared_path_publishes_nothing_it_matches(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)
        (work / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        (work / "tests" / "test_b.py").write_text("def test_b():\n    pass\n")

        resp = await _verdict(
            _perf(work, base),
            _qa_json(new_test_files=[{"path": "tests/test_*.py", "content": PROSE_NOTES}]),
        )

        assert _published_files(origin, base) == [REPORT]
        assert not (work / "tests" / "test_*.py").exists()
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`tests/test_*.py`" in _published_text(origin, REPORT)

    async def test_a_conftest_declared_path_is_refused_and_not_published(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(new_test_files=[{"path": "tests/conftest.py", "content": PROSE_NOTES}]),
        )

        assert _published_files(origin, base) == [REPORT]
        assert not (work / "tests" / "conftest.py").exists()
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`tests/conftest.py`" in _published_text(origin, REPORT)

    async def test_a_ci_workflow_declared_path_is_refused(self, clone: tuple[Path, Path]) -> None:
        work, origin = clone
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(
                new_test_files=[
                    {"path": ".github/workflows/test_ci.yml", "content": PROSE_NOTES},
                ],
            ),
        )

        assert _published_files(origin, base) == [REPORT]
        assert not (work / ".github").exists()
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`.github/workflows/test_ci.yml`" in _published_text(origin, REPORT)

    async def test_a_traversing_declared_path_writes_nothing_outside_the_clone(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(new_test_files=[{"path": "../escape/test_x.py", "content": PROSE_NOTES}]),
        )

        assert _published_files(origin, base) == [REPORT]
        assert not (work.parent / "escape").exists()
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`../escape/test_x.py`" in _published_text(origin, REPORT)

    async def test_a_case_variant_of_a_tracked_test_is_refused(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(new_test_files=[{"path": "tests/Test_Sample.py", "content": PROSE_NOTES}]),
        )

        assert (work / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert _published_files(origin, base) == [REPORT]
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`tests/Test_Sample.py`" in _published_text(origin, REPORT)

    async def test_a_directory_declared_path_is_refused(self, clone: tuple[Path, Path]) -> None:
        work, origin = clone
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(new_test_files=[{"path": "tests", "content": PROSE_NOTES}]),
        )

        assert _published_files(origin, base) == [REPORT]
        assert (work / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`tests`" in _published_text(origin, REPORT)

    async def test_a_symlinked_tests_directory_is_refused_before_any_write(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        _git(work, "rm", "-r", "-q", "tests")
        (work / "src").mkdir()
        (work / "src" / "app_old.py").write_text("x = 1\n")
        (work / "tests").symlink_to("src")
        _git(work, "add", "-A")
        _git(work, "commit", "-qm", "tests becomes a symlink")
        _git(work, "push", "-q", "origin", BRANCH)
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(new_test_files=[{"path": "tests/test_new.py", "content": PROSE_NOTES}]),
        )

        assert not (work / "src" / "test_new.py").exists()
        assert _published_files(origin, base) == [REPORT]
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert "`tests/test_new.py`" in _published_text(origin, REPORT)


class TestRefusedDeclarationsAreNotes:
    async def test_a_passing_turn_keeps_its_verdict_when_declarations_are_refused(
        self, clone: tuple[Path, Path]
    ) -> None:
        """A QA turn that passes on its merits declares a helper module and an existing
        test module. Both are refused: neither is written or changed, both are listed in
        the report with their reasons, and the verdict is the QA result, a pass."""
        work, origin = clone
        base = _head(work)

        resp = await _verdict(
            _perf(work, base),
            _qa_json(
                new_test_files=[
                    {"path": "tests/helpers.py", "content": PROSE_NOTES},
                    {"path": "tests/test_sample.py", "content": PROSE_NOTES},
                ],
            ),
        )

        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        assert not (work / "tests" / "helpers.py").exists()
        assert (work / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert set(_published_files(origin, base)) == {REPORT}
        assert _published_text(origin, "tests/test_sample.py") == ORIGINAL_TEST
        report = _published_text(origin, REPORT)
        assert "`tests/helpers.py`" in report
        assert "not named as a test" in report
        assert "`tests/test_sample.py`" in report
        assert "exists at HEAD" in report


class TestAgentCommitsOnTheRealPath:
    async def test_a_self_commit_to_an_out_of_scope_file_is_not_published(
        self, clone: tuple[Path, Path]
    ) -> None:
        """The incident path: the agent commits a test module itself. HEAD moves,
        the tree is clean, and a plain working-tree check would miss it."""
        work, origin = clone
        base = _head(work)
        (work / "tests" / "test_sample.py").write_text(PROSE_NOTES)
        _git(work, "commit", "-qam", "agent: rewrite the test module")

        resp = await _verdict(_perf(work, base), _qa_json())

        assert _published_files(origin, base) == [REPORT]
        assert _published_text(origin, "tests/test_sample.py") == ORIGINAL_TEST
        assert (work / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert resp.status == "qa_failed"
        assert any("tests/test_sample.py" in f["actual"] for f in _scope_failures(resp))

    async def test_a_turn_without_a_known_start_commit_publishes_nothing(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)
        (work / "tests" / "test_sample.py").write_text(PROSE_NOTES)
        perf = _perf(work, base)
        perf.head_at_start = None

        resp = await _verdict(perf, _qa_json())

        assert _git(origin, "rev-parse", BRANCH).strip() == base
        assert _published_text(origin, "tests/test_sample.py") == ORIGINAL_TEST
        assert resp.status == "error"
        assert "starting commit" in (resp.reason or "")


class TestNotesAndLegitimateOutput:
    async def test_a_legitimate_new_test_and_report_are_published(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)
        new_test = "tests/test_acceptance_remove_vowels.py"

        resp = await _verdict(
            _perf(work, base),
            _qa_json(
                new_test_files=[
                    {"path": new_test, "content": "def test_acceptance():\n    assert True\n"},
                ],
            ),
        )

        assert set(_published_files(origin, base)) == {new_test, REPORT}
        assert resp.status == "qa_passed"

    async def test_untracked_test_artifacts_do_not_fail_the_verdict(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)
        (work / "coverage").mkdir()
        (work / "coverage" / "index.html").write_text("<html></html>\n")
        (work / ".coverage").write_bytes(b"\x00")

        resp = await _verdict(_perf(work, base), _qa_json())

        assert _published_files(origin, base) == [REPORT]
        assert (work / "coverage" / "index.html").exists()
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []

    async def test_a_tracked_lockfile_change_is_restored_and_noted_not_failed(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)
        (work / "package-lock.json").write_text('{"lockfileVersion": 9}\n')

        resp = await _verdict(_perf(work, base), _qa_json())

        assert (work / "package-lock.json").read_text() == '{"lockfileVersion": 3}\n'
        assert _published_files(origin, base) == [REPORT]
        assert resp.status == "qa_passed"
        assert _scope_failures(resp) == []
        report_text = _published_text(origin, REPORT)
        assert "package-lock.json" in report_text
        assert "restored" in report_text.lower()

    async def test_unparseable_output_still_reports_what_was_restored(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        base = _head(work)
        (work / "tests" / "test_sample.py").write_text(PROSE_NOTES)

        resp = await _verdict(
            _perf(work, base),
            "I could not produce JSON for this turn.",
            Settings(AGENT_BACKEND="opencode", BACKEND_PARSE_RETRIES=0),
        )

        assert (work / "tests" / "test_sample.py").read_text() == ORIGINAL_TEST
        assert _git(origin, "rev-parse", BRANCH).strip() == base
        assert resp.status == "error"
        assert "tests/test_sample.py" in (resp.reason or "")


class TestCommitStaysOnItsPath:
    async def test_a_glob_path_stages_only_that_literal_file(
        self, clone: tuple[Path, Path]
    ) -> None:
        work, origin = clone
        (work / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        (work / "tests" / "test_b.py").write_text("def test_b():\n    pass\n")
        stand = Stand(path=work, branch=BRANCH)

        await commit_file(stand, "tests/test_*.py", PROSE_NOTES, "test: literal name")

        assert _git(work, "show", "--name-only", "--format=", "HEAD").split() == [
            "tests/test_*.py",
        ]
        assert (work / "tests" / "test_a.py").exists()
