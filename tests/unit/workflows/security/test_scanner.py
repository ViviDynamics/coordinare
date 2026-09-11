"""Unit tests for the security workflow scanner (spec 170, reworked for #366).

The tool pair and the per-tool normalizers are gone: the model decides what to
scan with and reads what the scanners printed. What remains mechanical here is
running a command, classifying its exit, and — the point of #366 — refusing to
call a scan clean when nothing was actually examined.

The suite this replaces had 22 tests, 21 of which exercised
``normalize_semgrep`` / ``normalize_bandit`` / ``build_*_command`` /
``cwe_numbers`` / ``safe_category``. Those are deleted by design, so their tests
are deleted with them rather than rewritten against a shim.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.scanner import ScannerUnavailable, run_scan
from performer.workflows.security.tooling import ExaminedFile, ScanReading, ScanTool

BUDGETS = SecurityBudgets()
FILES = ["app/models/week.rb", "app/controllers/weeks_controller.rb"]


def _tool(name: str = "semgrep", argv: list[str] | None = None) -> ScanTool:
    return ScanTool(name=name, argv=argv or [name, "--json", *FILES], why="applies here")


def _runner(exit_code: int = 0, stdout: str = "{}", stderr: str = ""):
    async def run(argv, cwd, timeout_s):
        return (exit_code, stdout, stderr)

    return run


def _reader(*, examined: list[str] | None = None, findings: list[dict] | None = None, unexamined_reason: str = "could not parse"):
    """A stand-in for the model read."""
    seen = FILES if examined is None else examined

    async def read(tool, argv, stdout, files):
        return ScanReading(
            findings=list(findings or []),
            coverage=[
                ExaminedFile(path=p, examined=p in seen, reason="" if p in seen else unexamined_reason)
                for p in files
            ],
        )

    return read


class TestAbstentionIsNotAPass:
    """#366: the defect this rework exists to remove."""

    @pytest.mark.asyncio
    async def test_a_tool_that_examined_nothing_holds_the_card(self):
        """bandit on a Ruby repo: exit 0, no findings, parsed nothing.

        Previously indistinguishable from a clean scan, which is how every
        non-Python repository got a passing security verdict.
        """
        with pytest.raises(ScannerUnavailable) as exc:
            await run_scan(
                FILES, Path("/repo"), tools=[_tool("bandit")],
                read=_reader(examined=[]), runner=_runner(0, '{"results": []}'),
                budgets=BUDGETS,
            )
        assert "examined none" in exc.value.reason
        assert "could not parse" in exc.value.reason, "the operator needs the tool's own reason"

    @pytest.mark.asyncio
    async def test_partial_coverage_is_not_a_hold(self):
        """One file unread is information, not a stop. Zero read is a stop."""
        _findings, results = await run_scan(
            FILES, Path("/repo"), tools=[_tool()],
            read=_reader(examined=[FILES[0]]), runner=_runner(0, "{}"),
            budgets=BUDGETS,
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_coverage_from_any_tool_is_enough(self):
        """A Python-only tool abstaining is fine if another tool read the code."""
        _findings, results = await run_scan(
            FILES, Path("/repo"),
            tools=[_tool("bandit"), _tool("semgrep")],
            read=_reader(examined=FILES), runner=_runner(0, "{}"),
            budgets=BUDGETS,
        )
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_the_hold_carries_the_results_of_tools_that_ran(self):
        with pytest.raises(ScannerUnavailable) as exc:
            await run_scan(
                FILES, Path("/repo"), tools=[_tool("bandit")],
                read=_reader(examined=[]), runner=_runner(0, "{}"), budgets=BUDGETS,
            )
        assert len(getattr(exc.value, "results", [])) == 1


class TestNoApplicableTooling:
    @pytest.mark.asyncio
    async def test_an_empty_plan_holds_rather_than_passes(self):
        """The model judging that nothing applies is never a clean verdict."""
        with pytest.raises(ScannerUnavailable):
            await run_scan(FILES, Path("/repo"), tools=[], read=_reader(), runner=_runner(), budgets=BUDGETS)

    @pytest.mark.asyncio
    async def test_none_tools_holds_too(self):
        with pytest.raises(ScannerUnavailable):
            await run_scan(FILES, Path("/repo"), tools=None, read=_reader(), runner=_runner(), budgets=BUDGETS)

    @pytest.mark.asyncio
    async def test_no_changed_files_is_not_a_hold(self):
        """Nothing to scan is a different condition from scanning nothing."""
        findings, results = await run_scan([], Path("/repo"), tools=[_tool()], read=_reader(), runner=_runner(), budgets=BUDGETS)
        assert findings == [] and results == []


class TestTheModelSuppliesTheCommand:
    @pytest.mark.asyncio
    async def test_the_planned_argv_is_what_runs(self):
        """No build_*_command: coordinare does not compose scanner invocations."""
        seen: list[list[str]] = []

        async def capture(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        await run_scan(
            FILES, Path("/repo"),
            tools=[ScanTool(name="brakeman", argv=["brakeman", "-f", "json"], why="rails app")],
            read=_reader(), runner=capture, budgets=BUDGETS,
        )
        assert seen == [["brakeman", "-f", "json"]]

    @pytest.mark.asyncio
    async def test_an_arbitrary_tool_name_is_accepted(self):
        """A stack coordinare has never heard of needs no code change."""
        _findings, results = await run_scan(
            FILES, Path("/repo"),
            tools=[ScanTool(name="gosec", argv=["gosec", "-fmt=json", "./..."], why="go module")],
            read=_reader(), runner=_runner(), budgets=BUDGETS,
        )
        assert results[0].tool == "gosec"

    @pytest.mark.asyncio
    async def test_findings_come_from_the_reader_not_a_normalizer(self):
        finding = {"path": "app/models/week.rb", "line": 3, "severity": "high"}
        findings, results = await run_scan(
            FILES, Path("/repo"), tools=[_tool()],
            read=_reader(findings=[finding]), runner=_runner(), budgets=BUDGETS,
        )
        # stamped with the tool that produced it. The gate used to recover this
        # by asking whether the description started with "bandit:", which is a
        # guess that only works for two scanners coordinare was built knowing.
        assert findings == [{**finding, "tool": "semgrep"}]
        assert results[0].finding_count == 1

    @pytest.mark.asyncio
    async def test_output_that_is_not_json_still_reaches_the_reader(self):
        """A scanner that prints a table is as readable as one that prints JSON."""
        got: list[str] = []

        async def read(tool, argv, stdout, files):
            got.append(stdout)
            return ScanReading(coverage=[ExaminedFile(path=p, examined=True) for p in files])

        await run_scan(
            FILES, Path("/repo"), tools=[_tool()], read=read,
            runner=_runner(0, "WARN app/models/week.rb:3 command injection"), budgets=BUDGETS,
        )
        assert got == ["WARN app/models/week.rb:3 command injection"]


class TestToolExecutionProblemsStillFailClosed:
    """Unchanged from spec 170: a tool that did not run is a hold."""

    @pytest.mark.asyncio
    async def test_missing_binary(self):
        async def missing(argv, cwd, timeout_s):
            raise FileNotFoundError(argv[0])

        with pytest.raises(ScannerUnavailable) as exc:
            await run_scan(FILES, Path("/repo"), tools=[_tool()], read=_reader(), runner=missing, budgets=BUDGETS)
        assert "binary not found" in exc.value.reason

    @pytest.mark.asyncio
    async def test_empty_output(self):
        with pytest.raises(ScannerUnavailable) as exc:
            await run_scan(FILES, Path("/repo"), tools=[_tool()], read=_reader(), runner=_runner(0, "   "), budgets=BUDGETS)
        assert "produced no output" in exc.value.reason

    @pytest.mark.parametrize("code", [2, 3, 127, 137])
    @pytest.mark.asyncio
    async def test_an_exit_code_other_than_zero_or_one_is_a_tool_problem(self, code):
        with pytest.raises(ScannerUnavailable):
            await run_scan(FILES, Path("/repo"), tools=[_tool()], read=_reader(), runner=_runner(code, "{}"), budgets=BUDGETS)

    @pytest.mark.parametrize("code", [0, 1])
    @pytest.mark.asyncio
    async def test_zero_and_one_both_mean_the_tool_ran(self, code):
        _findings, results = await run_scan(
            FILES, Path("/repo"), tools=[_tool()], read=_reader(), runner=_runner(code, "{}"), budgets=BUDGETS,
        )
        assert results[0].exit_code == code
