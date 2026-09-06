"""Test that schema violations in assessor workflow route to bounded retry gate (spec 166 FR-016, 098/119).

Under the workflow, unparseable or invalid model output fails into the existing
malformed-output retry path. The lenient fallback that treats prose as sufficient
does NOT apply. Schema violations must be prefixed with the retry gate marker.
"""
from __future__ import annotations

import asyncio

from performer.models import Score, Stand
from performer.workflows.adapter import _FORMAT_ERROR_PREFIX, WorkflowAdapter
from performer.workflows.toolkit import Toolkit


def _stubbed_toolkit_factory(metrics, event_sink):
    """Toolkit factory that returns a stub toolkit with a model that always fails."""
    from performer.workflows.budget import ModelReply

    class StubModel:
        def __init__(self):
            self.call_count = 0

        async def __call__(self, persona, content, max_tokens):
            self.call_count += 1
            # Return non-JSON on both attempts to trigger SchemaViolation
            if self.call_count == 1:
                return ModelReply(content="This is not JSON, just prose", finish_reason="stop")
            else:
                # Second attempt also fails
                return ModelReply(content="Still not JSON", finish_reason="stop")

    stub_model = StubModel()

    async def _stub_call(persona, content, max_tokens):
        return await stub_model(persona, content, max_tokens)

    return Toolkit(
        metrics=metrics,
        model_call=_stub_call,
        command_runner=None,
        screenshot_capture=None,
        dom_reader=None,
        event_sink=event_sink,
    )


async def test_assessor_schema_violation_returns_error_with_prefix(tmp_path):
    """Schema violation twice results in error status with BACKEND_FORMAT_ERROR prefix."""
    adapter = WorkflowAdapter(
        "assessor",
        toolkit_factory=_stubbed_toolkit_factory,
    )

    # Create minimal stand and score
    stand = Stand(path=tmp_path, branch="main")
    score = Score(
        title="Test",
        description="Test card",
        repo_url="https://github.com/test/test",
        branch="main",
        model="example/model",
    )

    # Start the workflow
    await adapter.start(stand, score)

    # Wait for the workflow to complete
    await asyncio.sleep(0.5)
    while adapter._task and not adapter._task.done():
        await asyncio.sleep(0.1)

    # Get status
    status = adapter.get_status()

    # Verify it's an error state
    assert status.state == "error"
    # Verify the error reason contains the prefix
    assert _FORMAT_ERROR_PREFIX in status.error_reason
    assert "SchemaViolation" in status.error_reason


def test_assessor_schema_violation_marker_is_correct():
    """The marker used matches the constant expected by the retry gate."""
    # This test ensures we're using the exact same constant as coordinare expects
    assert _FORMAT_ERROR_PREFIX == "BACKEND_FORMAT_ERROR:"
