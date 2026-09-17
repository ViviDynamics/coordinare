"""Tests for progress forwarding during agent turns (spec 167 R-e, T062)."""
import asyncio
import contextlib

import pytest
from performer.models import BackendEvent, BackendEventType
from performer.workflows.adapter import WorkflowAdapter


@pytest.fixture
def fake_backend_with_progress():
    """Fake backend adapter that emits growing progress."""
    class FakeBackend:
        def __init__(self):
            self._status_counter = 0
            self._started = False

        async def start(self, stand, score, **kwargs):
            self._started = True
            self._status_counter = 0

        def get_status(self):
            from performer.backends.base import BackendStatus
            if not self._started:
                return BackendStatus(state="working", progress="not started")
            self._status_counter += 1
            if self._status_counter < 3:
                return BackendStatus(
                    state="working",
                    progress=f"Processing step {self._status_counter}...",
                    questions=[],
                )
            return BackendStatus(state="done", output='{"result": "ok"}')

        def drain_events(self):
            if self._status_counter == 1:
                return [BackendEvent(type=BackendEventType.text, text="turn.started")]
            if self._status_counter == 2:
                return [BackendEvent(type=BackendEventType.text, text="turn.progress")]
            return []

        async def relay_feedback(self, feedback):
            pass

        async def stop(self):
            pass

    return FakeBackend()


@pytest.mark.asyncio
async def test_adapter_set_inner_progress():
    """T062: WorkflowAdapter.set_inner_progress updates get_status().progress."""
    adapter = WorkflowAdapter("qa", toolkit_factory=lambda *a: None)

    # Set inner progress
    adapter.set_inner_progress("inner progress text")
    adapter._task = asyncio.create_task(asyncio.sleep(10))  # Keep task running

    # Get status should show inner progress
    status = adapter.get_status()
    assert status.state == "working"
    assert status.progress == "inner progress text"

    # Cleanup
    adapter._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await adapter._task


@pytest.mark.asyncio
async def test_adapter_events_buffered():
    """T062: outer adapter buffers events from inner workflow."""
    event_log = []

    def _sink(event):
        event_log.append(event)

    adapter = WorkflowAdapter("qa", toolkit_factory=lambda *a: None)

    # Manually create a task and emit events
    async def _dummy():
        await asyncio.sleep(10)

    adapter._task = asyncio.create_task(_dummy())

    # Emit some events via _on_event
    event1 = BackendEvent(type=BackendEventType.progress, text="event1")
    adapter._on_event(event1)

    event2 = BackendEvent(type=BackendEventType.progress, text="event2")
    adapter._on_event(event2)

    # Drain events should return them
    events = adapter.drain_events()
    assert len(events) == 2
    assert events[0].text == "event1"
    assert events[1].text == "event2"

    # Cleanup
    adapter._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await adapter._task


@pytest.mark.asyncio
async def test_adapter_record_inner_status_progress(fake_backend_with_progress):
    """T062: record_inner_status updates inner progress text."""
    from performer.backends.base import BackendStatus

    adapter = WorkflowAdapter("implementer", toolkit_factory=lambda *a: None)

    # Create a dummy task to make adapter appear started
    async def _dummy():
        await asyncio.sleep(10)

    adapter._task = asyncio.create_task(_dummy())

    # Record a status with progress
    status = BackendStatus(state="working", progress="Step 1: Running tests")
    adapter.record_inner_status(status)

    # Inner progress should be updated
    assert adapter._inner_progress == "Step 1: Running tests"

    # get_status should reflect the inner progress
    current_status = adapter.get_status()
    assert "Step 1: Running tests" in current_status.progress

    # Cleanup
    adapter._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await adapter._task


@pytest.mark.asyncio
async def test_adapter_record_inner_status_with_events(fake_backend_with_progress):
    """T062: record_inner_status drains events from backend."""
    from performer.backends.base import BackendStatus

    event_log = []

    def _sink(event):
        event_log.append(event)

    adapter = WorkflowAdapter("implementer", toolkit_factory=lambda *a: None)
    adapter._event_sink = _sink

    # Create a dummy task
    async def _dummy():
        await asyncio.sleep(10)

    adapter._task = asyncio.create_task(_dummy())

    # Create a fake backend with events
    class FakeBackendWithEvents:
        def __init__(self):
            self._events = [
                BackendEvent(type=BackendEventType.progress, text="event1"),
                BackendEvent(type=BackendEventType.progress, text="event2"),
            ]

        def drain_events(self):
            events = self._events
            self._events = []
            return events

    backend = FakeBackendWithEvents()
    status = BackendStatus(state="working", progress="running")

    # Record status with backend
    adapter.record_inner_status(status, backend)

    # Events should be buffered
    drained = adapter.drain_events()
    assert len(drained) >= 2
    event_texts = [e.text for e in drained]
    assert "event1" in event_texts
    assert "event2" in event_texts

    # Cleanup
    adapter._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await adapter._task


@pytest.mark.asyncio
async def test_adapter_consecutive_status_differ_with_events(fake_backend_with_progress):
    """T062: consecutive get_status calls differ when inner produces events."""
    from performer.backends.base import BackendStatus

    adapter = WorkflowAdapter("implementer", toolkit_factory=lambda *a: None)

    # Create a dummy task
    async def _dummy():
        await asyncio.sleep(10)

    adapter._task = asyncio.create_task(_dummy())

    # First status call
    status1 = adapter.get_status()
    assert status1.state == "working"

    # Record inner status with progress
    inner_status = BackendStatus(state="working", progress="Step 1 complete")
    adapter.record_inner_status(inner_status)

    # Second status call should have different progress
    status2 = adapter.get_status()
    assert status2.state == "working"
    assert status2.progress != status1.progress
    assert "Step 1 complete" in status2.progress

    # Cleanup
    adapter._task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await adapter._task
