"""Baseline detection and test result parsing for implementer workflow (spec 167).

Detects test command, runs it once to record baseline, and parses results
per stack (pytest, rspec, jest, make). Per-test results when available,
counts otherwise.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from performer.test_results import TestSummary
from performer.workflows.implementer.models import Baseline

log = structlog.get_logger(__name__)

__all__ = ["NoTestRunner", "detect_baseline", "detect_test_command", "run_tests", "capture_baseline", "regressions"]


class NoTestRunner(Exception):
    """Raised when no test command is detected, or the detected one cannot run."""

    pass


# POSIX: the shell exits 127 when the command name cannot be found. Distinct
# from 126 (found but not executable) and from any exit code a test runner
# itself produces, so it is safe to treat as "the runner is not installed".
_COMMAND_NOT_FOUND = 127


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
            return (test_command, "", "score.local_test_gate_config")

    if hasattr(score, "test_command") and score.test_command:
        return (score.test_command, "", "score.test_command")

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
    command = result.test_command

    # ci_detection's stack is a language ("python", "ruby"); the parsers need the
    # RUNNER. A live round parsed no names because "python" matched no parser.
    # 365: no runner_kind. Nothing downstream parses by runner any more.
    return (command, "", "coordinare.ci_detection")


async def run_tests(toolkit, command: str, runner_kind: str, cwd: Path, timeout_s: int = 600, *, files: list[str] | None = None) -> TestSummary:
    """Run the test command and have the model read what it printed (#365).

    ``runner_kind`` is retained in the signature for call-site compatibility and
    is no longer consulted: there is no per-runner parsing left. The model reads
    whatever the runner emitted, which is what makes this work on a stack
    coordinare has never seen.

    An environment problem is the model's call too, not a substring list. The
    old ``_ENV_FAILURE_SIGNATURES`` was deliberately incomplete -- its own
    comment says so -- because a list cannot tell ``RecordNotFound`` from a
    missing test runner. A model reading the output can.
    """
    from performer.infrastructure import InfrastructureBlocked
    from performer.workflows.implementer.observe import observe_tests

    run_result = await toolkit.run_command(command, cwd=cwd, timeout_s=timeout_s)
    output = run_result.output_excerpt or ""
    exit_code = run_result.exit_code

    # 352: exit 127 is the shell saying the command name does not exist. That is
    # a POSIX fact rather than a judgement, so it short-circuits ahead of the
    # model -- there is nothing to read, and capture_baseline turns it into the
    # env_blocked hold.
    if exit_code == _COMMAND_NOT_FOUND:
        return TestSummary(passed=False, failed=None, exit_code=exit_code, raw_tail=output[-2000:])

    observation = await observe_tests(toolkit, command, output, exit_code, list(files or []))
    if observation.outcome == "could_not_run":
        raise InfrastructureBlocked(observation.environment_problem or "the test runner could not execute")
    if observation.outcome == "unreadable":
        raise InfrastructureBlocked(
            "could not read the test runner's output: " + (observation.summary or "no interpretation possible")
        )
    return observation.to_summary(exit_code, output[-2000:])


async def capture_baseline(toolkit, score, workspace: Path, *, timeout_s: int = 600) -> Baseline:
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

    parsed = await run_tests(toolkit, test_command, stack, workspace, timeout_s=timeout_s)

    # 352: a detected command that cannot execute is an environment failure,
    # not a repository without tests. Exit 127 is the shell's unambiguous
    # "command not found". Without this the baseline comes back empty
    # (no names, 0 passed, 0 failed), which is indistinguishable from a
    # test-free repo, so the TDD lane proceeds, reads the runner's own error
    # as "the tests did not fail for the right reason", and burns a full
    # repair cycle per turn -- indefinitely, since every turn fails
    # identically. NoTestRunner routes to env_blocked, which holds the card
    # and pages an operator instead.
    if parsed.exit_code == _COMMAND_NOT_FOUND:
        raise NoTestRunner(
            f"test command is not executable in this environment: {test_command!r} "
            f"exited {_COMMAND_NOT_FOUND}: {' '.join(parsed.raw_tail.split())[:200]}"
        )

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

    # 379: test_count alone cannot be read. It is 0 for a repository with no
    # tests, for a suite that ran but printed no passed names (the default for
    # most runners, per the table in observe.py), AND for a suite that was
    # killed at the timeout before producing anything. Those want different
    # responses. The third is the one that bit the website card: its suite runs
    # longer than the ceiling, so the run is killed every time -- and it only
    # surfaces as an error when the model happens to read the truncated output
    # as could_not_run. Any other reading flows through here as a silently
    # empty baseline. The runner's own outcome is what tells them apart.
    log.info(
        "baseline.detected",
        test_count=len(test_names) if test_names else baseline.pass_count,
        outcome=getattr(parsed.observation, "outcome", None),
        named_tests=bool(test_names),
        fail_count=fail_count,
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


async def detect_baseline(toolkit, score, *, timeout_s: int = 600) -> Baseline:
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
    return await capture_baseline(toolkit, score, workspace, timeout_s=timeout_s)
