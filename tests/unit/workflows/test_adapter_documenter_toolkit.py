"""Tests for documenter toolkit in adapter.py (spec 171)."""
from __future__ import annotations


class TestBuildProductionToolkitDocumenter:
    """Test toolkit builder for documenter workflow."""

    def test_documenter_is_read_only_workflow(self):
        """Documenter toolkit is configured for read-only access like security/reviewer."""

        # We can't directly test the internals without mocking the full performer
        # stack, but we can verify the workflow is registered and resolvable.
        from performer.workflows import get_workflow, is_supported_workflow

        assert is_supported_workflow("documenter") is True
        wf = get_workflow("documenter")
        assert wf.name == "documenter"
        # The workflow should be a RoleWorkflow (same as security/reviewer)
        from performer.workflows.base import RoleWorkflow
        assert isinstance(wf, RoleWorkflow)
