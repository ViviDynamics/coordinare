"""Test that main.py imports lifted parsers from performer.test_results (spec 167, item 1a)."""


def test_main_imports_from_test_results():
    """Verify main.py imports _strip_ansi from test_results (not duplicate)."""
    from performer import main, test_results

    assert main._strip_ansi is test_results._strip_ansi
    assert main._reported_failure_count is test_results._reported_failure_count
    assert main._match_env_signature is test_results._match_env_signature
    assert main._env_signature_reason is test_results._env_signature_reason
    assert main._format_failure_excerpt is test_results._format_failure_excerpt


def test_strip_ansi_removes_codes():
    """ANSI codes are stripped."""
    from performer.test_results import _strip_ansi

    colored = "\x1b[31m1 failed\x1b[0m"
    plain = _strip_ansi(colored)
    assert "\x1b" not in plain
    assert "1 failed" in plain


def test_reported_failure_count_extracts_count():
    """Failure count is extracted from output."""
    from performer.test_results import _reported_failure_count

    output = "FAILED tests/x.py::test_y  13 failed, 2 passed"
    count = _reported_failure_count(output)
    assert count == 13


def test_match_env_signature_finds_signature():
    """Environment signature is matched."""
    from performer.test_results import _match_env_signature

    output = "Error: connection refused to database"
    sig = _match_env_signature(output)
    assert sig == "connection refused"


def test_match_env_signature_returns_none_on_real_failure():
    """Real test failure short-circuits environment matching."""
    from performer.test_results import _match_env_signature

    output = "1 failed, 3 passed\nConnection refused (spurious phrase)"
    sig = _match_env_signature(output)
    assert sig is None


def test_format_failure_excerpt_truncates():
    """Long output is truncated with head and tail."""
    from performer.test_results import _format_failure_excerpt

    long_output = "a" * 1000 + "\n" + "b" * 1000
    excerpt = _format_failure_excerpt(long_output, limit=500)
    assert len(excerpt) < len(long_output)
    assert "[truncated]" in excerpt
