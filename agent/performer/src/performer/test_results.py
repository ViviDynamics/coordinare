"""Test output parsing for the implementer workflow (spec 167).

Lifts output parsers from spec-089 main.py:
- Environment signature matching for local test gate
- Test output formatting for feedback
- Test result classification (pass/fail, attempt counts)

These functions are used by both the 089 local gate and the implementer
workflow to detect red and green.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "TestSummary",
    "parse_test_output",
    "_strip_ansi",
    "_reported_failure_count",
    "_match_env_signature",
    "_env_signature_reason",
    "_format_failure_excerpt",
]


#: Substrings (matched case-insensitively against the failure output) that
#: attribute a local test failure to the environment rather than the diff.
#: Deliberately conservative: ambiguous signatures that frequently indicate a
#: real code defect are EXCLUDED so a genuine bug is never masked as an
#: environment block. These cover failure modes that surface only in test
#: stdout/stderr (refused service socket, port collision, missing toolchain).
_ENV_FAILURE_SIGNATURES: tuple[str, ...] = (
    "connection refused",
    "could not connect to",
    "couldn't connect to",
    "failed to connect to",
    "address already in use",
    "errno 98",  # EADDRINUSE (Linux)
    "errno 48",  # EADDRINUSE (macOS)
    "errno 111",  # ECONNREFUSED (Linux)
    "errno 61",  # ECONNREFUSED (macOS)
    "no such host",
    "name or service not known",
    "temporary failure in name resolution",
    "no space left on device",
)


#: Test runner reporting failed tests. Without this precedence a signature
#: in incidental output masks a real red test: assertion diffs and fixture
#: data routinely embed environment-shaped phrases, so a genuine failure
#: of one of those tests prints the phrase and would otherwise be held
#: as env_blocked instead of being handed back to the implementer.
_TEST_FAILURE_MARKERS = re.compile(
    r"[1-9]\d*\s+failed(?:,|\s+in\s)"  # pytest "1 failed, 12 passed"
    r"|^\s*[1-9]\d*\s+failing\s*$"  # mocha "  1 failing"
    r"|,\s*[1-9]\d*\s+failures?\b"  # rspec "3 examples, 1 failure"
    r"|failures:\s*[1-9]"  # maven/gradle "Failures: 1"
    r"|^\s*\d+\)\s+Failure:"  # minitest per-failure header
    r"|^\s*Failure/Error:"  # rspec per-failure detail
    r"|^FAILED\s+\S+::",  # pytest per-test failure line
    re.IGNORECASE | re.MULTILINE,
)


#: ANSI/CSI escape sequences that color output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


#: Failure count patterns. Anchored the same way _TEST_FAILURE_MARKERS is.
_FAILURE_COUNT_PATTERNS = (
    re.compile(r"([1-9]\d*)\s+failed(?:,|\s+in\s)", re.IGNORECASE),
    re.compile(r"^\s*([1-9]\d*)\s+failing\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r",\s*([1-9]\d*)\s+failures?\b", re.IGNORECASE),
    re.compile(r"failures:\s*([1-9]\d*)", re.IGNORECASE),
)


@dataclass(frozen=True)
class TestSummary:
    """Parsed test run output (spec 167 FR-004).

    Attributes:
        passed: True when all tests passed.
        failed: Count of failed tests, or None if unavailable.
        test_names_passed: List of passing test identifiers, or None.
        test_names_failed: List of failing test identifiers, or None.
        exit_code: Process exit code.
        raw_tail: Last 2000 chars of output for debugging.
    """

    passed: bool
    failed: int | None = None
    test_names_passed: list[str] | None = None
    test_names_failed: list[str] | None = None
    exit_code: int | None = None
    raw_tail: str = ""


def _strip_ansi(text: str) -> str:
    """Remove ANSI color codes from output."""
    return _ANSI_RE.sub("", text)


def _reported_failure_count(output: str) -> int | None:
    """Extract failing test count from runner summary.

    Returns the LAST count in output (runner summary is at the end after
    tail-truncation), or None if no count found.
    """
    matches = [
        (match.end(), int(match.group(1)))
        for pattern in _FAILURE_COUNT_PATTERNS
        for match in pattern.finditer(output)
    ]
    if not matches:
        return None
    return max(matches)[1]


def _match_env_signature(output: str) -> str | None:
    """Return first ENV_FAILURE_SIGNATURES substring in output, or None.

    A runner-reported test failure (TEST_FAILURE_MARKERS) short-circuits
    to None: if tests ran and failed, this is a code defect even if output
    mentions an environment phrase.
    """
    if _TEST_FAILURE_MARKERS.search(output):
        return None
    haystack = output.lower()
    for sig in _ENV_FAILURE_SIGNATURES:
        if sig in haystack:
            return sig
    return None


def _env_signature_reason(signature: str, output: str) -> str:
    """Build human-readable env-block reason for a matched signature.

    Includes a bounded excerpt of output so the cause is clear without
    having to read performer logs.
    """
    excerpt = _format_failure_excerpt(output, limit=400)
    reason = f"local test output matched environment-failure signature: '{signature}'"
    return f"{reason}\n{excerpt}" if excerpt else reason


def _format_failure_excerpt(output: str, *, limit: int = 1500) -> str:
    """Clamp failure output, keeping head and tail with elision.

    Output is already tail-truncated by run_command; if still over limit
    chars, keep head and tail so both first failure and final summary survive.
    Includes redaction of auth headers for safe sharing in PR comments.
    """
    from performer.workspace import _redact_auth_headers

    output = _redact_auth_headers(output.strip())
    if len(output) <= limit:
        return output
    head = output[: limit // 3]
    tail = output[-(limit - limit // 3) :]
    return f"{head}\n…[truncated]…\n{tail}"


def parse_test_output(
    runner_kind: str,
    output: str,
    exit_code: int,
) -> TestSummary:
    """Parse test runner output into a TestSummary.

    Args:
        runner_kind: One of "pytest", "rspec", "jest", "minitest", "make".
        output: Raw test command stdout/stderr.
        exit_code: Process exit code.

    Returns:
        TestSummary with passed, failed count, test names (when available),
        exit_code, and output tail for debugging.
    """
    stripped = _strip_ansi(output.strip())
    passed = exit_code == 0
    failed = _reported_failure_count(stripped)
    raw_tail = stripped[-(2000):] if len(stripped) > 2000 else stripped

    test_names_passed = None
    test_names_failed = None

    if runner_kind == "pytest":
        # Parse pytest --collect-only -q or summary lines like "PASSED tests/x.py::test_y"
        test_names_passed, test_names_failed = _parse_pytest_names(stripped)
    elif runner_kind == "rspec":
        # Parse rspec documentation format
        test_names_passed, test_names_failed = _parse_rspec_names(stripped)
    elif runner_kind == "jest":
        # Parse jest test list and ✓/✕ lines
        test_names_passed, test_names_failed = _parse_jest_names(stripped)
    elif runner_kind == "minitest":
        # Parse minitest format
        test_names_passed, test_names_failed = _parse_minitest_names(stripped)

    return TestSummary(
        passed=passed,
        failed=failed,
        test_names_passed=test_names_passed,
        test_names_failed=test_names_failed,
        exit_code=exit_code,
        raw_tail=raw_tail,
    )


def _parse_pytest_names(output: str) -> tuple[list[str] | None, list[str] | None]:
    """Parse pytest output for test identifiers.

    Looks for lines like:
      PASSED tests/x.py::test_y
      FAILED tests/x.py::test_z
    or from --collect-only:
      tests/x.py::test_y
    """
    passed = []
    failed = []

    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("PASSED "):
            name = line[7:].strip()
            if name:
                passed.append(name)
        elif line.startswith("FAILED "):
            name = line[7:].strip()
            if name:
                failed.append(name)
        elif line.startswith("ERROR "):
            # a collection error names the file, not a test: the whole file failed
            name = line[6:].split(" - ", 1)[0].strip()
            if name and name not in failed:
                failed.append(name)
        elif "::" in line and not line.startswith(" "):
            # Assume --collect-only format
            name = line.strip()
            if name and not name.startswith(("PASSED", "FAILED", "ERROR")):
                passed.append(name)

    return (passed if passed else None, failed if failed else None)


def _parse_rspec_names(output: str) -> tuple[list[str] | None, list[str] | None]:
    """Parse rspec documentation-format output for test identifiers.

    Looks for lines like:
      ✓ should do something
      ✗ should fail
    """
    passed = []
    failed = []

    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("✓ "):
            name = line[2:].strip()
            if name:
                passed.append(name)
        elif line.startswith("✗ "):
            name = line[2:].strip()
            if name:
                failed.append(name)

    return (passed if passed else None, failed if failed else None)


def _parse_jest_names(output: str) -> tuple[list[str] | None, list[str] | None]:
    """Parse jest output for test identifiers.

    Looks for lines like:
      ✓ test name
      ✕ failed test
    or from --listTests output.
    """
    passed = []
    failed = []

    for line in output.split("\n"):
        line = line.strip()
        if line.startswith("✓ "):
            name = line[2:].strip()
            if name:
                passed.append(name)
        elif line.startswith("✕ "):
            name = line[2:].strip()
            if name:
                failed.append(name)

    return (passed if passed else None, failed if failed else None)


def _parse_minitest_names(output: str) -> tuple[list[str] | None, list[str] | None]:
    """Parse minitest output for test identifiers.

    Minitest does not output individual test names easily; return None.
    """
    return (None, None)
