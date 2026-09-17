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
from performer.workflows.security.scanner import (
    FILES_TOKEN,
    NothingToScan,
    ScannerUnavailable,
    run_scan,
)
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
    async def test_a_coverage_row_for_an_unrelated_path_is_not_coverage(self):
        """412 round 24: examined paths are trusted only for the files the
        run actually scoped -- a malformed or hallucinated coverage row for
        an unrelated path must not turn a zero-coverage run into a clean
        one."""
        async def read(tool, argv, stdout, files):
            return ScanReading(
                findings=[],
                coverage=[ExaminedFile(path="vendor/unrelated.py", examined=True)],
            )

        with pytest.raises(ScannerUnavailable) as exc:
            await run_scan(
                FILES, Path("/repo"), tools=[_tool()],
                read=read, runner=_runner(0, "{}"),
                budgets=BUDGETS,
            )
        assert "examined none" in exc.value.reason

    @pytest.mark.asyncio
    async def test_a_changed_path_that_starts_with_dash_is_a_target(self):
        """412 round 24: a changed path like ``-flag.py`` would parse as an
        option -- spelled ``./-flag.py`` so the tool reads it as a target
        and the command stays intact."""
        seen: list = []

        async def run(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        files = ["-flag.py"]
        await run_scan(
            files, Path("/repo"),
            tools=[_tool(argv=["semgrep", "--json", FILES_TOKEN])],
            read=_reader(examined=files), runner=run, budgets=BUDGETS,
        )
        assert seen == [["semgrep", "--json", "./-flag.py"]]

    @pytest.mark.asyncio
    async def test_a_bare_subcommand_token_is_not_repointed_at_the_changed_set(self, tmp_path):
        """412 round 25: a known subcommand of the tool is CLI plumbing, not
        an operand -- ``trivy fs`` with a repo-local ``fs`` directory keeps
        its subcommand and the changed set rides where ``.`` used to sit."""
        seen: list = []

        async def run(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        repo = tmp_path
        (repo / "fs").mkdir()
        files = ["app.py"]
        await run_scan(
            files, repo,
            tools=[_tool(argv=["trivy", "fs", "."])],
            read=_reader(examined=files), runner=run, budgets=BUDGETS,
        )
        assert seen == [["trivy", "fs", "app.py"]]

    @pytest.mark.asyncio
    async def test_a_subcommand_after_global_options_is_not_repointed(self, tmp_path):
        """412 round 40: the subcommand slot is any bare token before the
        first operand -- ``trivy --quiet fs .`` with a repo-local ``fs``
        directory keeps its subcommand instead of repointing ``fs`` at the
        changed set (which corrupts the CLI shape into a hold)."""
        seen: list = []

        async def run(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        repo = tmp_path
        (repo / "fs").mkdir()
        files = ["app.py"]
        await run_scan(
            files, repo,
            tools=[_tool(argv=["trivy", "--quiet", "fs", "."])],
            read=_reader(examined=files), runner=run, budgets=BUDGETS,
        )
        assert seen == [["trivy", "--quiet", "fs", "app.py"]]

    @pytest.mark.asyncio
    async def test_a_subcommand_after_an_option_value_is_not_repointed(self, tmp_path):
        """412 round 40: a whitelisted option VALUE is plumbing and does not
        end the option prefix, so ``--format json fs`` still recognizes the
        subcommand that follows it."""
        seen: list = []

        async def run(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        repo = tmp_path
        (repo / "fs").mkdir()
        files = ["app.py"]
        await run_scan(
            files, repo,
            tools=[_tool(argv=["trivy", "--format", "json", "fs", "."])],
            read=_reader(examined=files), runner=run, budgets=BUDGETS,
        )
        assert seen == [["trivy", "--format", "json", "fs", "app.py"]]

    @pytest.mark.asyncio
    async def test_an_operand_outside_the_workspace_is_refused(self):
        """412 round 41: a bare absolute operand outside the workspace is a
        scan target the scoping cannot bound -- the tool reads it beside the
        changed set and its findings can carry the file's contents into the
        report -- so the plan is refused and the card holds."""
        with pytest.raises(ScannerUnavailable) as exc:
            await run_scan(
                FILES, Path("/repo"),
                tools=[_tool(argv=["semgrep", "/etc/passwd"])],
                read=_reader(examined=FILES), runner=_runner(0, "{}"), budgets=BUDGETS,
            )
        assert "outside the workspace" in exc.value.reason

    @pytest.mark.asyncio
    async def test_an_absolute_operand_inside_the_workspace_is_a_target(self, tmp_path):
        """412 round 15: an absolute operand under the workspace root is as
        much a scan target as its relative spelling -- replaced by the
        changed set, not refused by the round-41 outside-workspace guard."""
        seen: list = []

        async def run(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        repo = tmp_path
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        files = ["app.py"]
        await run_scan(
            files, repo,
            tools=[_tool(argv=["semgrep", str(repo / "app.py"), "."])],
            read=_reader(examined=files), runner=run, budgets=BUDGETS,
        )
        assert seen == [["semgrep", "app.py"]]

    @pytest.mark.asyncio
    async def test_an_absolute_option_value_outside_the_workspace_stays_a_report_path(self):
        """412 round 41: config and report paths ride option values -- the
        refusal reaches bare operands only, and whitelisted value-takers are
        preserved plumbing."""
        seen: list = []

        async def run(argv, cwd, timeout_s):
            seen.append(list(argv))
            return (0, "{}", "")

        files = ["app.py"]
        await run_scan(
            files, Path("/repo"),
            tools=[_tool(argv=["semgrep", "--output", "/tmp/report.json", "."])],
            read=_reader(examined=files), runner=run, budgets=BUDGETS,
        )
        assert seen == [["semgrep", "--output", "/tmp/report.json", "app.py"]]

    @pytest.mark.asyncio
    async def test_a_dot_slash_prefixed_coverage_path_is_normalized(self):
        """412 round 26: tool output names paths the way the tool printed
        them (``./src/app.py`` for ``src/app.py``) -- coverage is normalized
        exactly as findings are, or a valid run reads as zero coverage."""
        async def read(tool, argv, stdout, files):
            return ScanReading(
                findings=[],
                coverage=[ExaminedFile(path="./" + FILES[0], examined=True)],
            )

        _findings, results = await run_scan(
            FILES, Path("/repo"), tools=[_tool()],
            read=read, runner=_runner(0, "{}"), budgets=BUDGETS,
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_a_dash_prefixed_changed_file_named_in_the_plan_is_a_target(self):
        """412 round 27: ``-flag.py`` is a real filename -- a plan that names
        it literally (without ``{{files}}``) must not leave the option-token
        in argv; the token is replaced by the ``./``-spelled change set."""
        seen_argv: list[list[str]] = []

        async def run(argv, cwd, timeout_s):
            seen_argv.append(list(argv))
            return (0, "{}", "")

        async def read(tool, argv, stdout, files):
            return ScanReading(findings=[], coverage=[ExaminedFile(path="-flag.py", examined=True)])

        _findings, results = await run_scan(
            ["-flag.py"], Path("/repo"), tools=[_tool(argv=["semgrep", "-flag.py"])],
            read=read, runner=run, budgets=BUDGETS,
        )
        assert seen_argv == [["semgrep", "./-flag.py"]]
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
        """Nothing to scan is a different condition from scanning nothing: the
        scanner raises NothingToScan, which the workflow reports as the
        explicit nothing_to_scan verdict rather than a hold or a pass."""
        with pytest.raises(NothingToScan):
            await run_scan([], Path("/repo"), tools=[_tool()], read=_reader(), runner=_runner(), budgets=BUDGETS)


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
        # 412: no {{files}} token in the plan means coordinare appends the
        # changed files; the plan itself is never trusted to name them.
        assert seen == [["brakeman", "-f", "json", *FILES]]

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
        """412: empty stdout after a declared-ok exit is a clean quiet run, not
        a crash. The reading's per-file coverage is what separates read-nothing
        from read-everything; an abstention still holds the card there."""
        _findings, results = await run_scan(
            FILES, Path("/repo"), tools=[_tool()], read=_reader(examined=FILES), runner=_runner(0, "   "), budgets=BUDGETS,
        )
        assert results[0].exit_code == 0

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
