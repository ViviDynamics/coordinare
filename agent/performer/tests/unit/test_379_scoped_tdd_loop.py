"""379: the TDD inner loop runs the milestone's own tests, not the whole suite.

The red and green checks ask a question about one milestone's tests. Running the
whole suite to answer it cost ~20 of 120 minutes on the website card measured in
the issue, against scoped runs of 0.3s and 1.29s in the same session.

Scoping is runner-specific (``pytest path``, ``rspec path``, ``go test ./pkg``),
so the scoped command comes from the model exactly as ``ProjectShape`` supplies
``test_command`` (#367). Coordinare appending paths itself is the hardcoded stack
knowledge #364 forbids.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.workflows.implementer.observe import TestObservation
from performer.workflows.implementer.scoping import ScopedRun, scope_persona, scoped_command


def _real_baseline():
    """The REAL Baseline type, not a SimpleNamespace.

    387 caught this: a stand-in missing test_names_failed passed every test here
    while the production type has always had it, so these tests were asserting
    against a shape the lane never sees.
    """
    from performer.workflows.implementer.models import Baseline

    return Baseline(test_names=None, test_names_failed=None, pass_count=0,
                    fail_count=0, stack="", detected_from="test")


def _toolkit(*, scoped: ScopedRun, exit_code: int = 1, output: str = "1 failure"):
    """A toolkit whose call_model answers the scoping question then observation."""
    calls: list[str] = []

    async def call_model(*, persona, schema, content, budget):
        if schema is ScopedRun:
            calls.append("scope")
            return scoped
        calls.append("observe")
        return TestObservation(outcome="assertion_failure", failed=["a"], summary="one failed")

    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        calls.append(f"run:{cmd}")
        return SimpleNamespace(exit_code=exit_code, output_excerpt=output)

    return SimpleNamespace(call_model=call_model, run_command=run_command, agent_turn=AsyncMock()), calls


# --- the scoping step itself -------------------------------------------------

@pytest.mark.asyncio
async def test_scoped_command_returns_the_model_s_command():
    toolkit, _ = _toolkit(scoped=ScopedRun(can_scope=True, command="bundle exec rspec spec/requests/time_entries_spec.rb"))
    got = await scoped_command(toolkit, "bundle exec rspec", ["spec/requests/time_entries_spec.rb"])
    assert got == "bundle exec rspec spec/requests/time_entries_spec.rb"


@pytest.mark.asyncio
async def test_unscopable_runner_falls_back_to_the_whole_suite():
    """Fail open: an unsure model must not produce a command that selects nothing."""
    toolkit, _ = _toolkit(scoped=ScopedRun(can_scope=False, command="", reason="runner takes no path argument"))
    assert await scoped_command(toolkit, "make test", ["spec/a_spec.rb"]) is None


@pytest.mark.asyncio
async def test_can_scope_false_is_honoured_even_when_a_command_is_offered():
    """The refusal is the answer, not the empty string. A model that says it
    cannot scope while still filling in a plausible-looking command must not
    have that command run: it is the case where the model is least sure, and a
    command that selects nothing reports a false green."""
    toolkit, _ = _toolkit(scoped=ScopedRun(
        can_scope=False, command="bundle exec rspec spec/a_spec.rb", reason="not sure this wrapper forwards paths"))
    assert await scoped_command(toolkit, "make test", ["spec/a_spec.rb"]) is None


@pytest.mark.asyncio
async def test_a_scoped_command_that_drops_the_files_is_refused():
    """A command naming none of the files would silently run the whole suite
    while the caller believed it was scoped -- worse than not scoping."""
    toolkit, _ = _toolkit(scoped=ScopedRun(can_scope=True, command="bundle exec rspec"))
    assert await scoped_command(toolkit, "bundle exec rspec", ["spec/a_spec.rb"]) is None


@pytest.mark.asyncio
async def test_a_command_that_drops_only_SOME_files_is_refused():
    """The dangerous case, and the one a single-file test cannot see.

    A command naming some of the milestone's test files runs those and silently
    skips the rest. The skipped files produce no failures, so green_check sees
    an all-passed run and the milestone is declared green over tests that never
    executed. Every requested file must appear, not merely one.
    """
    toolkit, _ = _toolkit(scoped=ScopedRun(can_scope=True, command="bundle exec rspec spec/a_spec.rb"))
    got = await scoped_command(toolkit, "bundle exec rspec", ["spec/a_spec.rb", "spec/b_spec.rb"])
    assert got is None, f"a command dropping spec/b_spec.rb was accepted: {got!r}"


@pytest.mark.asyncio
async def test_every_requested_file_present_is_accepted():
    toolkit, _ = _toolkit(scoped=ScopedRun(
        can_scope=True, command="bundle exec rspec spec/a_spec.rb spec/b_spec.rb"))
    got = await scoped_command(toolkit, "bundle exec rspec", ["spec/a_spec.rb", "spec/b_spec.rb"])
    assert got == "bundle exec rspec spec/a_spec.rb spec/b_spec.rb"


@pytest.mark.asyncio
async def test_no_files_means_no_model_call():
    toolkit, calls = _toolkit(scoped=ScopedRun(can_scope=True, command="x"))
    assert await scoped_command(toolkit, "pytest", []) is None
    assert calls == []


def test_persona_names_no_runner_and_no_language():
    """#364: the persona must not carry stack knowledge. Any runner named here
    is a table in prose, and the next stack coordinare meets is not on it."""
    persona = scope_persona().lower()
    for runner in ("pytest", "rspec", "jest", "go test", "mix test", "npm", "minitest", "phpunit", "cargo"):
        assert runner not in persona, f"persona names {runner!r}"


# --- the driver seam ---------------------------------------------------------

@pytest.mark.asyncio
async def test_tests_runs_the_scoped_command_not_the_suite(tmp_path):
    from performer.workflows.implementer import driver

    toolkit, calls = _toolkit(scoped=ScopedRun(can_scope=True, command="bundle exec rspec spec/a_spec.rb"))
    ctx = driver.RunContext(
        toolkit=toolkit, stand=SimpleNamespace(path=str(tmp_path)), score=SimpleNamespace(),
        budgets=SimpleNamespace(), runner_kind="", test_command="bundle exec rspec",
        baseline=_real_baseline(),
    )
    await driver._tests(ctx, ["spec/a_spec.rb"], scope=True)
    assert "run:bundle exec rspec spec/a_spec.rb" in calls
    assert "run:bundle exec rspec" not in calls, "the whole suite was run anyway"


@pytest.mark.asyncio
async def test_scoping_is_asked_once_per_file_set_not_once_per_attempt(tmp_path):
    """The green loop runs up to impl_attempts times. A model call per attempt
    would trade suite minutes for gateway round trips."""
    from performer.workflows.implementer import driver

    toolkit, calls = _toolkit(scoped=ScopedRun(can_scope=True, command="bundle exec rspec spec/a_spec.rb"))
    ctx = driver.RunContext(
        toolkit=toolkit, stand=SimpleNamespace(path=str(tmp_path)), score=SimpleNamespace(),
        budgets=SimpleNamespace(), runner_kind="", test_command="bundle exec rspec",
        baseline=_real_baseline(),
    )
    for _ in range(3):
        await driver._tests(ctx, ["spec/a_spec.rb"], scope=True)
    assert calls.count("scope") == 1, f"asked the model {calls.count('scope')} times"


@pytest.mark.asyncio
async def test_unscoped_call_never_asks_the_model_to_scope(tmp_path):
    """The baseline, the chore lane and the quality phase need the whole suite."""
    from performer.workflows.implementer import driver

    toolkit, calls = _toolkit(scoped=ScopedRun(can_scope=True, command="x"))
    ctx = driver.RunContext(
        toolkit=toolkit, stand=SimpleNamespace(path=str(tmp_path)), score=SimpleNamespace(),
        budgets=SimpleNamespace(), runner_kind="", test_command="bundle exec rspec",
        baseline=_real_baseline(),
    )
    await driver._tests(ctx)
    assert calls.count("scope") == 0
    assert "run:bundle exec rspec" in calls


# --- the call sites ----------------------------------------------------------

def _phase_ctx(tmp_path, *, impl_attempts=2, baseline=None):
    from performer.workflows.implementer import driver
    from performer.workflows.implementer.models import Baseline

    return driver.RunContext(
        toolkit=SimpleNamespace(agent_turn=AsyncMock()),
        stand=SimpleNamespace(path=str(tmp_path)), score=SimpleNamespace(title="card"),
        budgets=SimpleNamespace(impl_attempts=impl_attempts, tests_reprompts=0),
        runner_kind="", test_command="bundle exec rspec",
        baseline=baseline or Baseline(test_names=None, pass_count=0, fail_count=0, stack="", detected_from="x"),
    )


def _milestone():
    from performer.workflows.implementer.models import MilestonePlan
    return MilestonePlan(index=0, goal="submit a timesheet", scope=".", done_when="the spec passes",
                         lane="feature", lane_source="default")


def _record():
    from performer.workflows.implementer.models import PerMilestoneRecord
    return PerMilestoneRecord(index=0, goal="submit a timesheet",
                              done_when="the spec passes", implementation_successful=False)


@pytest.mark.asyncio
async def test_green_phase_scopes_every_attempt(tmp_path, monkeypatch):
    """The green check asks 'do the milestone's tests pass now'. That is a
    question about those files, and the measured cost of asking it of the whole
    suite was ~9 minutes an attempt."""
    from performer.workflows.implementer import driver

    seen: list[dict] = []

    async def fake_tests(ctx, files=None, *, scope=False):
        seen.append({"files": files, "scope": scope})
        return TestObservation(outcome="all_passed", passed=["spec/a_spec.rb"]).to_summary(0, "")

    monkeypatch.setattr(driver, "_tests", fake_tests)
    monkeypatch.setattr(driver, "_build_brief", lambda *a, **k: SimpleNamespace(scope_paths=None, milestone_index=0))
    monkeypatch.setattr(driver, "run_turn", AsyncMock(
        return_value=(SimpleNamespace(exit_state="done"), SimpleNamespace(), {"app/a.rb": "modified"})))

    ctx = _phase_ctx(tmp_path)
    red = TestObservation(outcome="assertion_failure", failed=["spec/a_spec.rb"]).to_summary(1, "")
    await driver._green_phase(ctx, _milestone(), _record(), ["spec/a_spec.rb"], red)

    assert seen, "green ran no test check"
    assert all(s["scope"] for s in seen), f"an unscoped whole-suite run in green: {seen}"
    assert all(s["files"] == ["spec/a_spec.rb"] for s in seen)


@pytest.mark.asyncio
async def test_green_loop_does_not_report_a_phantom_regression(tmp_path, monkeypatch):
    """A scoped run reports only the milestone's tests, so comparing its counts
    against a whole-suite baseline invents regressions. Measured: baseline
    fail_count=0 against 5 scoped failures yields
    'test count regression: 0 -> 5', which driver.py then hands the repair
    persona as 'regressed baseline tests'. It sends REPAIR_IMPLEMENT hunting a
    regression that does not exist while the milestone is simply unimplemented.
    """
    from performer.workflows.implementer import driver
    from performer.workflows.implementer.driver import MilestoneFailed

    briefs: list[dict] = []

    async def fake_tests(ctx, files=None, *, scope=False):
        return TestObservation(outcome="assertion_failure",
                               failed=["spec/a_spec.rb:1", "spec/a_spec.rb:2"]).to_summary(1, "raw output")

    def fake_brief(ctx, milestone, **kw):
        briefs.append(kw)
        return SimpleNamespace(scope_paths=None, milestone_index=0)

    monkeypatch.setattr(driver, "_tests", fake_tests)
    monkeypatch.setattr(driver, "_build_brief", fake_brief)
    monkeypatch.setattr(driver, "run_turn", AsyncMock(
        return_value=(SimpleNamespace(exit_state="done"), SimpleNamespace(), {"app/a.rb": "modified"})))

    ctx = _phase_ctx(tmp_path, impl_attempts=2)
    red = TestObservation(outcome="assertion_failure", failed=["spec/a_spec.rb:1"]).to_summary(1, "")
    with pytest.raises(MilestoneFailed):
        await driver._green_phase(ctx, _milestone(), _record(), ["spec/a_spec.rb"], red)

    excerpts = [str(b.get("failure_excerpt") or "") for b in briefs]
    assert not any("regress" in e.lower() for e in excerpts), \
        f"the repair persona was told about a phantom regression: {excerpts}"


@pytest.mark.asyncio
async def test_red_phase_scopes_once_the_test_files_are_known(tmp_path, monkeypatch):
    from performer.workflows.implementer import driver

    seen: list[dict] = []

    async def fake_tests(ctx, files=None, *, scope=False):
        seen.append({"files": files, "scope": scope})
        return TestObservation(outcome="assertion_failure", failed=["spec/a_spec.rb"]).to_summary(1, "")

    monkeypatch.setattr(driver, "_tests", fake_tests)
    monkeypatch.setattr(driver, "_build_brief", lambda *a, **k: SimpleNamespace(scope_paths=None, milestone_index=0))
    monkeypatch.setattr(driver, "run_turn", AsyncMock(
        return_value=(SimpleNamespace(exit_state="done"), SimpleNamespace(), {"spec/a_spec.rb": "added"})))
    monkeypatch.setattr(driver, "_red_observed", AsyncMock(return_value=True))

    ctx = _phase_ctx(tmp_path)
    await driver._red_phase(ctx, _milestone(), _record())

    assert seen and seen[0]["scope"] is True, f"red ran the whole suite: {seen}"
    assert seen[0]["files"] == ["spec/a_spec.rb"]


@pytest.mark.asyncio
async def test_the_local_gate_runs_the_whole_unscoped_suite(tmp_path):
    """Removing regressions() from the inner loop is only safe because the
    whole suite still runs before the PR opens.

    That guarantee lives in the spec-089 local gate, NOT in the quality phase:
    run_quality_phase runs the LINT set, and only reaches run_tests inside
    repair_turn, which is entered only when a quality command fails. On the
    happy path the quality phase runs no tests at all. The local gate runs
    unconditionally and rejects anything that is not a clean whole-suite pass.
    """
    from performer.workflows.implementer import ImplementerWorkflow, driver

    ran: list[str] = []

    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        ran.append(cmd)
        return SimpleNamespace(exit_code=0, output_excerpt="ok")

    async def call_model(*, persona, schema, content, budget):
        if schema is ScopedRun:
            raise AssertionError("the local gate must not scope: it asks a whole-suite question")
        return TestObservation(outcome="all_passed", passed=["spec/a_spec.rb"])

    ctx = driver.RunContext(
        toolkit=SimpleNamespace(run_command=run_command, call_model=call_model, agent_turn=AsyncMock()),
        stand=SimpleNamespace(path=str(tmp_path)),
        score=SimpleNamespace(owner_repo=("org", "repo"), effective_github_token="t", branch_name="b"),
        budgets=SimpleNamespace(), runner_kind="", test_command="bundle exec rspec",
        baseline=_real_baseline(),
    )
    ImplementerWorkflow._default_edges(ctx)
    assert ctx.local_gate is not None
    verdict, _ = await ctx.local_gate()

    assert verdict == "pass"
    assert ran == ["bundle exec rspec"], \
        f"the local gate did not run the whole unscoped suite: {ran}"


@pytest.mark.asyncio
async def test_a_failing_whole_suite_stops_the_local_gate(tmp_path):
    """The other half: a regression a scoped inner loop could not see must
    still block. Without this the gate would be decorative."""
    from performer.workflows.implementer import ImplementerWorkflow, driver

    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        return SimpleNamespace(exit_code=1, output_excerpt="1 failure in an untouched file")

    async def call_model(*, persona, schema, content, budget):
        return TestObservation(outcome="assertion_failure", failed=["spec/unrelated_spec.rb"])

    ctx = driver.RunContext(
        toolkit=SimpleNamespace(run_command=run_command, call_model=call_model, agent_turn=AsyncMock()),
        stand=SimpleNamespace(path=str(tmp_path)),
        score=SimpleNamespace(owner_repo=("org", "repo"), effective_github_token="t", branch_name="b"),
        budgets=SimpleNamespace(), runner_kind="", test_command="bundle exec rspec",
        baseline=_real_baseline(),
    )
    ImplementerWorkflow._default_edges(ctx)
    verdict, detail = await ctx.local_gate()
    assert verdict == "fail", f"a failing whole suite passed the gate: {verdict} {detail!r}"


# --- the issue's open diagnostic --------------------------------------------

@pytest.mark.asyncio
async def test_baseline_log_distinguishes_no_tests_from_no_names(tmp_path, monkeypatch):
    """379 left 'baseline.detected test_count=0' as an open question. It is 0
    in two opposite situations: a repository with no tests, and a suite that
    ran and failed but printed no passed names -- which observe.py records as
    the default for most runners. The log line must say which."""
    from performer.workflows.implementer import baseline as baseline_mod

    events: list[dict] = []
    monkeypatch.setattr(baseline_mod, "log", SimpleNamespace(
        info=lambda event, **kw: events.append({"event": event, **kw})))

    toolkit, _ = _toolkit(scoped=ScopedRun(can_scope=False))
    score = SimpleNamespace(test_command="bundle exec rspec")
    await baseline_mod.capture_baseline(toolkit, score, tmp_path)

    detected = [e for e in events if e.get("event") == "baseline.detected"]
    assert detected, f"no baseline.detected event: {events}"
    assert detected[0]["test_count"] == 0
    assert detected[0]["outcome"] == "assertion_failure", \
        "a suite that ran and failed is indistinguishable from a repo with no tests"
    assert detected[0]["named_tests"] is False


# --- the whole-suite ceiling ------------------------------------------------

def test_test_timeout_is_configurable():
    """379, found live: `bundle exec rspec timed out after 600s and was killed
    (exit code -1); no test results were produced.` -- the website card, blocked
    2026-09-12 04:35 UTC.

    test_timeout_s was a hardcoded dataclass default that nothing ever set, so
    a repository whose suite takes longer than 600s could never complete ANY
    whole-suite run: not the baseline, not the local gate this PR relies on as
    its regression safety net. Scoping the inner loop removes most whole-suite
    runs, but the ones that remain still have to be able to finish.
    """
    from performer.workflows.implementer.budgets import ImplementerBudgets

    assert ImplementerBudgets.from_env(None).test_timeout_s == 600
    assert ImplementerBudgets.from_env({"IMPL_TEST_TIMEOUT_S": "1800"}).test_timeout_s == 1800
    # invalid values fall back rather than disabling the ceiling
    assert ImplementerBudgets.from_env({"IMPL_TEST_TIMEOUT_S": "nonsense"}).test_timeout_s == 600
    assert ImplementerBudgets.from_env({"IMPL_TEST_TIMEOUT_S": "0"}).test_timeout_s == 600


@pytest.mark.asyncio
async def test_the_baseline_uses_the_configured_timeout_not_a_hardcoded_600(tmp_path):
    """capture_baseline hardcoded timeout_s=600 of its own, so raising the
    budget would have fixed the gate and left the baseline still being killed."""
    from performer.workflows.implementer.baseline import capture_baseline

    seen: list[int] = []

    async def run_command(cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        seen.append(timeout_s)
        return SimpleNamespace(exit_code=0, output_excerpt="ok")

    async def call_model(*, persona, schema, content, budget):
        return TestObservation(outcome="all_passed", passed=["a"])

    toolkit = SimpleNamespace(run_command=run_command, call_model=call_model)
    await capture_baseline(toolkit, SimpleNamespace(test_command="bundle exec rspec"),
                           tmp_path, timeout_s=1800)
    assert seen == [1800], f"the baseline ignored the configured timeout: {seen}"
