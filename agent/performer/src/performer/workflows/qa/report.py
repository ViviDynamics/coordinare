"""Step 6: assemble the report and the repair brief (spec 164).

The report keys match what ``main.py``'s existing QA post-processing and
coordinare's ``qa_verdict`` evidence floor already consume, so the workflow's
output flows through machinery that exists and is already tested. Nothing here
loosens that floor: ``executed_checks`` carries real exit codes, and a criterion
counted as passed is one the judge bound to evidence.
"""
from __future__ import annotations

from performer.workflows.models import ExecutedCheck
from performer.workflows.qa.models import CriterionVerdict, Finding, VisualDelta


def build_report(
    verdicts: list[CriterionVerdict],
    executed: list[ExecutedCheck],
    delta: VisualDelta,
    findings: list[Finding],
    *,
    passed: bool,
    visual_required: bool,
    visual_evidence: list[dict] | None = None,
    environment_error: str | None = None,
    boot_check: ExecutedCheck | None = None,
    app_start_command: str | None = None,
) -> dict:
    """Produce the QA report dict."""
    report: dict = {
        "passed": passed,
        "criteria_checked": len(verdicts),
        "criteria_passed": sum(1 for v in verdicts if v.passed),
        "executed_checks": [
            {
                "command": c.command,
                "exit_code": c.exit_code,
                "output": c.output_excerpt,
            }
            for c in executed
        ],
        "failures": [
            {
                "criterion": f.criterion,
                "expected": f.expected,
                "actual": f.observed,
                "test": f.plan_check_id or "",
            }
            for f in findings
            if f.category == "unmet_criterion"
        ],
        "verification_steps": _verification_steps(executed),
        "visual_validation_required": visual_required,
        "visual_evidence": list(visual_evidence or []),
    }

    # coordinare's qa_verdict reads this to tell "could not capture" apart from
    # "chose not to". Without it a run whose screenshot failed looks identical
    # to one that never needed a screenshot, and the capture-unavailable
    # handling downstream never fires.
    if visual_required and not (visual_evidence or []):
        report["visual_capture_unavailable"] = True
    # 088 FR-005: visual criteria need boot proof, and the floor requires the
    # boot command to appear in executed_checks with exit 0 -- a claim that
    # references nothing is the self-report spec 083 refuses. The workflow boots
    # the app itself, so it can supply real proof instead of asserting one.
    # Absent when no boot happened: never fabricate it.
    if boot_check is not None:
        report["app_boot_check"] = {
            "command": boot_check.command,
            "exit_code": boot_check.exit_code,
        }

    # The fallback capture (post-processing) boots the app with the same
    # command the shape reading produced — override → shape → inference —
    # instead of guessing from the framework heuristics again (411 review).
    if app_start_command:
        report["app_start_command"] = app_start_command

    if environment_error:
        # The honest "could not verify". Distinct from a failure, and the
        # evidence floor treats it as such.
        report["environment_error"] = environment_error
    return report


def _verification_steps(executed: list[ExecutedCheck]) -> list[str]:
    """Steps a human can follow. Derived from what actually ran, so they cannot
    describe a check that was never executed."""
    steps = [f"Run: {c.command}" for c in executed]
    return steps or ["No checks executed; see environment_error."]


def findings_payload(findings: list[Finding]) -> list[dict]:
    """The repair brief, in scanner_findings-compatible shape."""
    return [f.model_dump(exclude_none=False) for f in findings]
