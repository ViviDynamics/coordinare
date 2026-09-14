"""408: command output is truncated three times on the way to a reader.

A per-call output budget and capture mode on ``Toolkit.run_command`` (carried
through to ``workspace.run_command``), stderr that survives the excerpt slice,
and a ``git ls-files`` read large enough for the real tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.workflows.models import ExecutedCheck
from performer.workflows.project_shape import repo_tree
from performer.workflows.toolkit import Toolkit


def _ci_result(stdout: str, stderr: str = "", exit_code: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        success=exit_code == 0, exit_code=exit_code, stdout=stdout, stderr=stderr,
        command="cmd", duration_seconds=0.0, timed_out=False,
    )


# ---------------------------------------------------------------------------
# workspace.run_command: per-call output budget and head+tail capture
# ---------------------------------------------------------------------------


class TestWorkspaceRunCommandBudget:
    @pytest.mark.asyncio
    async def test_max_output_widens_the_cap(self, tmp_path: Path) -> None:
        """The default 2000-char cap is a default, not a law: a caller who
        asks for more gets more."""
        from performer.workspace import run_command

        result = await run_command("python3 -c \"print('x' * 5000)\"", tmp_path, max_output=10_000)
        assert "x" * 5000 in result.stdout
        assert len(result.stdout) > 2000

    @pytest.mark.asyncio
    async def test_default_cap_is_unchanged(self, tmp_path: Path) -> None:
        from performer.workspace import run_command

        result = await run_command("python3 -c \"print('x' * 5000)\"", tmp_path)
        assert len(result.stdout) <= 2000

    @pytest.mark.asyncio
    async def test_truncate_both_keeps_head_and_tail(self, tmp_path: Path) -> None:
        """A test run shows the runner's configuration at the start and its
        failure summary at the END. A combined mode keeps a slice of each."""
        from performer.workspace import run_command

        script = "python3 -c \"print('ZZSTART' + 'M' * 5000 + 'ZZEND')\""
        result = await run_command(script, tmp_path, truncate="both")
        assert len(result.stdout) <= 2000
        assert "ZZSTART" in result.stdout
        assert "ZZEND" in result.stdout

    @pytest.mark.asyncio
    async def test_degenerate_budgets_stay_bounded(self, tmp_path: Path) -> None:
        """A budget below the truncation marker's own length must degrade to a
        bounded slice, never grow the text (text[-0:] is the whole text)."""
        from performer.workspace import run_command

        script = "python3 -c \"print('x' * 5000)\""
        result = await run_command(script, tmp_path, truncate="both", max_output=1)
        assert len(result.stdout) <= 1

        result = await run_command(script, tmp_path, truncate="tail", max_output=0)
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_timeout_failure_path_honours_the_budget(self, tmp_path: Path) -> None:
        """The timeout and exception paths share the per-call budget with the
        success path: no call returns more output than its budget."""
        from performer.workspace import run_command

        result = await run_command("sleep 10", tmp_path, timeout=1, max_output=0)
        assert result.timed_out is True
        assert result.stderr == ""


# ---------------------------------------------------------------------------
# ExecutedCheck.from_result: excerpt budget
# ---------------------------------------------------------------------------


class TestExecutedCheckBudget:
    def test_from_result_honours_output_budget(self) -> None:
        output = "y" * 5000
        check = ExecutedCheck.from_result(command="c", exit_code=0, output=output, output_budget=6000)
        assert check.output_excerpt == output

    def test_from_result_default_is_unchanged(self) -> None:
        check = ExecutedCheck.from_result(command="c", exit_code=0, output="y" * 5000)
        assert len(check.output_excerpt) == 2000

    def test_negative_budget_is_not_a_negative_slice(self) -> None:
        """output[: -1] would return nearly the whole output; a non-positive
        budget yields an empty excerpt."""
        check = ExecutedCheck.from_result(command="c", exit_code=0, output="y" * 5000, output_budget=-1)
        assert check.output_excerpt == ""


# ---------------------------------------------------------------------------
# Toolkit.run_command: budget and capture reach the runner and the excerpt
# ---------------------------------------------------------------------------


class TestToolkitRunCommandBudget:
    @pytest.mark.asyncio
    async def test_budget_and_capture_reach_supporting_runner(self) -> None:
        runner = AsyncMock(return_value=(0, "ok"))
        toolkit = Toolkit(metrics=SimpleNamespace(commands_run=0), command_runner=runner)
        check = await toolkit.run_command("make test", output_budget=60_000, capture="both")
        assert "output_budget" in runner.call_args.kwargs
        assert runner.call_args.kwargs["output_budget"] == 60_000
        assert runner.call_args.kwargs["capture"] == "both"
        assert check.output_excerpt == "ok"

    @pytest.mark.asyncio
    async def test_budget_reaches_excerpt_with_legacy_runner(self) -> None:
        """A runner that predates the budget (test fakes, other harnesses) is
        still honoured: the Toolkit applies the budget to the excerpt itself
        and never passes the runner keywords it cannot accept."""
        output = "y" * 5000

        async def legacy_runner(cmd, cwd, timeout_s):
            return 0, output

        toolkit = Toolkit(metrics=SimpleNamespace(commands_run=0), command_runner=legacy_runner)
        check = await toolkit.run_command("make test", output_budget=6000)
        assert check.output_excerpt == output

    @pytest.mark.asyncio
    async def test_default_excerpt_is_unchanged(self) -> None:
        toolkit = Toolkit(
            metrics=SimpleNamespace(commands_run=0),
            command_runner=AsyncMock(return_value=(0, "y" * 5000)),
        )
        check = await toolkit.run_command("make test")
        assert len(check.output_excerpt) == 2000

    @pytest.mark.asyncio
    async def test_capture_without_budget_is_a_contract_error(self) -> None:
        """A capture direction without a budget would silently do nothing: the
        excerpt stays the default 2000-char head slice. Refuse loudly instead."""
        toolkit = Toolkit(
            metrics=SimpleNamespace(commands_run=0),
            command_runner=AsyncMock(return_value=(0, "ok")),
        )
        with pytest.raises(ValueError, match="output_budget"):
            await toolkit.run_command("make test", capture="tail")

    @pytest.mark.asyncio
    async def test_adapter_splits_budget_across_streams(self, monkeypatch, tmp_path: Path) -> None:
        """The production runner splits the caller's budget across stdout and
        stderr, so the combined output never exceeds it -- concat-then-slice
        could bury stderr under a long stdout (408)."""
        from performer.workflows import adapter
        import performer.workspace as ws

        captured: dict = {}

        async def fake_run_command(cmd, cwd, timeout, *, truncate="head", max_output=2000):
            captured["max_output"] = max_output
            captured["truncate"] = truncate
            return _ci_result("a" * max_output, "b" * max_output, exit_code=1)

        monkeypatch.setattr(ws, "run_command", fake_run_command)
        runner = adapter._command_runner(tmp_path)
        exit_code, output = await runner("make test", tmp_path, 60, output_budget=60_000, capture="both")
        assert captured["max_output"] == 30_000
        assert captured["truncate"] == "both"
        assert len(output) <= 60_000
        assert "b" * 29_000 in output

    @pytest.mark.asyncio
    async def test_adapter_degenerate_budgets_stay_bounded(self, monkeypatch, tmp_path: Path) -> None:
        """A zero or one-character budget cannot exceed itself across the two
        streams: the split floors at zero, so both streams go empty."""
        from performer.workflows import adapter
        import performer.workspace as ws

        async def fake_run_command(cmd, cwd, timeout, *, truncate="head", max_output=2000):
            bounded = max(0, min(max_output, 10))
            return _ci_result("a" * bounded, "b" * bounded, exit_code=1)

        monkeypatch.setattr(ws, "run_command", fake_run_command)
        runner = adapter._command_runner(tmp_path)
        for budget in (0, 1):
            _, output = await runner("make test", tmp_path, 60, output_budget=budget)
            assert len(output) <= budget

    @pytest.mark.asyncio
    async def test_stderr_survives_budget_slice(self) -> None:
        """The old chain tail-truncated stdout and head-sliced the excerpt, so
        a long stdout buried stderr entirely. With a per-call budget the
        runner's stderr lands inside the excerpt."""
        stdout = "a" * 30_000
        stderr = "b" * 29_000 + "STDERR_FAILSUMMARY"

        async def runner(cmd, cwd, timeout_s, *, output_budget=4000, capture="tail"):
            half = max(output_budget // 2, 1)
            return 1, stdout[-half:] + stderr[-half:]

        toolkit = Toolkit(metrics=SimpleNamespace(commands_run=0), command_runner=runner)
        check = await toolkit.run_command("make test", output_budget=60_000, capture="tail")
        assert len(check.output_excerpt) <= 60_000
        assert "STDERR_FAILSUMMARY" in check.output_excerpt


# ---------------------------------------------------------------------------
# project_shape.repo_tree: a budget large enough for the real tree
# ---------------------------------------------------------------------------


def _ls_files_listing(paths: list[str]) -> str:
    return "\n".join(sorted(paths))


class TestRepoTreeBudget:
    @pytest.mark.asyncio
    async def test_repo_tree_sees_paths_beyond_the_default_cap(self, tmp_path: Path) -> None:
        """400 tracked paths is ~6KB of ls-files output: the default 2000-char
        cap showed only the alphabetical tail. The tree must be complete."""
        paths = [f"dir{n:03d}/file{n:03d}.py" for n in range(400)]
        listing = _ls_files_listing(paths)
        assert len(listing) > 2000

        toolkit = SimpleNamespace(
            run_command=AsyncMock(return_value=SimpleNamespace(exit_code=0, output_excerpt=listing))
        )
        tree = await repo_tree(toolkit, tmp_path)
        assert len(tree) == 400
        assert "dir000/file000.py" in tree

    @pytest.mark.asyncio
    async def test_repo_tree_requests_a_large_head_budget(self, tmp_path: Path) -> None:
        """Root manifests (Cargo.toml, go.mod, package.json) sort near the
        front of ls-files output, so the capture must be a HEAD read. The
        request is twice the intended stdout cap: the adapter splits the
        combined budget per stream, so 4M asks 2M for the ls-files stream."""
        toolkit = SimpleNamespace(
            run_command=AsyncMock(return_value=SimpleNamespace(exit_code=0, output_excerpt="main.py"))
        )
        await repo_tree(toolkit, tmp_path)
        kwargs = toolkit.run_command.call_args.kwargs
        assert kwargs["output_budget"] == 4_000_000
        assert kwargs["capture"] == "head"

    @pytest.mark.asyncio
    async def test_root_manifest_reaches_manifest_excerpts_through_production_chain(
        self, tmp_path: Path
    ) -> None:
        """End to end: a real git checkout whose ls-files output exceeds the
        default cap, read through the production runner. package.json sorts
        before the paths that would once have pushed it past the tail."""
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')
        (tmp_path / "src").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        for n in range(200):
            (tmp_path / f"src/module_{n:03d}_{'x' * 40}.py").write_text("pass\n")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=tmp_path, check=True, capture_output=True,
        )

        from performer.workflows.adapter import _command_runner
        from performer.workflows.project_shape import _manifest_excerpts

        toolkit = Toolkit(metrics=SimpleNamespace(commands_run=0), command_runner=_command_runner(tmp_path))
        tree = await repo_tree(toolkit, tmp_path)
        assert "package.json" in tree
        assert len(tree) == 201  # true count, not the tail's path count
        excerpts = _manifest_excerpts(tmp_path, tree)
        assert '"scripts": {"test": "vitest run"}' in excerpts


