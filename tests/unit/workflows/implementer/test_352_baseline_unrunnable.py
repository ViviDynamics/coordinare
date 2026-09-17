"""An unrunnable test command is an environment failure (issue #352).

``capture_baseline`` detected a test command, ran it, and built a Baseline out
of whatever came back -- without ever asking whether the command was runnable.
When ci_detection correctly detected ``bundle exec rspec`` on a Rails repo but
``bundle`` was not on PATH, the run exited 127 and the baseline came back
empty: no names, 0 passed, 0 failed. That is indistinguishable from a
repository with no tests yet, so the TDD lane proceeded, read the shell's own
"command not found" as "the tests did not fail for the right reason", spent a
full REPAIR_TESTS cycle on it, and returned partial_progress -- every turn,
identically, forever.

``NoTestRunner`` already routes to ``env_blocked``, which holds the card and
pages an operator. These tests pin exit 127 onto that path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from performer.workflows.implementer.baseline import NoTestRunner, capture_baseline
from performer.workflows.implementer.models import Baseline


def _observation_for(exit_code: int, output: str):
    """Stand in for the model reading the runner output (#365).

    Mirrors what a model would say about these fixtures' output so the
    assertions under test -- which are about baselines and env holds, not about
    how the output was read -- keep their original meaning.
    """
    from performer.workflows.implementer.observe import TestObservation

    if exit_code == 0:
        return TestObservation(outcome="all_passed", passed=["example"])
    return TestObservation(outcome="assertion_failure", failed=["example"], summary=output[:200])



def _toolkit(*, exit_code: int, output: str) -> MagicMock:
    toolkit = MagicMock()
    toolkit.run_command = AsyncMock(
        return_value=MagicMock(output_excerpt=output, exit_code=exit_code),
    )
    toolkit.call_model = AsyncMock(return_value=_observation_for(exit_code, output))
    return toolkit


def _score(command: str = "bundle exec rspec") -> MagicMock:
    score = MagicMock()
    score.local_test_gate_config = {"command": command}
    return score


class TestUnrunnableRunnerIsEnvBlocked:
    @pytest.mark.asyncio
    async def test_exit_127_raises_no_test_runner(self, tmp_path: Path) -> None:
        toolkit = _toolkit(exit_code=127, output="/bin/sh: 1: bundle: not found")

        with pytest.raises(NoTestRunner) as excinfo:
            await capture_baseline(toolkit, _score(), tmp_path)

        # The message has to name the command and say what happened, or the
        # operator gets a hold with no way to act on it.
        message = str(excinfo.value)
        assert "bundle exec rspec" in message
        assert "127" in message
        assert "bundle: not found" in message

    @pytest.mark.asyncio
    async def test_the_check_runs_before_any_baseline_is_built(
        self, tmp_path: Path,
    ) -> None:
        """Failing closed is the point: no empty Baseline may escape."""
        toolkit = _toolkit(exit_code=127, output="sh: rspec: not found")

        with pytest.raises(NoTestRunner):
            result = await capture_baseline(toolkit, _score("rspec"), tmp_path)
            assert not isinstance(result, Baseline)  # pragma: no cover


class TestRealTestOutcomesAreUntouched:
    """127 is the only code that means "not installed" -- nothing else moves."""

    @pytest.mark.asyncio
    async def test_passing_suite_still_builds_a_baseline(self, tmp_path: Path) -> None:
        toolkit = _toolkit(exit_code=0, output="3 examples, 0 failures\n")
        baseline = await capture_baseline(toolkit, _score(), tmp_path)
        assert isinstance(baseline, Baseline)

    @pytest.mark.asyncio
    async def test_failing_suite_still_builds_a_baseline(self, tmp_path: Path) -> None:
        """A red baseline is a real signal, not an environment problem."""
        toolkit = _toolkit(exit_code=1, output="3 examples, 1 failure\n")
        baseline = await capture_baseline(toolkit, _score(), tmp_path)
        assert isinstance(baseline, Baseline)
        assert baseline.fail_count >= 0

    @pytest.mark.asyncio
    async def test_exit_126_is_not_treated_as_missing(self, tmp_path: Path) -> None:
        """126 is "found but not executable" -- a different problem.

        Pinned so the guard is not loosened to `>= 126` or `!= 0`, either of
        which would swallow ordinary red test runs as environment blocks.
        """
        toolkit = _toolkit(exit_code=126, output="permission denied")
        baseline = await capture_baseline(toolkit, _score(), tmp_path)
        assert isinstance(baseline, Baseline)

    @pytest.mark.asyncio
    async def test_exit_1_with_not_found_in_output_is_not_env_blocked(
        self, tmp_path: Path,
    ) -> None:
        """The guard must key on the exit code, not on the words.

        "not found" appears constantly in real test output (RecordNotFound,
        404 Not Found, fixture not found). Matching the substring would mask
        genuine failures as environment blocks -- which is exactly why
        _ENV_FAILURE_SIGNATURES deliberately omits it.
        """
        toolkit = _toolkit(
            exit_code=1,
            output="1 example, 1 failure\n  ActiveRecord::RecordNotFound: not found",
        )
        baseline = await capture_baseline(toolkit, _score(), tmp_path)
        assert isinstance(baseline, Baseline)
