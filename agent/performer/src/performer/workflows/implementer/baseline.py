"""Baseline detection and test result parsing for implementer workflow (spec 167).

Detects test command, runs it once to record baseline, and parses results
per stack (pytest, rspec, jest, make). Per-test results when available,
counts otherwise.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from performer.test_results import parse_test_output, TestSummary
from performer.workflows.implementer.models import Baseline

log = structlog.get_logger(__name__)

__all__ = ["NoTestRunner", "detect_baseline", "detect_test_command", "run_tests", "capture_baseline", "regressions"]


class NoTestRunner(Exception):
    """Raised when no test command is detected before the first turn."""

    pass


def detect_lint_command(score, workspace: Path) -> str | None:
    """The repository's lint command: the gate config when it names one, else
    what ci_detection finds (rubocop, ruff, npm run lint), else None."""
    gate = getattr(score, "local_test_gate", None) or {}
    if isinstance(gate, dict) and gate.get("lint_command"):
        return str(gate["lint_command"])
    try:
        from coordinare_ci_detection import detect
    except ImportError:
        return None
    try:
        result = detect(workspace)
    except Exception:  # noqa: BLE001 - detection is best effort
        return None
    return getattr(result, "lint_command", None) or None


def with_test_names(command: str) -> str:
    """Make the runner print per-test names so red and green can read them:
    pytest gets ``-rA`` when it has no ``-r`` flag; other runners are unchanged."""
    if stack_from_command(command) == "pytest" and " -r" not in f" {command}" and "--report" not in command:
        return command + " -rA"
    return command


KNOWN_RUNNERS = ("pytest", "rspec", "jest", "minitest")


def runner_kind_for(detected_stack: str | None, command: str) -> str:
    """The runner kind the output parsers understand: a detected value only
    when it already names a runner, else what the command implies."""
    if detected_stack in KNOWN_RUNNERS:
        return detected_stack
    return stack_from_command(command)


def stack_from_command(command: str) -> str:
    """The runner kind a test command implies, so its output can be parsed."""
    text = (command or "").lower()
    for needle, stack in (("pytest", "pytest"), ("rspec", "rspec"), ("jest", "jest"), ("vitest", "jest"), ("rake test", "minitest"), ("minitest", "minitest")):
        if needle in text:
            return stack
    return "unknown"


def detect_test_command(score, workspace: Path) -> tuple[str, str, str]:
    """Detect the test command and stack (FR-004).

    Tries in order: score.local_test_gate_config, score.test_command,
    coordinare ci_detection.detect(workspace).

    Args:
        score: The dispatch payload.
        workspace: The workspace path for ci_detection.

    Returns:
        Tuple of (test_command, stack, detected_from) where stack is "pytest", "rspec", "jest", "make", or "unknown",
        and detected_from indicates the source ("score.local_test_gate_config", "score.test_command", etc.).

    Raises:
        NoTestRunner: When no test command is detected.
    """
    test_command = None

    if hasattr(score, "local_test_gate_config") and score.local_test_gate_config:
        test_command = score.local_test_gate_config.get("command")
        if test_command:
            return (with_test_names(test_command), stack_from_command(test_command), "score.local_test_gate_config")

    if hasattr(score, "test_command") and score.test_command:
        return (with_test_names(score.test_command), stack_from_command(score.test_command), "score.test_command")

    try:
        from coordinare_ci_detection import detect
    except ImportError as exc:  # pragma: no cover - packaging failure, not a repo condition
        # 339: this used to import coordinare.services.ci_detection, which the
        # performer image does not ship, so EVERY in-container run took this
        # branch and reported a packaging bug as a repository problem.
        raise NoTestRunner(
            "CI detection package unavailable in this image (coordinare-ci-detection "
            f"is not installed): {exc}"
        ) from exc

    result = detect(workspace)
    if not result or not result.test_command:
        raise NoTestRunner("no test command detected")
    command = with_test_names(result.test_command)

    # ci_detection's stack is a language ("python", "ruby"); the parsers need the
    # RUNNER. A live round parsed no names because "python" matched no parser.
    return (command, runner_kind_for(getattr(result, "stack", None), command), "coordinare.ci_detection")


async def run_tests(toolkit, command: str, runner_kind: str, cwd: Path, timeout_s: int = 600) -> TestSummary:
    """Run the test command and parse results (FR-004).

    Args:
        toolkit: Execution toolkit with run_command method.
        command: Test command to run.
        runner_kind: Stack kind ("pytest", "rspec", "jest", "make", etc.).
        cwd: Working directory for the command.
        timeout_s: Command timeout in seconds.

    Returns:
        TestSummary with parsed test results.
    """
    run_result = await toolkit.run_command(command, cwd=cwd, timeout_s=timeout_s)

    output = run_result.output_excerpt or ""
    exit_code = run_result.exit_code

    from performer.infrastructure import InfrastructureBlocked
    from performer.test_results import _match_env_signature

    if exit_code != 0 and (cause := _match_env_signature(output)):
        raise InfrastructureBlocked(cause)

    parsed = parse_test_output(
        runner_kind=runner_kind if runner_kind != "unknown" else "make",
        output=output,
        exit_code=exit_code,
    )

    return parsed


async def capture_baseline(toolkit, score, workspace: Path) -> Baseline:
    """Run the test command once and capture baseline results (FR-004).

    Args:
        toolkit: Execution toolkit.
        score: The dispatch payload.
        workspace: The workspace path.

    Returns:
        Baseline with test names or counts.

    Raises:
        NoTestRunner: When no test command is detected.
    """
    test_command, stack, detected_from = detect_test_command(score, workspace)

    log.info("baseline.running", command=test_command, stack=stack)

    parsed = await run_tests(toolkit, test_command, stack, workspace, timeout_s=600)

    test_names = parsed.test_names_passed or []
    failed_names = parsed.test_names_failed or []
    fail_count = parsed.failed or 0

    baseline = Baseline(
        test_names=test_names if test_names else None,
        # 171: kept so the resume rule can place a baseline failure in a file
        test_names_failed=failed_names if failed_names else None,
        pass_count=len(test_names) if test_names else (1 if parsed.passed else 0),
        fail_count=fail_count,
        stack=stack,
        detected_from=detected_from,
    )

    log.info(
        "baseline.detected",
        test_count=len(test_names) if test_names else baseline.pass_count,
        stack=stack,
    )

    return baseline


def regressions(baseline: Baseline, summary: TestSummary) -> list[str]:
    """Detect test regressions (baseline tests that now fail) (FR-006).

    Args:
        baseline: The baseline test results.
        summary: Current test results.

    Returns:
        List of test names that passed in baseline but fail now, or empty list.
    """
    if not baseline.test_names or not summary.test_names_failed:
        if baseline.test_names and summary.test_names_passed is None:
            return []
        if baseline.pass_count is not None and summary.failed is not None:
            if summary.failed > baseline.fail_count:
                return [f"test count regression: {baseline.fail_count} -> {summary.failed}"]
        return []

    regressed = []
    for test_name in baseline.test_names:
        if summary.test_names_failed and test_name in summary.test_names_failed:
            regressed.append(test_name)

    return regressed


async def detect_baseline(toolkit, score) -> Baseline:
    """Detect and run the test command once to record baseline (FR-004).

    Wrapper around detect_test_command and capture_baseline.

    Args:
        toolkit: The execution toolkit with run_command method.
        score: The dispatch payload carrying workspace and test command detection.

    Returns:
        Baseline with test names or pass/fail counts.

    Raises:
        NoTestRunner: When no test command is detected (env_blocked).
    """
    workspace = Path(score.workspace_path) if hasattr(score, "workspace_path") else Path.cwd()
    return await capture_baseline(toolkit, score, workspace)
