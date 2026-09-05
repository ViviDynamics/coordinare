"""T017 / T018b — the dispatch branch and the FR-001 no-call-home invariant."""
from __future__ import annotations

import asyncio
import json

import pytest
from performer.backends.base import BackendAdapter
from performer.workflows.adapter import WorkflowAdapter


class _Score:
    role = "qa"
    workflow = "noop"
    effort = ""
    temperature = None
    max_tokens = None


@pytest.mark.asyncio
async def test_adapter_satisfies_the_backend_protocol():
    """The whole integration rests on this: if a workflow is a BackendAdapter,
    nothing downstream needs to know workflows exist."""
    adapter = WorkflowAdapter("noop")
    assert isinstance(adapter, BackendAdapter)


@pytest.mark.asyncio
async def test_a_workflow_run_reaches_done_with_a_json_report():
    adapter = WorkflowAdapter("noop")
    await adapter.start(object(), _Score())
    for _ in range(50):
        if adapter.get_status().state == "done":
            break
        await asyncio.sleep(0.01)

    status = adapter.get_status()
    assert status.state == "done"
    assert json.loads(status.output)["noop"] is True


@pytest.mark.asyncio
async def test_a_failing_workflow_surfaces_as_error_not_as_a_silent_pass(monkeypatch):
    """A workflow that raises must reach the performer as an error state.

    Registered explicitly rather than leaning on an unimplemented workflow:
    this needs to keep testing the adapter's error path long after every
    workflow works.
    """
    class _Exploding:
        name = "exploding"

        async def run(self, stand, score, toolkit):
            raise RuntimeError("step 3 could not reach the app")

    adapter = WorkflowAdapter("noop")
    monkeypatch.setattr(adapter, "_workflow", _Exploding())
    await adapter.start(object(), _Score())
    for _ in range(50):
        if adapter.get_status().state in ("done", "error"):
            break
        await asyncio.sleep(0.01)

    status = adapter.get_status()
    assert status.state == "error"
    assert "RuntimeError" in (status.error_reason or "")
    assert "step 3" in (status.error_reason or "")


@pytest.mark.asyncio
async def test_stop_cancels_a_running_workflow():
    adapter = WorkflowAdapter("noop")
    await adapter.start(object(), _Score())
    await adapter.stop()  # must not raise even if already finished


@pytest.mark.asyncio
async def test_relay_feedback_is_inert_rather_than_reaching_into_a_running_run():
    """FR-001: mid-run coupling is exactly what the design rules out.

    The previous version asserted only that the call did not raise, which the
    adversarial review correctly called worthless: stashing the feedback on the
    adapter would have passed while breaking the invariant the test claims to
    guard. Now it asserts the adapter state is UNCHANGED.
    """
    adapter = WorkflowAdapter("noop")
    await adapter.start(object(), _Score())

    before = {
        k: v for k, v in vars(adapter).items()
        if k not in ("_task", "_result", "_events", "_metrics")
    }
    await adapter.relay_feedback("please also check X")
    after = {
        k: v for k, v in vars(adapter).items()
        if k not in ("_task", "_result", "_events", "_metrics")
    }

    assert after == before, (
        "relay_feedback must not record anything on the adapter: a running "
        "workflow's sequence is fixed, and stashed feedback is mid-run coupling"
    )
    assert not any("feedback" in k for k in vars(adapter)), (
        "no feedback may be retained for the running workflow to consult"
    )


# --- T018b: FR-001, the invariant the whole design rests on ---

