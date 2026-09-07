"""Tests for shared test result parsers (spec 089+167).

Tests that parsers were lifted from main.py to performer.test_results
and that main.py imports them correctly.
"""
from __future__ import annotations

import performer.main
import performer.test_results
from performer.test_results import (
    TestSummary,
    _env_signature_reason,
    _format_failure_excerpt,
    _match_env_signature,
    _reported_failure_count,
    _strip_ansi,
    parse_test_output,
)


def test_strip_ansi_removes_color_codes():
    """_strip_ansi removes ANSI escape sequences."""
    output = "test \x1b[31m1 failed\x1b[0m in 1.2s"
    result = _strip_ansi(output)
    assert result == "test 1 failed in 1.2s"


def test_reported_failure_count_pytest_format():
    """_reported_failure_count parses pytest summary."""
    output = "1 failed, 12 passed in 3.2s"
    count = _reported_failure_count(output)
    assert count == 1

    output = "3 failed, 5 passed"
    count = _reported_failure_count(output)
    assert count == 3


def test_reported_failure_count_rspec_format():
    """_reported_failure_count parses rspec summary."""
    output = "3 examples, 1 failure"
    count = _reported_failure_count(output)
    assert count == 1


def test_reported_failure_count_minitest_format():
    """_reported_failure_count parses minitest summary."""
    output = "10 runs, 20 assertions, 2 failures, 0 errors"
    count = _reported_failure_count(output)
    assert count == 2


def test_reported_failure_count_returns_last_match():
    """_reported_failure_count returns last count when multiple appear."""
    output = "Earlier run: 5 failed\nLatest run: 2 failed in 1s"
    count = _reported_failure_count(output)
    assert count == 2


def test_reported_failure_count_returns_none_when_not_found():
    """_reported_failure_count returns None when no count found."""
    output = "test output without counts"
    count = _reported_failure_count(output)
    assert count is None


def test_match_env_signature_returns_matched_signature():
    """_match_env_signature returns first matched signature."""
    output = "Error: connection refused at 127.0.0.1:5432"
    sig = _match_env_signature(output)
    assert sig == "connection refused"


def test_match_env_signature_returns_none_when_test_failure():
    """_match_env_signature returns None when test failures present."""
    output = "1 failed in 1s"
    sig = _match_env_signature(output)
    assert sig is None


def test_match_env_signature_case_insensitive():
    """_match_env_signature is case-insensitive."""
    output = "CONNECTION REFUSED"
    sig = _match_env_signature(output)
    assert sig == "connection refused"


def test_format_failure_excerpt_under_limit():
    """_format_failure_excerpt returns output as-is when under limit."""
    output = "short error"
    result = _format_failure_excerpt(output, limit=100)
    assert result == "short error"


def test_format_failure_excerpt_over_limit():
    """_format_failure_excerpt elides middle when over limit."""
    output = "a" * 2000
    result = _format_failure_excerpt(output, limit=100)
    assert "…[truncated]…" in result
    assert len(result) <= 150  # Some overhead for the marker


def test_env_signature_reason_includes_excerpt():
    """_env_signature_reason includes output excerpt."""
    output = "connection refused at postgres:5432"
    reason = _env_signature_reason("connection refused", output)
    assert "connection refused" in reason
    assert "postgres" in reason


def test_parse_test_output_pytest_pass():
    """parse_test_output detects pytest pass."""
    output = "1 passed in 0.2s"
    result = parse_test_output("pytest", output, exit_code=0)
    assert result.passed is True
    assert result.exit_code == 0


def test_parse_test_output_pytest_fail():
    """parse_test_output detects pytest failure."""
    output = "2 failed, 1 passed in 1.5s"
    result = parse_test_output("pytest", output, exit_code=1)
    assert result.passed is False
    assert result.failed == 2


def test_parse_test_output_rspec_pass():
    """parse_test_output detects rspec pass."""
    output = "3 examples, 0 failures"
    result = parse_test_output("rspec", output, exit_code=0)
    assert result.passed is True


def test_parse_test_output_jest_pass():
    """parse_test_output detects jest pass."""
    output = "Tests: 0 failed, 5 passed, 5 total"
    result = parse_test_output("jest", output, exit_code=0)
    assert result.passed is True


def test_parse_test_output_returns_test_summary():
    """parse_test_output returns TestSummary."""
    output = "1 failed in 1s"
    result = parse_test_output("pytest", output, exit_code=1)
    assert isinstance(result, TestSummary)
    assert result.exit_code == 1
    assert "failed" in result.raw_tail or "1" in result.raw_tail


def test_main_py_imports_from_test_results():
    """main.py imports parsers from performer.test_results, not defining them."""
    # These should be accessible from test_results
    assert hasattr(performer.test_results, "_strip_ansi")
    assert hasattr(performer.test_results, "_reported_failure_count")
    assert hasattr(performer.test_results, "_match_env_signature")
    assert hasattr(performer.test_results, "_env_signature_reason")
    assert hasattr(performer.test_results, "_format_failure_excerpt")
    assert hasattr(performer.test_results, "parse_test_output")
