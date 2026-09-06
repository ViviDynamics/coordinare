"""Test adapter toolkit configuration for assessor workflow.

Mutation check: temporarily re-wire screenshot_capture=_capture in adapter.py
build_production_toolkit for assessor, observer this test fail on
assert toolkit._screenshot_capture is None, restore to verify the assertion pins
the write-free requirement.
"""
from __future__ import annotations

from performer.models import Score
from performer.workflows.adapter import build_production_toolkit
from performer.workflows.base import WorkflowMetrics


def test_assessor_toolkit_write_free():
    """Assessor workflow is write-free: no command_runner, screenshot_capture, or dom_reader."""
    score = Score(
        title="Test",
        description="Test card",
        repo_url="https://github.com/test/test",
        branch="main",
        model="example/model"
    )
    toolkit = build_production_toolkit(
        score,
        metrics=WorkflowMetrics(),
        event_sink=lambda _: None,
        workflow_name="assessor"
    )
    assert toolkit._command_runner is None
    assert toolkit._screenshot_capture is None
    assert toolkit._dom_reader is None


def test_qa_toolkit_has_all_capabilities():
    """QA workflow has command_runner, screenshot_capture, and dom_reader."""
    score = Score(
        title="Test",
        description="Test card",
        repo_url="https://github.com/test/test",
        branch="main",
        model="example/model"
    )
    toolkit = build_production_toolkit(
        score,
        metrics=WorkflowMetrics(),
        event_sink=lambda _: None,
        workflow_name="qa"
    )
    assert toolkit._command_runner is not None
    assert toolkit._screenshot_capture is not None
    assert toolkit._dom_reader is not None


def test_architect_toolkit_has_all_capabilities():
    """Architect workflow has command_runner, screenshot_capture, and dom_reader."""
    score = Score(
        title="Test",
        description="Test card",
        repo_url="https://github.com/test/test",
        branch="main",
        model="example/model"
    )
    toolkit = build_production_toolkit(
        score,
        metrics=WorkflowMetrics(),
        event_sink=lambda _: None,
        workflow_name="architect"
    )
    assert toolkit._command_runner is not None
    assert toolkit._screenshot_capture is not None
    assert toolkit._dom_reader is not None