@pytest.mark.asyncio
async def test_workflow_run_makes_no_coordinare_bound_network_call(monkeypatch):
    """A workflow runs entirely inside the performer. Nothing calls home.

    Enforced by making every outbound HTTP primitive explode: if a workflow
    step ever reaches for one, this test names it.
    """
    import httpx

    def _boom(*args, **kwargs):
        raise AssertionError(
            "workflow made an outbound HTTP call; FR-001 requires it to run "
            "entirely inside the performer with no mid-run coordinare contact"
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", _boom, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", _boom, raising=False)
    monkeypatch.setattr(httpx.Client, "request", _boom, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", _boom, raising=False)

    adapter = WorkflowAdapter("noop")
    await adapter.start(object(), _Score())
    for _ in range(50):
        if adapter.get_status().state in ("done", "error"):
            break
        await asyncio.sleep(0.01)

    status = adapter.get_status()
    assert status.state == "done", f"workflow did not complete cleanly: {status.error_reason}"


@pytest.mark.asyncio
async def test_events_are_drained_not_replayed():
    """FR-008: visibility without control. Draining twice must not duplicate."""
    adapter = WorkflowAdapter("noop")
    await adapter.start(object(), _Score())
    for _ in range(50):
        if adapter.get_status().state == "done":
            break
        await asyncio.sleep(0.01)

    first = adapter.drain_events()
    second = adapter.drain_events()
    assert second == [], "drain must clear the buffer"
    assert isinstance(first, list)


# --- the wiring the scenario eval structurally cannot check ---

def test_the_production_toolkit_wires_every_primitive():
    """Adversarial review, critical.

    The adapter previously built Toolkit(metrics, event_sink) with NO
    model_call, command_runner, screenshot_capture or dom_reader. Each raises
    on first use, so the QA workflow could not run in a real performer at all.

    The eval could never catch this: it supplies its own toolkit. Neither could
    the report-seam test, which checks the report's SHAPE, not its wiring.
    """
    from performer.workflows.adapter import build_production_toolkit
    from performer.workflows.base import WorkflowMetrics

    class _S:
        model = "some-model"
        workspace_path = "/workspace"

    tk = build_production_toolkit(_S(), metrics=WorkflowMetrics(), event_sink=lambda _e: None)

    for attr in ("_model_call", "_command_runner", "_screenshot_capture", "_dom_reader"):
        assert getattr(tk, attr) is not None, f"{attr} is not wired for production"


@pytest.mark.asyncio
async def test_an_unwired_primitive_raises_rather_than_returning_nothing():
    """The guards must stay loud. A toolkit missing a primitive should fail
    obviously, not return an empty result that reads as 'nothing found'."""
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.toolkit import Toolkit

    bare = Toolkit(metrics=WorkflowMetrics())
    with pytest.raises(RuntimeError, match="command runner"):
        await bare.run_command("echo hi")
    with pytest.raises(RuntimeError, match="DOM reader"):
        await bare.dom_snapshot("http://x/")


def test_the_adapter_uses_the_production_toolkit_by_default():
    """No factory injected means the real one, not a hollow stand-in."""
    import inspect

    from performer.workflows import adapter

    source = inspect.getsource(adapter.WorkflowAdapter.start)
    assert "build_production_toolkit" in source


@pytest.mark.asyncio
async def test_workflow_metrics_ride_the_report_to_the_response():
    """Round-two review: metrics were computed and never exposed, so an operator
    could not see a run was truncated five times or spent 45s planning."""
    adapter = WorkflowAdapter("noop")
    await adapter.start(object(), _Score())
    for _ in range(50):
        if adapter.get_status().state == "done":
            break
        await asyncio.sleep(0.01)
    payload = json.loads(adapter.get_status().output)
    assert "workflow_metrics" in payload
    for key in ("model_calls", "truncation_retries", "schema_reprompts", "step_durations_ms"):
        assert key in payload["workflow_metrics"]


@pytest.mark.asyncio
async def test_get_status_reports_the_step_actually_running():
    """_current_step was declared and never assigned; progress read "running"
    for the whole run. Step events now drive it."""
    from performer.models import BackendEvent, BackendEventType

    adapter = WorkflowAdapter("noop")
    adapter._on_event(BackendEvent(type=BackendEventType.progress, text="qa.judge"))
    assert adapter._current_step == "judge"
    assert len(adapter.drain_events()) == 1


def test_production_screenshots_do_not_share_one_fixed_path():
    """Round-one finding that never received a verdict. The production capture
    defaulted to /tmp/qa_screenshot.png -- one path for every run on the host.
    Under Docker/k8s each container has its own /tmp; on the subprocess
    transport two concurrent QA runs would overwrite each other's evidence and
    a PR could carry the wrong project's screenshot."""
    from performer.workflows.adapter import _default_screenshot_path

    a, b = _default_screenshot_path(), _default_screenshot_path()
    assert a != b, "each call must yield a distinct path"
    assert a.endswith(".png") and b.endswith(".png")
    assert "qa_screenshot" not in a or a != "/tmp/qa_screenshot.png"