# ---------------------------------------------------------------------------
# run_tests: a budget the observer can read
# ---------------------------------------------------------------------------


class TestRunTestsBudget:
    @pytest.mark.asyncio
    async def test_run_tests_requests_large_budget_with_both_capture(self, tmp_path: Path) -> None:
        from performer.workflows.implementer.baseline import run_tests
        from performer.workflows.implementer.observe import TestObservation

        toolkit = SimpleNamespace(
            run_command=AsyncMock(return_value=SimpleNamespace(exit_code=0, output_excerpt="ok")),
            call_model=AsyncMock(return_value=TestObservation(outcome="all_passed", summary="ok")),
        )
        await run_tests(toolkit, "pytest", "pytest", tmp_path)
        kwargs = toolkit.run_command.call_args.kwargs
        assert kwargs["output_budget"] >= 40_000
        assert kwargs["capture"] == "both"

    @pytest.mark.asyncio
    async def test_stderr_summary_is_readable_by_the_observer(self, tmp_path: Path) -> None:
        """The failure summary lands at the END of the combined excerpt, inside
        the last 40000 chars the observer reads."""
        from performer.workflows.implementer.baseline import run_tests
        from performer.workflows.implementer.observe import TestObservation

        summary = "OUTPUT_END " + "x" * 45_000 + "5 failed, 4 passed in 3.2s"
        toolkit = SimpleNamespace(
            run_command=AsyncMock(return_value=SimpleNamespace(exit_code=1, output_excerpt=summary)),
        )
        observed: dict = {}

        async def call_model(*args, **kwargs):
            observed["content"] = kwargs["content"]
            return TestObservation(outcome="assertion_failure", summary="readable")

        toolkit.call_model = call_model
        await run_tests(toolkit, "pytest", "pytest", tmp_path)
        text = observed["content"][0]["text"]
        assert "5 failed, 4 passed in 3.2s" in text
