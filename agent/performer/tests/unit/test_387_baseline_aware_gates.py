"""387: the baseline knows what was already failing; tell the gates.

Measured 2026-09-12: the performer's Selenium driver was broken (#385), so
sixteen website feature specs failed for environmental reasons. judge_red was
shown those sixteen failures and no way to know every one of them predated the
card, so it correctly refused the red -- and four cards blocked on a condition
they did not create.

Two consumers need the baseline's failure record and never got it: the red
judgement, and the local gate, which demanded absolute green and therefore
could never pass on a repository with a single pre-existing failing test.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.workflows.implementer.baseline import new_failures
from performer.workflows.implementer.models import Baseline
from performer.workflows.implementer.observe import TestObservation


def _baseline(failed=None, passed=None, fail_count=0):
    return Baseline(
        test_names=passed, test_names_failed=failed,
        pass_count=len(passed) if passed else 0,
        fail_count=fail_count if failed is None else len(failed),
        stack="", detected_from="test",
    )


def _summary(failed=(), passed=()):
    outcome = "assertion_failure" if failed else "all_passed"
    return TestObservation(outcome=outcome, failed=list(failed), passed=list(passed)).to_summary(
        1 if failed else 0, "")


# --- new_failures: did THIS card break anything? -----------------------------

def test_a_failure_that_predates_the_card_is_not_new():
    base = _baseline(failed=["spec/features/site_navigation_spec.rb:67"])
    now = _summary(failed=["spec/features/site_navigation_spec.rb:67"])
    assert new_failures(base, now) == []


def test_a_failure_the_card_introduced_is_new():
    base = _baseline(failed=["spec/features/site_navigation_spec.rb:67"])
    now = _summary(failed=["spec/features/site_navigation_spec.rb:67", "spec/models/post_spec.rb:12"])
    assert new_failures(base, now) == ["spec/models/post_spec.rb:12"]


def test_the_whole_measured_set_of_sixteen_is_not_new():
    """The exact situation: every failure predates the card."""
    sixteen = [f"spec/features/f_spec.rb:{i}" for i in range(16)]
    assert new_failures(_baseline(failed=sixteen), _summary(failed=sixteen)) == []


def test_a_green_run_has_no_new_failures():
    assert new_failures(_baseline(failed=["a"]), _summary(passed=["x"])) == []


def test_counts_are_used_when_the_runner_prints_no_names():
    """rspec, jest and minitest print no per-test names by default, so the
    comparison has to fall back to counts or it silently passes everything."""
    base = _baseline(failed=None, fail_count=16)
    assert new_failures(base, _named_count(20)) != [], "20 failures against a baseline of 16 is a regression"
    assert new_failures(base, _named_count(16)) == [], "the same count is not a regression"
    assert new_failures(base, _named_count(3)) == [], "fewer failures is not a regression"


def _named_count(n: int):
    """A summary carrying only a failure COUNT, as a nameless runner reports."""
    from performer.test_results import TestSummary
    return TestSummary(passed=False, failed=n, exit_code=1, raw_tail="")


def test_an_unknown_baseline_never_excuses_a_failure():
    """Fail closed: with nothing recorded, a failure cannot be shown to be
    pre-existing, so it counts as new."""
    empty = Baseline(test_names=None, test_names_failed=None, pass_count=0,
                     fail_count=0, stack="", detected_from="test")
    assert new_failures(empty, _summary(failed=["spec/a_spec.rb:1"])) == ["spec/a_spec.rb:1"]


# --- the local gate ----------------------------------------------------------

def _gate_ctx(tmp_path, baseline, failed):
    from performer.workflows.implementer import ImplementerWorkflow, driver

    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        return SimpleNamespace(exit_code=1 if failed else 0, output_excerpt="out")

    async def call_model(*, persona, schema, content, budget):
        return TestObservation(
            outcome="assertion_failure" if failed else "all_passed",
            failed=list(failed), passed=[] if failed else ["ok"])

    ctx = driver.RunContext(
        toolkit=SimpleNamespace(run_command=run_command, call_model=call_model, agent_turn=AsyncMock()),
        stand=SimpleNamespace(path=str(tmp_path)),
        score=SimpleNamespace(owner_repo=("org", "repo"), effective_github_token="t", branch_name="b"),
        budgets=SimpleNamespace(), runner_kind="", test_command="bundle exec rspec",
        baseline=baseline,
    )
    ImplementerWorkflow._default_edges(ctx)
    return ctx


@pytest.mark.asyncio
async def test_the_gate_passes_when_the_only_failures_predate_the_card(tmp_path):
    """A repository with a pre-existing failing test could never ship a card:
    the gate asserted absolute green, which no card can achieve and none caused.
    """
    sixteen = [f"spec/features/f_spec.rb:{i}" for i in range(16)]
    ctx = _gate_ctx(tmp_path, _baseline(failed=sixteen), failed=sixteen)
    verdict, detail = await ctx.local_gate()
    assert verdict == "pass", f"a card was blocked by failures it did not cause: {detail!r}"
    assert "already failing" in detail


@pytest.mark.asyncio
async def test_the_gate_still_fails_on_a_regression_the_card_caused(tmp_path):
    """The half that must not weaken."""
    ctx = _gate_ctx(tmp_path, _baseline(failed=["spec/old_spec.rb:1"]),
                    failed=["spec/old_spec.rb:1", "spec/new_spec.rb:9"])
    verdict, detail = await ctx.local_gate()
    assert verdict == "fail", "a regression this card introduced passed the gate"
    assert "spec/new_spec.rb:9" in detail


@pytest.mark.asyncio
async def test_a_fully_green_run_still_passes(tmp_path):
    ctx = _gate_ctx(tmp_path, _baseline(), failed=[])
    verdict, _ = await ctx.local_gate()
    assert verdict == "pass"


# --- the red judge -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_judge_is_told_what_was_already_failing():
    """It refused the red because it was shown sixteen failures and no way to
    know they predated the card. That is a question asked with the answer
    withheld."""
    from performer.workflows.implementer.observe import RedJudgement, judge_red

    seen = {}

    async def call_model(*, persona, schema, content, budget):
        seen["text"] = "".join(c.get("text", "") for c in content)
        seen["persona"] = persona
        return RedJudgement(is_expected_red=True, reason="ok")

    obs = TestObservation(outcome="assertion_failure", failed=["spec/old_spec.rb:1", "spec/new_spec.rb:2"])
    await judge_red(SimpleNamespace(call_model=call_model), "add a thing", obs,
                    ["spec/new_spec.rb"], already_failing=["spec/old_spec.rb:1"])

    assert "spec/old_spec.rb:1" in seen["text"]
    assert "already failing" in seen["text"].lower() or "before" in seen["text"].lower()


@pytest.mark.asyncio
async def test_the_judge_persona_says_what_to_do_with_them():
    from performer.workflows.implementer.observe import judge_persona

    p = judge_persona().lower()
    assert "already failing" in p or "before your partner" in p


@pytest.mark.asyncio
async def test_the_driver_actually_hands_the_baseline_to_the_judge(tmp_path):
    """Mutation Q6: setting already_failing=None at the call site passed every
    test above, because they exercised judge_red directly and never the wiring.
    A judge that is told nothing is exactly the bug this issue is about, so the
    wiring needs its own assertion.
    """
    from performer.workflows.implementer import driver
    from performer.workflows.implementer.observe import RedJudgement

    captured = {}

    async def fake_judge(toolkit, goal, observation, files, already_failing=None):
        captured["already_failing"] = already_failing
        return RedJudgement(is_expected_red=True, reason="ok")

    # judge_red is imported inside _red_observed, so the patch has to land on
    # the module it is imported FROM, not on the driver.
    import performer.workflows.implementer.observe as obs_mod
    orig = obs_mod.judge_red
    obs_mod.judge_red = fake_judge
    try:
        ctx = driver.RunContext(
            toolkit=SimpleNamespace(), stand=SimpleNamespace(path=str(tmp_path)),
            score=SimpleNamespace(), budgets=SimpleNamespace(), runner_kind="",
            test_command="x",
            baseline=_baseline(failed=["spec/features/site_navigation_spec.rb:67"]),
        )
        obs = TestObservation(outcome="assertion_failure", failed=["spec/new_spec.rb:1"])
        summary = obs.to_summary(1, "")
        await driver._red_observed(
            ctx, SimpleNamespace(index=0, goal="g"), ["spec/new_spec.rb"], summary)
    finally:
        obs_mod.judge_red = orig

    assert captured.get("already_failing") == ["spec/features/site_navigation_spec.rb:67"], (
        "the driver did not hand the baseline's known failures to the judge: "
        f"{captured.get('already_failing')!r}"
    )


# --- the count fallback, which review found could excuse a regression --------

def test_no_baseline_count_recorded_fails_closed():
    """Adversarial review, confirmed critical. The docstring promised a failure
    with nothing recorded at baseline counts as new; the code returned [] and
    passed the gate on the strength of having no evidence at all."""
    from performer.test_results import TestSummary

    nothing_recorded = Baseline(test_names=None, test_names_failed=None, pass_count=0,
                                fail_count=None, stack="", detected_from="test")
    got = new_failures(nothing_recorded, TestSummary(passed=False, failed=5, exit_code=1, raw_tail=""))
    assert got, "a failure was excused by a baseline that recorded nothing"


def test_a_swapped_set_of_named_failures_is_all_new():
    """Same count, different tests. The name path must not be fooled by the
    count being equal."""
    base = _baseline(failed=["a", "b", "c"])
    now = _summary(failed=["x", "y", "z"])
    assert new_failures(base, now) == ["x", "y", "z"]


def test_counts_alone_cannot_verify_and_the_caller_is_told_so():
    """The honest limit. A card that fixes two tests and breaks two different
    ones leaves the count unchanged, and counts cannot see that. Treating
    equality as a regression would block every card on the very repositories
    this gate exists to unblock, so the answer is to SAY the comparison was
    weak rather than to pretend it was not."""
    from performer.test_results import TestSummary
    from performer.workflows.implementer.baseline import name_based_comparison

    count_only = TestSummary(passed=False, failed=5, exit_code=1, raw_tail="")
    assert name_based_comparison(_baseline(failed=None, fail_count=5), count_only) is False

    named = _summary(failed=["a"])
    assert name_based_comparison(_baseline(failed=["a"]), named) is True


@pytest.mark.asyncio
async def test_the_gate_admits_when_it_could_only_compare_counts(tmp_path):
    """A pass that says "none are new" when it only compared numbers is a
    claim the evidence does not support."""
    from performer.workflows.implementer import ImplementerWorkflow, driver

    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        return SimpleNamespace(exit_code=1, output_excerpt="5 failures")

    async def call_model(*, persona, schema, content, budget):
        # a runner that prints counts but no per-test names
        return TestObservation(outcome="assertion_failure", failed=[], passed=[])

    ctx = driver.RunContext(
        toolkit=SimpleNamespace(run_command=run_command, call_model=call_model, agent_turn=AsyncMock()),
        stand=SimpleNamespace(path=str(tmp_path)),
        score=SimpleNamespace(owner_repo=("o", "r"), effective_github_token="t", branch_name="b"),
        budgets=SimpleNamespace(), runner_kind="", test_command="x",
        baseline=_baseline(failed=None, fail_count=5),
    )
    ImplementerWorkflow._default_edges(ctx)
    verdict, detail = await ctx.local_gate()
    assert verdict == "pass"
    assert "COUNT only" in detail, f"the gate overclaimed what it verified: {detail!r}"
