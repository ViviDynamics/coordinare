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
    r"|^FAILED\s+\S+::"  # pytest per-test failure line
    r"|^--- FAIL: "  # go test per-test failure line
    r"|test result: FAILED"  # cargo test summary
    r"|^\s*Failed!"  # xUnit summary header
    r"|[1-9]\d*\s+failed\s*\|",  # "1 failed | 6 passed" (cargo, rspec-ish)
    re.IGNORECASE | re.MULTILINE,
)


#: ANSI/CSI escape sequences that color output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


#: Failure count patterns. Anchored the same way _TEST_FAILURE_MARKERS is.
_FAILURE_COUNT_PATTERNS = (
    re.compile(r"([1-9]\d*)\s+failed(?:,|;|\s+in\s)", re.IGNORECASE),
    re.compile(r"^\s*([1-9]\d*)\s+failing\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r",\s*([1-9]\d*)\s+failures?\b", re.IGNORECASE),
    re.compile(r"failures:\s*([1-9]\d*)", re.IGNORECASE),
    re.compile(r"-\s*Failed:\s*([1-9]\d*)", re.IGNORECASE),  # xUnit "- Failed: 3"
    re.compile(r"([1-9]\d*)\s+failed\s*\|", re.IGNORECASE),  # "1 failed | 6 passed"
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
        observation: The model's reading of this run, when there was one
            (``workflows.implementer.observe.TestObservation``). Untyped here
            to keep this low-level module free of a workflow import. This
            summary is a lossy projection -- it has no room for an outcome, a
            load error or an environment problem -- so anything that needs the
            full reading must take it from here rather than infer it back from
            the counts. None when nothing was read: the exit-127 short circuit,
            or a summary built directly by a test.
    """

    passed: bool
    failed: int | None = None
    test_names_passed: list[str] | None = None
    test_names_failed: list[str] | None = None
    exit_code: int | None = None
    raw_tail: str = ""
    observation: object | None = None


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
    from performer.infrastructure import infrastructure_reason

    infra = infrastructure_reason(output)
    if infra:
        return infra
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


