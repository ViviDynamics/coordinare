"""Test that security workflow gets the correct toolkit (spec 170).

Mirrors test_adapter_reviewer_toolkit.py: security should have no screenshot
or DOM reader, but should have command_runner and model_call.
"""

from __future__ import annotations

from performer.models import Score
from performer.workflows.adapter import build_production_toolkit
from performer.workflows.base import WorkflowMetrics


def test_security_toolkit_has_command_runner():
    """Security toolkit has a command_runner."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="security",
        workspace_path="/tmp/test",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="security",
    )

    assert toolkit._command_runner is not None


def test_security_toolkit_has_model_call():
    """Security toolkit has a model_call."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="security",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="security",
    )

    assert toolkit._model_call is not None


def test_security_toolkit_no_screenshot_or_dom():
    """Security toolkit has no screenshot_capture or dom_reader."""
    score = Score(
        title="Test",
        repo_url="https://github.com/test/repo",
        branch="main",
        model="example/model",
        role="security",
        workspace_path="/tmp/test",
    )
    metrics = WorkflowMetrics()

    def event_sink(event):
        pass

    toolkit = build_production_toolkit(
        score,
        metrics=metrics,
        event_sink=event_sink,
        workflow_name="security",
    )

    assert toolkit._screenshot_capture is None
    assert toolkit._dom_reader is None


__all__ = []
