"""Step 5: per-criterion verdicts and the before/after delta (spec 164).

Two rules carry the weight here, and both exist because QA's historical failure
is the confident false pass that spec 120 was written to stop:

1. A criterion passes ONLY when an executed check demonstrates it. The model's
   opinion never promotes a criterion on its own; ``reconcile`` overrides the
   model in the safe direction and never in the unsafe one.
2. Anything present before the change and absent after is an unexpected change,
   even when the claimed feature did land. That is the regression signal, and
   the design-session prototype caught a silently deleted password field with
   exactly this comparison.

The visual delta is advisory. Layout defects can add a finding but can never be
the sole cause of a failed criterion — a model's aesthetic opinion is a soft
signal, and spec 120 exists because soft signals given hard authority is how QA
goes wrong.
"""
from __future__ import annotations

from performer.workflows.models import ExecutedCheck
from performer.workflows.qa.models import (
    CriterionVerdict,
    Finding,
    JudgeOutput,
    TestPlan,
    VisualDelta,
    normalise_criterion,
)


def _norm(text: str) -> str:
    """Criterion identity for matching model output back to the plan."""
    return normalise_criterion(text)


def _checks_for(criterion: str, plan: TestPlan, executed: list[ExecutedCheck]) -> list[ExecutedCheck]:
    target = normalise_criterion(criterion)
    ids = {c.id for c in plan.checks if normalise_criterion(c.criterion) == target}
    return [e for e in executed if e.plan_check_id in ids]


def reconcile(
    model_output: JudgeOutput,
    plan: TestPlan,
    executed: list[ExecutedCheck],
    criteria: list[str],
) -> list[CriterionVerdict]:
    """Bind the model's verdicts to evidence, demoting anything unsupported.

    Demotion only. A criterion the model called failed stays failed even if the
    checks passed: the model may have seen something the checks did not, and a
    false failure costs a round while a false pass ships a defect.
    """
    # Keyed on a normalised form: the model reproduces criterion text with case
    # and whitespace drift, and an exact-string lookup turned that drift into a
    # false FAILURE on a criterion whose check had passed.
    by_criterion = {_norm(v.criterion): v for v in model_output.criteria}
    verdicts: list[CriterionVerdict] = []

    for criterion in criteria:
        bound = _checks_for(criterion, plan, executed)
        claimed = by_criterion.get(_norm(criterion))

        if not bound:
            verdicts.append(
                CriterionVerdict(
                    criterion=criterion,
                    passed=False,
                    note="no executed check demonstrates this criterion",
                )
            )
            continue

        evidence_passed = all(c.passed for c in bound)
        model_passed = bool(claimed.passed) if claimed is not None else False
        verdicts.append(
            CriterionVerdict(
                criterion=criterion,
                # Both must agree for a pass. Either alone can fail it.
                passed=evidence_passed and model_passed,
                plan_check_ids=[c.plan_check_id for c in bound if c.plan_check_id],
                note=(claimed.note if claimed is not None else "")
                or ("" if evidence_passed else "an executed check failed"),
            )
        )
    return verdicts


def build_findings(
    verdicts: list[CriterionVerdict],
    delta: VisualDelta,
    executed: list[ExecutedCheck],
    model_output: JudgeOutput,
) -> list[Finding]:
    """Assemble the repair brief. Reports; never prescribes (FR-014)."""
    by_id = {c.plan_check_id: c for c in executed if c.plan_check_id}
    findings: list[Finding] = []

    for verdict in verdicts:
        if verdict.passed:
            continue
        evidence_check = next(
            (by_id[i] for i in verdict.plan_check_ids if i in by_id and not by_id[i].passed),
            None,
        )
        findings.append(
            Finding(
                category="unmet_criterion",
                severity="high",
                criterion=verdict.criterion,
                plan_check_id=evidence_check.plan_check_id if evidence_check else None,
                expected=verdict.criterion,
                observed=verdict.note or "criterion not demonstrated",
                evidence=evidence_check.model_dump() if evidence_check else None,
                repro_command=evidence_check.command if evidence_check else None,
            )
        )

    # Regressions are reported even when every criterion passed: collateral
    # damage is a failure of the change, not of the criteria.
    for removed in delta.removed:
        findings.append(
            Finding(
                category="unexpected_regression",
                severity="high",
                criterion="",
                expected=f"{removed.kind} present (label={removed.label!r})",
                observed=f"{removed.kind} absent after the change",
            )
        )

    for change in model_output.unexpected_changes:
        findings.append(
            Finding(
                category="unexpected_regression",
                severity="medium",
                expected="no change beyond the claimed one",
                observed=change,
            )
        )

    return findings


def overall_passed(
    verdicts: list[CriterionVerdict],
    delta: VisualDelta,
    model_output: JudgeOutput | None = None,
) -> bool:
    """A run passes when every criterion is demonstrated and nothing regressed.

    Layout defects are deliberately absent from this decision: advisory means
    advisory. A judge-reported change beyond the claimed one is NOT advisory
    and is not absent from this decision: build_findings turns it into a hard
    unexpected_regression finding, so the passed flag must latch too — a
    report that says PASSED with a hard finding attached is exactly the
    false-reassurance this module exists to prevent (411 round-eight review).
    """
    if not verdicts:
        return False
    passed = all(v.passed for v in verdicts) and not delta.removed
    if passed and model_output is not None and model_output.unexpected_changes:
        return False
    return passed
