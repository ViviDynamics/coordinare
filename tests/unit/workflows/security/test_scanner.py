"""Unit tests for the security workflow scanner (spec 170).

Strategy: mock the async runner to test normalization, ScannerUnavailable
handling, the no-files path, and command building.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from pathlib import Path as _Path

import pytest
import pytest as _pytest
from performer.workflows.security.budgets import SecurityBudgets as _Budgets
from performer.workflows.security.scanner import (
    ScannerUnavailable,
    build_bandit_command,
    build_semgrep_command,
    cwe_numbers,
    normalize_bandit,
    normalize_semgrep,
    run_scan,
    safe_category,
)
from performer.workflows.security.scanner import ScannerUnavailable as _Unavailable
from performer.workflows.security.scanner import run_scan as _run_scan


class TestNormalizers:
    """Test that normalization matches coordinare behavior."""

    def test_normalize_semgrep_error_level(self):
        result = {
            "check_id": "python.lang.security.audit.dangerous-system-call",
            "path": "app.py",
            "start": {"line": 10},
            "extra": {
                "severity": "ERROR",
                "message": "Found user input flowing into os.system",
                "metadata": {"cwe": ["CWE-78: OS Command Injection"]},
            },
        }
        finding = normalize_semgrep(result)
        assert finding["severity"] == "critical"
        assert finding["file"] == "app.py"
        assert finding["line"] == 10

    def test_normalize_semgrep_warning_dangerous_cwe(self):
        result = {
            "check_id": "javascript.browser.security.insecure-innerhtml",
            "path": "ui.js",
            "start": {"line": 20},
            "extra": {
                "severity": "WARNING",
                "message": "innerHTML assignment from user input",
                "metadata": {"cwe": ["CWE-79: Cross-site Scripting"]},
            },
        }
        finding = normalize_semgrep(result)
        assert finding["severity"] == "high"
        assert finding["line"] == 20

    def test_normalize_semgrep_warning_safe_cwe(self):
        result = {
            "check_id": "python.lang.best-practice",
            "path": "util.py",
            "start": {"line": 5},
            "extra": {
                "severity": "WARNING",
                "message": "Variable name is too short",
                "metadata": {"cwe": ["CWE-500: Not a security CWE"]},
            },
        }
        finding = normalize_semgrep(result)
        assert finding["severity"] == "medium"

    def test_normalize_semgrep_info_level(self):
        result = {
            "check_id": "info.rule",
            "path": "app.py",
            "start": {"line": 1},
            "extra": {"severity": "INFO", "metadata": {}},
        }
        finding = normalize_semgrep(result)
        assert finding["severity"] == "low"

    def test_normalize_bandit_high_high(self):
        result = {
            "filename": "app.py",
            "line_number": 15,
            "issue_severity": "HIGH",
            "issue_confidence": "HIGH",
            "test_id": "B602",
            "test_name": "subprocess_popen_with_shell_equals_true",
        }
        finding = normalize_bandit(result)
        assert finding["severity"] == "critical"
        assert finding["line"] == 15

    def test_normalize_bandit_high_medium(self):
        result = {
            "filename": "app.py",
            "line_number": 10,
            "issue_severity": "HIGH",
            "issue_confidence": "MEDIUM",
            "test_id": "B602",
        }
        finding = normalize_bandit(result)
        assert finding["severity"] == "high"

    def test_normalize_bandit_medium_high(self):
        result = {
            "filename": "app.py",
            "line_number": 10,
            "issue_severity": "MEDIUM",
            "issue_confidence": "HIGH",
            "test_id": "B602",
        }
        finding = normalize_bandit(result)
        assert finding["severity"] == "high"

    def test_normalize_bandit_medium(self):
        result = {
            "filename": "app.py",
            "line_number": 10,
            "issue_severity": "MEDIUM",
            "issue_confidence": "LOW",
            "test_id": "B602",
        }
        finding = normalize_bandit(result)
        assert finding["severity"] == "medium"

    def test_normalize_bandit_high_low(self):
        result = {
            "filename": "app.py",
            "line_number": 10,
            "issue_severity": "HIGH",
            "issue_confidence": "LOW",
            "test_id": "B602",
        }
        finding = normalize_bandit(result)
        assert finding["severity"] == "high"

    def test_normalize_bandit_low(self):
        result = {
            "filename": "app.py",
            "line_number": 10,
            "issue_severity": "LOW",
            "issue_confidence": "HIGH",
            "test_id": "B602",
        }
        finding = normalize_bandit(result)
        assert finding["severity"] == "low"


class TestCweNumbers:
    """Test CWE extraction from semgrep metadata."""

    def test_cwe_numbers_single(self):
        metadata = {"cwe": ["CWE-78: OS Command Injection"]}
        assert cwe_numbers(metadata) == ["78"]

    def test_cwe_numbers_multiple(self):
        metadata = {"cwe": ["CWE-89: SQL Injection", "CWE-79: XSS"]}
        assert cwe_numbers(metadata) == ["89", "79"]

    def test_cwe_numbers_string_not_list(self):
        metadata = {"cwe": "CWE-798: Hardcoded Credentials"}
        assert cwe_numbers(metadata) == ["798"]

    def test_cwe_numbers_empty(self):
        metadata = {"cwe": []}
        assert cwe_numbers(metadata) == []

    def test_cwe_numbers_missing(self):
        metadata = {}
        assert cwe_numbers(metadata) == []

    def test_cwe_numbers_none_metadata(self):
        assert cwe_numbers(None) == []


class TestSafeCategory:
    """Test category label sanitization."""

    def test_safe_category_short(self):
        assert safe_category("injection") == "injection"

    def test_safe_category_strips_whitespace(self):
        assert safe_category("  whitespace  ") == "whitespace"

    def test_safe_category_caps_at_80(self):
        long_name = "x" * 100
        result = safe_category(long_name)
        assert len(result) == 80


class TestBuildCommands:
    """Test command builder functions."""

    def test_build_semgrep_command(self):
        cmd = build_semgrep_command(["app.py", "util.py"], "auto")
        assert cmd == ["semgrep", "--config", "auto", "--json", "app.py", "util.py"]

    def test_build_bandit_command(self):
        cmd = build_bandit_command(["app.py", "util.py"])
        assert cmd == ["bandit", "-f", "json", "-r", "app.py", "util.py"]


class FakeScannerUnavailable:
    """Fake runner that signals scanner unavailable."""

    def __init__(self, tool: str, reason: str):
        self.tool = tool
        self.reason = reason

    async def __call__(self, argv, cwd, timeout_s):
        raise ScannerUnavailable(self.tool, self.reason)


class FakeScannerOutput:
    """Fake runner that returns canned output."""

    def __init__(self, semgrep_out: dict, bandit_out: dict):
        self.semgrep_out = semgrep_out
        self.bandit_out = bandit_out

    async def __call__(self, argv, cwd, timeout_s):
        import json

        tool = argv[0]
        if tool == "semgrep":
            return (0, json.dumps(self.semgrep_out), "")
        else:
            return (0, json.dumps(self.bandit_out), "")


class TestRunScan:
    """Test async run_scan function."""

    @pytest.mark.asyncio
    async def test_run_scan_no_files(self):
        """With no files, returns ([], []) without running."""
        async def dummy_runner(argv, cwd, timeout_s):
            raise AssertionError("should not be called")

        from performer.workflows.security.budgets import SecurityBudgets

        budgets = SecurityBudgets(scan_timeout_s=120, semgrep_config="auto", survey_max_commands=12, survey_max_output_chars=4000, max_findings=30)
        findings, results = await run_scan([], Path("."), runner=dummy_runner, budgets=budgets)
        assert findings == []
        assert results == []

    @pytest.mark.asyncio
    async def test_run_scan_clean(self):
        """Clean scan returns empty findings list."""
        runner = FakeScannerOutput({"results": [], "errors": []}, {"results": [], "errors": [], "metrics": {}})

        from performer.workflows.security.budgets import SecurityBudgets

        budgets = SecurityBudgets(scan_timeout_s=120, semgrep_config="auto", survey_max_commands=12, survey_max_output_chars=4000, max_findings=30)
        findings, results = await run_scan(["app.py"], Path("."), runner=runner, budgets=budgets)
        assert findings == []
        assert len(results) == 2  # semgrep and bandit
        assert all(r.finding_count == 0 for r in results)

    @pytest.mark.asyncio
    async def test_run_scan_semgrep_unavailable(self):
        """Semgrep binary missing raises ScannerUnavailable."""
        runner = FakeScannerUnavailable("semgrep", "binary not found")

        from performer.workflows.security.budgets import SecurityBudgets

        budgets = SecurityBudgets(scan_timeout_s=120, semgrep_config="auto", survey_max_commands=12, survey_max_output_chars=4000, max_findings=30)
        with pytest.raises(ScannerUnavailable) as exc_info:
            await run_scan(["app.py"], Path("."), runner=runner, budgets=budgets)
        assert exc_info.value.tool == "semgrep"
        assert "binary not found" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_run_scan_tool_order(self):
        """Scan runs semgrep then bandit in order."""
        call_order = []

        async def ordered_runner(argv, cwd, timeout_s):
            tool = argv[0]
            call_order.append(tool)
            import json

            if tool == "semgrep":
                return (0, json.dumps({"results": [], "errors": []}), "")
            else:
                return (0, json.dumps({"results": [], "errors": [], "metrics": {}}), "")

        from performer.workflows.security.budgets import SecurityBudgets

        budgets = SecurityBudgets(scan_timeout_s=120, semgrep_config="auto", survey_max_commands=12, survey_max_output_chars=4000, max_findings=30)
        await run_scan(["app.py"], Path("."), runner=ordered_runner, budgets=budgets)
        assert call_order == ["semgrep", "bandit"]

    @pytest.mark.asyncio
    async def test_run_scan_malformed_json(self):
        """Malformed JSON output raises ScannerUnavailable."""
        async def bad_json_runner(argv, cwd, timeout_s):
            return (1, "not valid json {{{", "")

        from performer.workflows.security.budgets import SecurityBudgets

        budgets = SecurityBudgets(scan_timeout_s=120, semgrep_config="auto", survey_max_commands=12, survey_max_output_chars=4000, max_findings=30)
        with pytest.raises(ScannerUnavailable) as exc_info:
            await run_scan(["app.py"], Path("."), runner=bad_json_runner, budgets=budgets)
        assert "unparseable JSON" in exc_info.value.reason

    @pytest.mark.asyncio
    async def test_run_scan_empty_output(self):
        """Empty output raises ScannerUnavailable."""
        async def empty_runner(argv, cwd, timeout_s):
            return (1, "", "")

        from performer.workflows.security.budgets import SecurityBudgets

        budgets = SecurityBudgets(scan_timeout_s=120, semgrep_config="auto", survey_max_commands=12, survey_max_output_chars=4000, max_findings=30)
        with pytest.raises(ScannerUnavailable) as exc_info:
            await run_scan(["app.py"], Path("."), runner=empty_runner, budgets=budgets)
        assert "produced no output" in exc_info.value.reason


class TestScannerUnavailable:
    """Test ScannerUnavailable exception."""

    def test_scanner_unavailable_attributes(self):
        exc = ScannerUnavailable("semgrep", "binary not found")
        assert exc.tool == "semgrep"
        assert exc.reason == "binary not found"
        assert "semgrep" in str(exc)


# --- review findings (spec 170 PR): exit codes and partial results -------------------------------




def _runner(exit_codes: dict[str, int], fail_second: str | None = None):
    async def runner(argv, cwd, timeout_s):
        tool = _Path(argv[0]).name
        if tool == fail_second:
            raise FileNotFoundError(tool)
        return exit_codes.get(tool, 0), _json.dumps({"results": [], "errors": []}), ""

    return runner


@_pytest.mark.asyncio
@_pytest.mark.parametrize("code", [2, 127, -9])
async def test_an_exit_code_other_than_zero_or_one_is_a_tool_problem_even_with_json(code):
    """Review finding: semgrep exit 2 (fatal) or 127 (not found via a wrapper) with a parseable body must hold, not pass."""
    with _pytest.raises(_Unavailable) as exc:
        await _run_scan(["a.py"], _Path("."), runner=_runner({"semgrep": code}), budgets=_Budgets())
    assert exc.value.tool == "semgrep" and f"exited {code}" in exc.value.reason


@_pytest.mark.asyncio
async def test_exit_one_with_json_is_a_scan_with_findings():
    findings, results = await _run_scan(["a.py"], _Path("."), runner=_runner({"semgrep": 1, "bandit": 1}), budgets=_Budgets())
    assert findings == [] and [r.exit_code for r in results] == [1, 1]


@_pytest.mark.asyncio
async def test_a_failure_in_the_second_tool_keeps_the_first_tools_result_on_the_exception():
    """Review finding: semgrep completed, bandit missing; the record must still show semgrep ran."""
    with _pytest.raises(_Unavailable) as exc:
        await _run_scan(["a.py"], _Path("."), runner=_runner({}, fail_second="bandit"), budgets=_Budgets())
    assert exc.value.tool == "bandit" and [r.tool for r in exc.value.results] == ["semgrep"]
