"""run_tests holds the card when the run cannot be believed (#365).

Three outcomes of reading a test run are not statements about the code, and
each must stop rather than be folded into a verdict:

- ``could_not_run`` — the runner itself could not execute, or the box is broken
- ``unreadable`` — the model cannot tell what the output means
- exit 127 — the shell says the command name does not exist

The first two are the model's judgement, replacing a deliberately incomplete
substring list. The third is a POSIX fact and short-circuits ahead of the model:
there is nothing to read, and #352's env hold depends on seeing it.

These tests exist because a mutation run found all three paths uncovered:
deleting each guard left the whole implementer suite green.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from performer.infrastructure import InfrastructureBlocked
from performer.test_results import TestSummary
from performer.workflows.implementer.baseline import run_tests
from performer.workflows.implementer.driver import _red_observed
from performer.workflows.implementer.observe import RedJudgement, TestObservation


def _toolkit(observation: TestObservation | None, *, exit_code: int = 1, output: str = "boom") -> MagicMock:
    toolkit = MagicMock()
    toolkit.run_command = AsyncMock(
        return_value=MagicMock(output_excerpt=output, exit_code=exit_code)
    )
    toolkit.call_model = AsyncMock(return_value=observation)
    return toolkit


class TestTheRunIsHeldWhenItCannotBeBelieved:
    @pytest.mark.asyncio
    async def test_could_not_run_holds_the_card(self, tmp_path: Path) -> None:
        toolkit = _toolkit(TestObservation(
            outcome="could_not_run",
            environment_problem="postgres refused the connection",
        ))
        with pytest.raises(InfrastructureBlocked, match="postgres refused"):
            await run_tests(toolkit, "bundle exec rspec", "", tmp_path)

    @pytest.mark.asyncio
    async def test_could_not_run_without_detail_still_holds(self, tmp_path: Path) -> None:
        """An empty reason must not turn a hold into a pass."""
        toolkit = _toolkit(TestObservation(outcome="could_not_run"))
        with pytest.raises(InfrastructureBlocked):
            await run_tests(toolkit, "go test ./...", "", tmp_path)

    @pytest.mark.asyncio
    async def test_unreadable_holds_rather_than_guessing(self, tmp_path: Path) -> None:
        """Honest incomprehension beats a confident wrong reading.

        A misread run becomes a verdict about the code; a hold becomes a
        question for an operator. Only one of those is recoverable.
        """
        toolkit = _toolkit(TestObservation(
            outcome="unreadable", summary="output is a progress bar and nothing else",
        ))
        with pytest.raises(InfrastructureBlocked, match="could not read"):
            await run_tests(toolkit, "gradle test", "", tmp_path)

    @pytest.mark.asyncio
    async def test_a_real_failure_is_not_held(self, tmp_path: Path) -> None:
        """The guards must not swallow an ordinary red run."""
        toolkit = _toolkit(TestObservation(
            outcome="assertion_failure", failed=["spec/week_spec.rb:12"],
        ))
        summary = await run_tests(toolkit, "bundle exec rspec", "", tmp_path)
        assert summary.passed is False
        assert summary.test_names_failed == ["spec/week_spec.rb:12"]

    @pytest.mark.asyncio
    async def test_a_passing_run_is_not_held(self, tmp_path: Path) -> None:
        toolkit = _toolkit(TestObservation(outcome="all_passed", passed=["a", "b"]), exit_code=0)
        summary = await run_tests(toolkit, "npx jest", "", tmp_path)
        assert summary.passed is True


class TestExitOneTwentySevenShortCircuits:
    """POSIX 'command not found' is a fact, not a reading."""

    @pytest.mark.asyncio
    async def test_the_model_is_never_asked(self, tmp_path: Path) -> None:
        toolkit = _toolkit(None, exit_code=127, output="/bin/sh: 1: bundle: not found")
        summary = await run_tests(toolkit, "bundle exec rspec", "", tmp_path)

        toolkit.call_model.assert_not_awaited()
        assert summary.exit_code == 127
        assert summary.passed is False

    @pytest.mark.asyncio
    async def test_the_output_is_kept_for_the_hold_message(self, tmp_path: Path) -> None:
        """capture_baseline turns this into the env hold and needs the reason."""
        toolkit = _toolkit(None, exit_code=127, output="/bin/sh: 1: bundle: not found")
        summary = await run_tests(toolkit, "bundle exec rspec", "", tmp_path)
        assert "bundle: not found" in summary.raw_tail

    @pytest.mark.asyncio
    async def test_exit_126_is_read_by_the_model(self, tmp_path: Path) -> None:
        """126 is 'found but not executable' -- a different condition, and one
        the model can say something useful about."""
        toolkit = _toolkit(TestObservation(outcome="could_not_run", environment_problem="permission denied"), exit_code=126)
        with pytest.raises(InfrastructureBlocked):
            await run_tests(toolkit, "./run-tests", "", tmp_path)
        toolkit.call_model.assert_awaited()


class TestTheJudgeGetsTheReadingNotAReconstruction:
    """The reading survives the trip to the judge (#365, found in review).

    ``run_tests`` reads the run into a ``TestObservation``, but the lane passes
    a ``TestSummary``, which has no room for an outcome, a load error or an
    environment problem. An earlier cut of ``_red_observed`` rebuilt the
    observation from the summary's counts. That reconstruction reproduced the
    exact bug this change exists to remove: a load error carries no counts, so
    it flattened into the same summary as a suite that selected nothing and
    came back out as ``no_tests_ran`` -- and the judge persona says a load
    error IS the expected red while a suite that selected nothing is not. Card
    #106 would have been handed the one input that yields the wrong verdict.
    """

    @staticmethod
    def _ctx(judgement: RedJudgement) -> MagicMock:
        ctx = MagicMock()
        ctx.toolkit.call_model = AsyncMock(return_value=judgement)
        return ctx

    @staticmethod
    def _milestone() -> MagicMock:
        return MagicMock(index=0, goal="add Week")

    @pytest.mark.asyncio
    async def test_a_load_error_reaches_the_judge_as_a_load_error(self) -> None:
        """Card #106's red, end to end through the summary."""
        seen: dict = {}

        async def capture(persona, content, **_kw):
            seen["text"] = "".join(c.get("text", "") for c in content)
            return RedJudgement(is_expected_red=True, reason="the class does not exist yet")

        ctx = MagicMock()
        ctx.toolkit.call_model = AsyncMock(side_effect=capture)

        observed = TestObservation(
            outcome="load_error",
            load_errors=["spec/week_spec.rb: uninitialized constant Week"],
            summary="0 examples, 0 failures, 1 error occurred outside of examples",
        )
        summary = observed.to_summary(exit_code=1, raw_tail="1 error occurred outside of examples")

        assert await _red_observed(ctx, self._milestone(), ["spec/week_spec.rb"], summary) is True
        assert "outcome: load_error" in seen["text"], "the judge was told the suite selected nothing"
        assert "uninitialized constant Week" in seen["text"], "the load error's reason was dropped"

    @pytest.mark.asyncio
    async def test_every_outcome_arrives_unchanged(self) -> None:
        """Not just load_error: the summary is never inverted for any of them.

        Pinning the rule rather than the one example -- a reconstruction that
        happened to get load_error right could still corrupt another outcome.
        """
        for outcome, kwargs, exit_code in (
            ("all_passed", {"passed": ["a"]}, 0),
            ("assertion_failure", {"failed": ["spec/week_spec.rb:12"]}, 1),
            ("load_error", {"load_errors": ["spec/week_spec.rb: no constant"]}, 1),
            ("no_tests_ran", {}, 0),
        ):
            seen: dict = {}

            async def capture(persona, content, _s=seen, **_kw):
                _s["text"] = "".join(c.get("text", "") for c in content)
                return RedJudgement(is_expected_red=False, reason="x")

            ctx = MagicMock()
            ctx.toolkit.call_model = AsyncMock(side_effect=capture)
            summary = TestObservation(outcome=outcome, **kwargs).to_summary(exit_code, "tail")
            await _red_observed(ctx, self._milestone(), ["spec/week_spec.rb"], summary)
            assert f"outcome: {outcome}" in seen["text"], f"{outcome} was changed in transit"

    @pytest.mark.asyncio
    async def test_a_summary_nobody_read_is_not_the_expected_red(self) -> None:
        """Exit 127 short-circuits before the model, so it carries no reading.

        A failure nobody could read is not a failure anyone can call the
        expected red, and guessing one back from the counts is the whole
        defect. The judge must not be asked to rule on nothing.
        """
        ctx = MagicMock()
        ctx.toolkit.call_model = AsyncMock()
        summary = TestSummary(passed=False, failed=None, exit_code=127, raw_tail="bundle: not found")

        assert await _red_observed(ctx, self._milestone(), ["spec/week_spec.rb"], summary) is False
        ctx.toolkit.call_model.assert_not_awaited()


class TestEveryRequestedBudgetStepIsRegistered:
    """A step name that is not in the table silently takes the default (#365).

    ``Budget.for_step`` is a ``.get`` with a fallback, so a typo or a name
    nobody registered is not an error -- it is a budget you did not choose.
    Both of this change's steps were requested under names the table did not
    have. This checks the RULE over every call site rather than those two, so
    the next unregistered name fails here instead of in production.
    """

    def test_no_call_site_asks_for_a_step_the_table_lacks(self) -> None:
        import ast

        from performer.workflows.budget import _STEP_BUDGETS

        root = Path(__file__).resolve().parents[4] / "agent" / "performer" / "src" / "performer"
        assert root.is_dir(), root

        requested: list[tuple[str, str]] = []
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "for_step" or not node.args:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    requested.append((arg.value, str(path.relative_to(root))))

        assert requested, "found no for_step call sites -- the scan is broken, not the code"
        missing = [(step, where) for step, where in requested if step not in _STEP_BUDGETS]
        assert not missing, f"step names absent from _STEP_BUDGETS: {missing}"
