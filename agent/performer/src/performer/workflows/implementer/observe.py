"""Reading a test run, and judging whether its failure is the expected red (#365).

Both were answered by code that only ever worked for one language.

``_parse_pytest_names`` / ``_parse_rspec_names`` / ``_parse_jest_names`` each
matched a format by hand. Measured against every runner's real default output,
one genuine assertion failure each:

    runner     flag      failed  names_failed  names_passed
    pytest     -rA            1             1             2
    rspec      none           1          None          None
    jest       none           1          None          None
    minitest   none           1          None          None

pytest worked because ``with_test_names`` adds ``-rA`` for it and for nothing
else. The invocation flag and the parser are a pair; three of four runners had
neither. With no names, ``baseline.test_names`` is None, ``red_check`` skips its
name branch and falls to counting, ``regressions()`` cannot detect anything, and
a load error -- which reports no counts at all -- is invisible.

That last case is what made website card #106 bounce ten times in a day. The
model correctly wrote a failing spec for a class that did not exist yet; RSpec
said ``0 examples, 0 failures, 1 error occurred outside of examples``; the
harness read that as "the tests did not fail for the right reason". A load error
is the canonical red for a new class in most languages.

So the model reads the output and judges the red, per #364: if a step required
human judgement ten years ago it requires model judgement today. A pair reading
an unfamiliar runner's output does exactly this, and does not consult a table.
Integrity comes from the gates downstream -- green must actually pass, then CI,
then reviewer and security -- and from the loop keeping its shape: #364's
scope rule means the tests that established red cannot be edited while making
them pass.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from typing_extensions import Literal

from performer.workflows.prose import Prose

from performer.test_results import TestSummary

__all__ = [
    "TestObservation",
    "RedJudgement",
    "observe_persona",
    "judge_persona",
    "observe_tests",
    "judge_red",
]


class TestObservation(BaseModel):
    """What a test run reported, as read by the model."""

    model_config = {"extra": "forbid"}

    #: What happened overall. ``could_not_run`` covers a missing runner, an
    #: unusable environment or a suite that never started -- an environment
    #: problem rather than a statement about the code.
    outcome: Literal[
        "all_passed",
        "assertion_failure",
        "load_error",
        "no_tests_ran",
        "could_not_run",
        "unreadable",
    ]
    failed: list[str] = Field(default_factory=list)
    passed: list[str] = Field(default_factory=list)
    #: Files the runner could not load or collect, with the reason it gave.
    load_errors: list[str] = Field(default_factory=list)
    #: Set when ``outcome`` is ``could_not_run``: what is wrong with the box.
    # 383: clamped, not rejected. A card was blocked because this ran long.
    environment_problem: Prose(300) = ""
    summary: Prose(500) = ""

    def to_summary(self, exit_code: int, raw_tail: str) -> TestSummary:
        """The shape the rest of the lane already consumes.

        Keeping ``TestSummary`` means ``green_check``, ``regressions`` and the
        resume rule are untouched by this change: only the thing that produces
        it changes, from a per-runner parser to the model.

        The projection is lossy -- ``outcome``, ``load_errors`` and
        ``environment_problem`` have nowhere to go -- so the observation rides
        along on the summary. ``judge_red`` needs the outcome, and a load error
        flattens to the same counts as a suite that selected nothing: inferring
        it back is how card #106 was misread in the first place.
        """
        return TestSummary(
            passed=self.outcome == "all_passed",
            failed=len(self.failed) if self.failed else (None if self.outcome in {"all_passed", "no_tests_ran"} else 1),
            test_names_passed=list(self.passed) or None,
            test_names_failed=list(self.failed) or None,
            exit_code=exit_code,
            raw_tail=raw_tail,
            observation=self,
        )


class RedJudgement(BaseModel):
    """Whether the failure observed is the red this milestone expected."""

    model_config = {"extra": "forbid"}

    is_expected_red: bool
    #: Why, in terms of what the runner actually reported.
    #: 383: clamped. The verdict is carried by is_expected_red, not by this.
    reason: Prose(500)
    #: Only meaningful when ``is_expected_red`` is false: what should happen
    #: next. The old code had exactly one remedy and assumed the test was at
    #: fault, which on card #106 was the one thing that was not wrong.
    next_action: Literal["write_the_code", "rewrite_the_test", "fix_something_else", "environment_is_broken"] = "rewrite_the_test"


def observe_persona() -> str:
    """Read a test run. Names no runner and no language."""
    return (
        "You are reading the raw output of a test run in an unfamiliar "
        "repository. Report what the runner actually said.\n\n"
        "Identify each test that failed and each that passed, using whatever "
        "identifiers this runner prints. If a test file could not be loaded or "
        "collected at all, record it in load_errors with the reason given -- "
        "that is a different thing from a test that ran and failed, and in most "
        "languages it is what you see when a test names a class that does not "
        "exist yet.\n\n"
        "Choose outcome carefully:\n"
        "- all_passed: everything ran and passed\n"
        "- assertion_failure: tests ran, at least one failed on its expectation\n"
        "- load_error: a test file could not be loaded or collected\n"
        "- no_tests_ran: the runner ran but selected nothing\n"
        "- could_not_run: the runner itself could not execute, or the "
        "environment is broken (missing binary, unreachable service, no disk). "
        "Say what is wrong in environment_problem.\n"
        "- unreadable: you cannot tell what this output means. Use it rather "
        "than guessing; a wrong reading here is worse than an honest one.\n\n"
        "Report only what the output supports. Do not infer a passing test you "
        "cannot see.\n\n"
        "Keep summary under 500 characters and environment_problem under 300."
    )


def judge_persona() -> str:
    """Judge the red. Deliberately not told what verdict would be convenient."""
    return (
        "You are pair-programming, working test-first. Your partner has just "
        "written a failing test for the milestone below and run the suite.\n\n"
        "Decide one thing: is this the failure you expected to see?\n\n"
        "It is the expected red when the new tests fail because the behaviour "
        "is not implemented yet. In most languages that includes the test file "
        "failing to load because the class or module it names does not exist -- "
        "the code has not been written, which is the whole point of running the "
        "test first.\n\n"
        "Some tests may have been already failing before your partner started. "
        "Those are the repository's problem, not evidence about this milestone: "
        "do not let them decide your verdict, and do not send your partner to "
        "fix them. Judge only what the new tests did.\n\n"
        "It is NOT the expected red when the new tests pass without any "
        "implementation (the test asserts nothing useful), when the failure is "
        "in unrelated pre-existing tests, when the test itself is broken in a "
        "way unrelated to the missing behaviour, or when the suite could not "
        "run at all.\n\n"
        "Keep reason under 500 characters.\n\n"
        "If it is not the expected red, say what should happen next: rewrite "
        "the test, fix something else first, or stop because the environment is "
        "broken. Saying 'write the code' when red was not genuinely observed "
        "produces a test that never tested anything."
    )


async def observe_tests(
    toolkit: Any, command: str, output: str, exit_code: int | None, files: list[str]
) -> TestObservation:
    """Read one test run with the model."""
    from performer.workflows.budget import Budget

    listing = "\n".join(f"- {p}" for p in files[:100]) or "(none named)"
    content = [{
        "type": "text",
        "text": (
            f"Command: {command}\nExit code: {exit_code}\n\n"
            f"Test files changed in this step:\n{listing}\n\n"
            f"Raw output:\n{output[-40000:]}\n\n"
            "Return the observation JSON."
        ),
    }]
    return await toolkit.call_model(
        persona=observe_persona(), schema=TestObservation, content=content,
        budget=Budget.for_step("implementer_observe"),
    )


async def judge_red(
    toolkit: Any,
    milestone_goal: str,
    observation: TestObservation,
    changed_test_files: list[str],
    already_failing: list[str] | None = None,
) -> RedJudgement:
    """Judge whether the observed failure is the expected red.

    387: ``already_failing`` is what the baseline recorded as failing BEFORE the
    card began. Without it this asks a question with the answer withheld -- on
    the measured website run the judge was shown sixteen failures, every one of
    them pre-existing, and correctly concluded the failures were unrelated to
    the milestone. It was reasoning properly from an incomplete picture.
    """
    from performer.workflows.budget import Budget

    changed = "\n".join(f"- {p}" for p in changed_test_files[:100]) or "(no test file changed)"
    prior = list(already_failing or [])
    prior_block = (
        "\n".join(f"- {p}" for p in prior[:60])
        if prior else "(nothing was failing before your partner started)"
    )
    content = [{
        "type": "text",
        "text": (
            f"Milestone: {milestone_goal}\n\n"
            f"Test files your partner just wrote or changed:\n{changed}\n\n"
            f"What the run reported:\n"
            f"  outcome: {observation.outcome}\n"
            f"  failed: {observation.failed[:40]}\n"
            f"  passed: {len(observation.passed)} test(s)\n"
            f"  load errors: {observation.load_errors[:20]}\n"
            f"  environment problem: {observation.environment_problem or '(none)'}\n"
            f"  summary: {observation.summary}\n\n"
            f"Tests that were ALREADY FAILING before your partner started "
            f"({len(prior)} total):\n{prior_block}\n\n"
            "Return the judgement JSON."
        ),
    }]
    return await toolkit.call_model(
        persona=judge_persona(), schema=RedJudgement, content=content,
        budget=Budget.for_step("implementer_judge_red"),
    )
