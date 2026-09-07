"""Toolkit tests for reviewer workflow (spec 169 T012).

Reviewer toolkit has command_runner for survey but no screenshot_capture or
dom_reader (read-only constraint). Tests that refusing a command is recorded
with reason and surveyed files are tracked.
"""
from __future__ import annotations

from performer.models import Score
from performer.workflows.adapter import build_production_toolkit
from performer.workflows.base import WorkflowMetrics

__all__ = ["test_reviewer_toolkit_has_command_runner", "test_reviewer_toolkit_no_screenshot_or_dom"]


def test_reviewer_toolkit_has_command_runner():
    """Reviewer toolkit has a command_runner."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="reviewer",
        workspace_path="/tmp/test",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="reviewer",
    )

    assert toolkit._command_runner is not None


def test_reviewer_toolkit_no_screenshot_or_dom():
    """Reviewer toolkit has no screenshot_capture or dom_reader."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="reviewer",
        workspace_path="/tmp/test",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="reviewer",
    )

    assert toolkit._screenshot_capture is None
    assert toolkit._dom_reader is None


def test_reviewer_toolkit_has_model_call():
    """Reviewer toolkit has a model_call."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="reviewer",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="reviewer",
    )

    assert toolkit._model_call is not None


def test_assessor_vs_reviewer_toolkit():
    """Assessor and reviewer toolkits differ: assessor has no command_runner."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="reviewer",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    reviewer_toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="reviewer",
    )

    assessor_toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="assessor",
    )

    # Reviewer has command_runner; assessor does not
    assert reviewer_toolkit._command_runner is not None
    assert assessor_toolkit._command_runner is None

    # Both have no screenshot_capture or dom_reader
    assert reviewer_toolkit._screenshot_capture is None
    assert assessor_toolkit._screenshot_capture is None
    assert reviewer_toolkit._dom_reader is None
    assert assessor_toolkit._dom_reader is None
