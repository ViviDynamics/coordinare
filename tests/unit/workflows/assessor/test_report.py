"""Assessor report step (spec 166 FR-003): write-free proof and metrics packaging.

build_report raises AssessorHadCommandRunner if any command ran, if a command
runner is present, if a screenshot_capture is present, or if a dom_reader is present.
With none of those, it returns a valid report with metrics.

Mutation check: remove metrics.commands_run += 1 from toolkit.run_command and
observe test_build_report_passes_with_no_runner fail while test with commands
still passes (wrongly); restore, then both pass. Also temporarily wire
screenshot_capture and dom_reader in adapter.py for assessor, observe the
screenshot/dom tests fail, restore to verify.
"""
from __future__ import annotations

import pytest
from performer.workflows.assessor.models import Assessment, GateRecord
from performer.workflows.assessor.report import AssessorHadCommandRunner, build_report
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit


def _sample_assessment() -> Assessment:
    """A minimal valid assessment for testing."""
    return Assessment(
        ready=True,
        goal="Do the thing",
        expected_behavior="It works",
        out_of_scope=[],
        questions=[],
        assumptions=[],
        criteria=[],
        criteria_source="card",
        clarifications=[],
        assessment_hash="a" * 64,
    )


def _sample_record() -> GateRecord:
    """A minimal valid gate record for testing."""
    return GateRecord(
        questions_kept=0,
        questions_dropped_by_cap=[],
        questions_dropped_as_answered=[],
        answers_matched=[],
        questions_turned_to_assumptions=[],
        round_count=1,
    )


def test_build_report_passes_with_no_runner_and_no_commands():
    """With no command runner and commands_run == 0, the report is built and
    valid."""
    metrics = WorkflowMetrics()
    tk = Toolkit(metrics=metrics, command_runner=None)
    assessment = _sample_assessment()
    record = _sample_record()

    result = build_report(assessment, record, tk)

    assert isinstance(result, dict)
    assert "assessment" in result
    assert "gate_record" in result
    assert "write_free_check" in result
    assert result["write_free_check"]["passed"] is True
    assert result["write_free_check"]["commands_run"] == 0
    assert result["write_free_check"]["has_command_runner"] is False


def test_build_report_raises_when_command_runner_present():
    """With a command runner configured, raise AssessorHadCommandRunner."""
    metrics = WorkflowMetrics()

    async def dummy_runner(cmd, cwd, timeout_s):
        return 0, ""

    tk = Toolkit(metrics=metrics, command_runner=dummy_runner)
    assessment = _sample_assessment()
    record = _sample_record()

    with pytest.raises(AssessorHadCommandRunner, match="Command runner present"):
        build_report(assessment, record, tk)


def test_build_report_raises_when_commands_ran():
    """With commands_run > 0, raise AssessorHadCommandRunner."""
    metrics = WorkflowMetrics()
    metrics.commands_run = 1
    tk = Toolkit(metrics=metrics, command_runner=None)
    assessment = _sample_assessment()
    record = _sample_record()

    with pytest.raises(AssessorHadCommandRunner, match="commands run: 1"):
        build_report(assessment, record, tk)


def test_build_report_includes_workflow_metrics():
    """The report carries model_calls, truncation_retries, and step_durations_ms."""
    metrics = WorkflowMetrics()
    metrics.model_calls = 1
    metrics.truncation_retries = 0
    metrics.step_durations_ms = {"intake": 100, "assess": 2000, "gate": 50, "report": 10}
    tk = Toolkit(metrics=metrics, command_runner=None)
    assessment = _sample_assessment()
    record = _sample_record()

    result = build_report(assessment, record, tk)

    assert result["workflow_metrics"]["model_calls"] == 1
    assert result["workflow_metrics"]["truncation_retries"] == 0
    assert result["workflow_metrics"]["step_durations_ms"] == metrics.step_durations_ms


def test_build_report_raises_when_screenshot_capture_present():
    """With screenshot_capture configured, raise AssessorHadWriteCapability."""
    metrics = WorkflowMetrics()

    async def dummy_capture(*, url: str, out_path: str | None = None) -> str | None:
        return None

    tk = Toolkit(metrics=metrics, command_runner=None, screenshot_capture=dummy_capture)
    assessment = _sample_assessment()
    record = _sample_record()

    with pytest.raises(AssessorHadCommandRunner, match="screenshot_capture"):
        build_report(assessment, record, tk)


def test_build_report_raises_when_dom_reader_present():
    """With dom_reader configured, raise AssessorHadWriteCapability."""
    metrics = WorkflowMetrics()

    async def dummy_dom_reader(url: str, selector: str | None = None) -> str:
        return ""

    tk = Toolkit(metrics=metrics, command_runner=None, dom_reader=dummy_dom_reader)
    assessment = _sample_assessment()
    record = _sample_record()

    with pytest.raises(AssessorHadCommandRunner, match="dom_reader"):
        build_report(assessment, record, tk)


def test_build_report_write_free_check_includes_capabilities():
    """The write_free_check includes has_screenshot_capture and has_dom_reader."""
    metrics = WorkflowMetrics()
    tk = Toolkit(metrics=metrics, command_runner=None)
    assessment = _sample_assessment()
    record = _sample_record()

    result = build_report(assessment, record, tk)

    assert "has_screenshot_capture" in result["write_free_check"]
    assert "has_dom_reader" in result["write_free_check"]
    assert result["write_free_check"]["has_screenshot_capture"] is False
    assert result["write_free_check"]["has_dom_reader"] is False
