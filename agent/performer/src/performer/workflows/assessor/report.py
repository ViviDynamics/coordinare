"""Assessor report step (spec 166 FR-003, FR-011): proves write-free and packages the result.

Builds the performer response carrying the assessment, gate record, write-free
proof, and workflow metrics. The write-free proof is structural: the Toolkit
has no command runner, no screenshot_capture, no dom_reader, and metrics show
zero commands run.
"""
from __future__ import annotations

from performer.workflows.assessor.models import Assessment, GateRecord


class AssessorHadCommandRunner(Exception):
    """The assessor workflow is write-free; toolkit must not have write capabilities."""
    pass


def build_report(assessment: Assessment, record: GateRecord, toolkit) -> dict:
    """Build the performer response report.

    Packages the assessment, gate record, write-free proof, and metrics.
    Raises AssessorHadCommandRunner if any write capability is present (FR-003).

    Args:
        assessment: The final Assessment after gate processing.
        record: The GateRecord documenting what the gate changed.
        toolkit: The execution toolkit (for metrics and write-capability check).

    Returns:
        dict with keys: assessment, gate_record, write_free_check, workflow_metrics.

    Raises:
        AssessorHadCommandRunner: If the toolkit has a command runner, screenshot_capture,
        dom_reader, or if any commands ran (the metrics show commands).
    """
    metrics = toolkit.metrics
    has_command_runner = toolkit._command_runner is not None
    has_screenshot_capture = toolkit._screenshot_capture is not None
    has_dom_reader = toolkit._dom_reader is not None
    commands_run = metrics.commands_run

    if has_command_runner or has_screenshot_capture or has_dom_reader or commands_run > 0:
        raise AssessorHadCommandRunner(
            f"Assessor workflow must be write-free. Command runner present: "
            f"{has_command_runner}, screenshot_capture present: {has_screenshot_capture}, "
            f"dom_reader present: {has_dom_reader}, commands run: {commands_run}"
        )

    return {
        "assessment": assessment.model_dump(),
        "gate_record": record.model_dump(),
        "write_free_check": {
            "commands_run": commands_run,
            "has_command_runner": has_command_runner,
            "has_screenshot_capture": has_screenshot_capture,
            "has_dom_reader": has_dom_reader,
            "passed": True,
        },
        "workflow_metrics": {
            "step_durations_ms": dict(getattr(metrics, "step_durations_ms", {})),
            "model_calls": getattr(metrics, "model_calls", 0),
            "truncation_retries": getattr(metrics, "truncation_retries", 0),
        },
    }
