"""409: operator overrides were dead fields and the stack vocabulary was thin.

Covers: the local-test-gate override reaching detect_test_command /
detect_lint_command, Score declaring test_command, per-root monorepo
commands, anchored test-path patterns with per-symphony extensions,
failure markers for go/cargo/dotnet/vitest, the 089 gate treating exit 127
as an environment block, resume base-branch candidates, and
test_conventions rendering the real command.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from performer.workflows.implementer.baseline import detect_lint_command, detect_test_command
from performer.workflows.implementer.cycle import is_test_path
from performer.test_results import _match_env_signature, _reported_failure_count


class TestOverridePrecedence:
    def test_local_test_gate_command_wins(self, tmp_path: Path) -> None:
        score = SimpleNamespace(local_test_gate={"command": "go test ./..."})
        command, _, detected_from = detect_test_command(score, tmp_path)
        assert command == "go test ./..."
        assert detected_from == "score.local_test_gate.command"

    def test_gate_command_beats_detection(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        score = SimpleNamespace(local_test_gate={"command": "make check"})
        command, _, _ = detect_test_command(score, tmp_path)
        assert command == "make check"

    def test_test_command_attribute_still_honoured(self, tmp_path: Path) -> None:
        score = SimpleNamespace(local_test_gate=None, test_command="tox")
        command, _, detected_from = detect_test_command(score, tmp_path)
        assert command == "tox"
        assert detected_from == "score.test_command"

    def test_lint_command_override(self, tmp_path: Path) -> None:
        score = SimpleNamespace(local_test_gate={"lint_command": "golangci-lint run"})
        assert detect_lint_command(score, tmp_path) == "golangci-lint run"

    def test_score_declares_test_command(self) -> None:
        """extra="ignore" once silently dropped test_command from the payload:
        the field must survive construction."""
        from performer.models import Score

        score = Score(
            title="t", repo_url="https://github.com/o/r", branch="main",
            test_command="tox",
        )
        assert score.test_command == "tox"


class TestMonorepoOverride:
    def test_per_root_command_is_composed_at_the_root(self, tmp_path: Path) -> None:
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "go.mod").write_text("module example.com/api\n")
        score = SimpleNamespace(
            local_test_gate={"roots": ["api"]},
        )
        command, _, detected_from = detect_test_command(score, tmp_path)
        assert command == "cd api && go test ./..."
        assert detected_from == "sub-root: api"

    def test_top_level_command_beats_sub_root(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "Cargo.toml").write_text("[package]\nname = \"a\"\n")
        score = SimpleNamespace(local_test_gate={"roots": ["api"]})
        command, _, _ = detect_test_command(score, tmp_path)
        assert command == "go test ./..."

    def test_root_with_space_is_shell_quoted(self, tmp_path: Path) -> None:
        (tmp_path / "my api").mkdir()
        (tmp_path / "my api" / "go.mod").write_text("module example.com/api\n")
        score = SimpleNamespace(local_test_gate={"roots": ["my api"]})
        command, _, detected_from = detect_test_command(score, tmp_path)
        assert command == "cd 'my api' && go test ./..."
        assert detected_from == "sub-root: my api"

    def test_lint_command_composed_at_sub_root(self, tmp_path: Path) -> None:
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "go.mod").write_text("module example.com/api\n")
        score = SimpleNamespace(local_test_gate={"roots": ["api"]})
        with patch("coordinare_ci_detection._verify_tool", return_value=True):
            assert detect_lint_command(score, tmp_path) == "cd api && go vet ./..."


class TestAnchoredPatterns:
    @pytest.mark.parametrize(
        "path",
        [
            "src/Button.spec.tsx",
            "src/Button.test.tsx",
            "src/app.spec.ts",
            "src/com/ExampleTest.java",
            "src/test/java/com/ExampleTest.java",
            "src/ExampleTests.cs",
            "tests/integration.rs",
            "src/test_edge_cases.py",
            "src/user_test.go",
            "spec/models/user_spec.rb",
        ],
    )
    def test_test_files_recognised(self, path: str) -> None:
        assert is_test_path(path)

    @pytest.mark.parametrize(
        "path",
        [
            "greatest_common.py",
            "latest_run.go",
            "src/greatest_common.py",
            "src/latest_run.go",
            "src/main.py",
            "attest_utils.go",
        ],
    )
    def test_non_test_files_not_recognised(self, path: str) -> None:
        assert not is_test_path(path)

    def test_extra_patterns_extend(self) -> None:
        assert is_test_path("src/custom_thing.feature", extra_patterns=[r"\.feature$"])

    def test_extra_patterns_empty_keeps_defaults(self) -> None:
        assert is_test_path("src/test_main.py", extra_patterns=[])


class TestFailureMarkers:
    @pytest.mark.parametrize(
        "output",
        [
            "--- FAIL: TestThing (0.00s)\nconnection refused",
            "test result: FAILED. 0 passed; 1 failed; 0 ignored\nconnection refused",
            "Failed!  - Failed:     1, - Passed:     6 (xUnit)\nconnection refused",
            " 1 failed | 6 passed\nconnection refused",
        ],
    )
    def test_new_runners_are_not_env(self, output: str) -> None:
        """A real assertion failure mentioning an environment phrase must be a
        code defect, not an env hold (409)."""
        assert _match_env_signature(output) is None

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("test result: FAILED. 0 passed; 1 failed; 0 ignored", 1),
            ("Failed!  - Failed:     3, - Passed:     6", 3),
            (" 2 failed | 6 passed", 2),
        ],
    )
    def test_counts_parse(self, output: str, expected: int) -> None:
        assert _reported_failure_count(output) == expected

    def test_count_not_confused_by_loose_colon(self) -> None:
        """Only an xUnit-style '- Failed: n' summary counts; ordinary output
        containing the word failed before a number stays uncounted."""
        assert _reported_failure_count("request failed: 20 retries left") is None

    def test_invalid_extra_pattern_is_skipped(self) -> None:
        """A malformed per-symphony pattern must not abort the turn: the
        built-ins still apply and the invalid extension is ignored."""
        assert is_test_path("src/test_main.py", extra_patterns=["["]) is True
        assert is_test_path("src/main.py", extra_patterns=["["]) is False


class TestGateExit127:
    @pytest.mark.asyncio
    async def test_exit_127_is_environment(self) -> None:
        """pytest: command not found is the shell saying the runner is not
        installed. The 089 gate must hold the card as environment, exactly
        like the 167 lane, instead of burning max_fix_attempts on a defect
        report the implementer cannot act on."""
        from performer.main import _run_test_check

        detection = SimpleNamespace(
            test_command="pytest", stack="python", lint_command=None,
            per_root=None, detected_from="",
        )
        run_result = SimpleNamespace(
            success=False, exit_code=127, stdout="", stderr="bash: pytest: command not found",
            command="pytest", duration_seconds=0.1, timed_out=False,
        )
        with (
            patch("coordinare_ci_detection.detect", return_value=detection),
            patch("performer.main.run_command", new=AsyncMock(return_value=run_result)),
        ):
            result = await _run_test_check(Path("/tmp/x"), timeout_seconds=600)
        assert result.env_blocked is True
        assert result.passed is False
        assert result.exit_code == 127


class TestResumeBaseCandidates:
    @pytest.mark.asyncio
    async def test_master_and_develop_are_candidates(self) -> None:
        from performer.workflows.implementer import commits
        from performer.workflows.implementer import ImplementerWorkflow

        seen: list[list[str]] = []

        async def fake_entries(workspace, refs):
            seen.append(refs)
            return []

        with patch.object(commits, "branch_commit_entries", fake_entries):
            ctx = SimpleNamespace(score=SimpleNamespace(base_branch=""), prior_paths=frozenset(), issue_number=0)
            plans: list = []
            result, index = await ImplementerWorkflow._resume(ctx, plans, Path("/tmp/x"))
        candidates = seen[0]
        assert any("master" in c for c in candidates)
        assert any("develop" in c for c in candidates)
        assert candidates[0] == "origin/main"

    @pytest.mark.asyncio
    async def test_explicit_base_is_first(self) -> None:
        from performer.workflows.implementer import commits
        from performer.workflows.implementer import ImplementerWorkflow

        seen: list[list[str]] = []

        async def fake_entries(workspace, refs):
            seen.append(refs)
            return []

        with patch.object(commits, "branch_commit_entries", fake_entries):
            ctx = SimpleNamespace(score=SimpleNamespace(base_branch="trunk"), prior_paths=frozenset(), issue_number=0)
            await ImplementerWorkflow._resume(ctx, [], Path("/tmp/x"))
        candidates = seen[0]
        assert candidates[0] == "origin/trunk"
        assert "trunk" in candidates


class TestTestConventionsRendered:
    def test_brief_renders_the_real_command(self) -> None:
        """test_conventions once rendered " conventions" -- runner_kind has
        been "" since 365. The detected command is the real content."""
        from performer.workflows.implementer.driver import _build_brief

        ctx = SimpleNamespace(
            runner_kind="",
            test_command="go test ./...",
            score=SimpleNamespace(title="t", description="d"),
            investigation_note=None,
        )
        milestone = SimpleNamespace(goal="g", done_when="done", scope="", lane=None, index=0)
        brief = _build_brief(ctx, milestone, kind="tests", persona_kind="TESTS")
        assert "go test ./..." in brief.persona


class TestPatternsReachTheGuard:
    def test_scope_violations_honours_extra_patterns(self) -> None:
        from performer.workflows.implementer.cycle import scope_violations

        violations = scope_violations(
            "tests",
            {"src/custom.feature": "modified"},
            runner_kind="",
            extra_test_patterns=[r"\.feature$"],
        )
        assert not any(v["kind"] == "reverted_source" for v in violations)

    def test_changed_test_files_honours_extra_patterns(self) -> None:
        from performer.workflows.implementer.cycle import changed_test_files

        files = changed_test_files(
            {"src/custom.feature": "modified", "src/main.py": "modified"},
            runner_kind="",
            extra_test_patterns=[r"\.feature$"],
        )
        assert files == ["src/custom.feature"]
