"""T032 / T033 — FR-018 and the evidence-binding rule."""
from __future__ import annotations

from performer.workflows.models import ExecutedCheck, Observation
from performer.workflows.qa.judge import (
    build_findings,
    overall_passed,
    reconcile,
)
from performer.workflows.qa.models import (
    CriterionVerdict,
    JudgeOutput,
    PlanCheck,
    TestPlan,
    VisualDelta,
)

CRIT = "Users can sign in with a workspace selected"
OTHER = "The workspace persists across sessions"


def _plan(*pairs):
    return TestPlan(checks=[PlanCheck(id=i, criterion=c, kind="command", command="x")
                            for i, c in pairs])


def _check(check_id, passed):
    return ExecutedCheck(plan_check_id=check_id, command="pytest", exit_code=0 if passed else 1,
                         passed=passed)


def test_a_criterion_with_no_executed_check_can_never_pass():
    """The evidence-binding rule. The model saying yes is not evidence."""
    plan = _plan(("c1", CRIT))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=True)])

    verdicts = reconcile(model, plan, executed=[], criteria=[CRIT])

    assert verdicts[0].passed is False
    assert "no executed check" in verdicts[0].note


def test_a_failing_check_overrides_a_model_pass():
    plan = _plan(("c1", CRIT))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=True)])

    verdicts = reconcile(model, plan, [_check("c1", passed=False)], [CRIT])

    assert verdicts[0].passed is False


def test_a_model_failure_is_not_overridden_by_passing_checks():
    """Demotion only. The model may have seen what the checks did not, and a
    false failure costs a round while a false pass ships a defect."""
    plan = _plan(("c1", CRIT))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=False)])

    verdicts = reconcile(model, plan, [_check("c1", passed=True)], [CRIT])

    assert verdicts[0].passed is False


def test_a_criterion_passes_only_when_evidence_and_model_agree():
    plan = _plan(("c1", CRIT))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=True)])

    verdicts = reconcile(model, plan, [_check("c1", passed=True)], [CRIT])

    assert verdicts[0].passed is True
    assert verdicts[0].plan_check_ids == ["c1"]


def test_an_unplanned_criterion_is_reported_not_silently_ignored():
    plan = _plan(("c1", CRIT))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=True)])

    verdicts = reconcile(model, plan, [_check("c1", True)], [CRIT, OTHER])

    assert len(verdicts) == 2
    assert next(v for v in verdicts if v.criterion == OTHER).passed is False


# --- FR-018: the regression signal ---

def test_a_removed_element_yields_an_unexpected_regression_finding():
    delta = VisualDelta(removed=[Observation(kind="password_input", position=1, label="Password")])
    findings = build_findings([CriterionVerdict(criterion=CRIT, passed=True)], delta, [], JudgeOutput())

    regressions = [f for f in findings if f.category == "unexpected_regression"]
    assert regressions, "a removed element must be reported"
    assert "password_input" in regressions[0].expected


def test_a_regression_fails_the_run_even_when_every_criterion_passed():
    """Collateral damage is a failure of the change, not of the criteria."""
    verdicts = [CriterionVerdict(criterion=CRIT, passed=True)]
    delta = VisualDelta(removed=[Observation(kind="password_input", position=1)])

    assert overall_passed(verdicts, delta) is False


def test_a_clean_run_passes():
    assert overall_passed([CriterionVerdict(criterion=CRIT, passed=True)], VisualDelta()) is True


def test_no_criteria_is_not_a_pass():
    assert overall_passed([], VisualDelta()) is False


def test_layout_defects_alone_never_fail_a_run():
    """Advisory means advisory. A model's aesthetic opinion is a soft signal,
    and spec 120 exists because soft signals given hard authority is how QA
    goes wrong."""
    delta = VisualDelta(layout_defects=["the heading looks cramped", "colours seem off"])
    assert overall_passed([CriterionVerdict(criterion=CRIT, passed=True)], delta) is True


# --- FR-013 / FR-014: the brief carries evidence and never prescribes ---

def test_a_failure_finding_carries_its_evidence():
    plan = _plan(("c1", CRIT))
    failing = _check("c1", passed=False)
    verdicts = reconcile(JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=False)]),
                         plan, [failing], [CRIT])

    findings = build_findings(verdicts, VisualDelta(), [failing], JudgeOutput())

    unmet = next(f for f in findings if f.category == "unmet_criterion")
    assert unmet.evidence is not None
    assert unmet.evidence["exit_code"] == 1
    assert unmet.repro_command == "pytest"


def test_no_finding_field_can_carry_a_prescribed_fix():
    from performer.workflows.qa.models import Finding

    forbidden = {"fix", "suggested_fix", "remediation", "patch", "solution", "how_to_fix"}
    assert not (forbidden & set(Finding.model_fields)), (
        "FR-014: the brief reports what failed and how to reproduce it. "
        "Prescribing the fix has QA doing the implementer's job with less context."
    )


def test_criterion_matching_tolerates_case_and_whitespace_drift():
    """Round-two review (verified on resume). reconcile() matched criterion
    strings exactly, so a judge that returned 'USERS CAN SIGN IN' for
    'Users can sign in' was treated as having said nothing -- a FALSE FAILURE
    on a criterion whose check passed."""
    plan = _plan(("c1", CRIT))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=f"  {CRIT.upper()} ", passed=True)])

    verdicts = reconcile(model, plan, [_check("c1", passed=True)], [CRIT])

    assert verdicts[0].passed is True, "case/whitespace drift must not fail a demonstrated criterion"


def test_a_criterion_the_judge_omits_entirely_cannot_pass():
    """Silence is not assent. The judge returning verdicts for only some
    criteria leaves the rest failed, however green their checks are."""
    plan = _plan(("c1", CRIT), ("c2", OTHER))
    model = JudgeOutput(criteria=[CriterionVerdict(criterion=CRIT, passed=True)])

    verdicts = reconcile(model, plan, [_check("c1", True), _check("c2", True)], [CRIT, OTHER])

    by = {v.criterion: v for v in verdicts}
    assert by[CRIT].passed is True
    assert by[OTHER].passed is False, "omitted by the judge -> not passed"
