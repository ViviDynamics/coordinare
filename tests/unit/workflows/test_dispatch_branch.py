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

    # 343: the adapter is now built for the workflow whose events it is fed.
    # This used to pass a `qa.` event to a `noop` adapter, which worked only
    # because the latch matched the `qa.` prefix on ANY adapter. Matching each
    # adapter against its own declared steps is what stops model prose shaped
    # like `self.assertEqual` registering as a step.
    adapter = WorkflowAdapter("qa")
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


@pytest.mark.asyncio
async def test_the_architect_workflow_runs_behind_the_same_adapter_seam(tmp_path):
    """165: the second consumer of the layer is dispatched exactly like the
    first: BackendAdapter-shaped, done with a JSON report main.py can parse,
    and its report carries the blueprint coordinare lifts."""
    import json
    from types import SimpleNamespace

    from performer.workflows.adapter import WorkflowAdapter

    from tests.unit.workflows.architect.test_workflow_end_to_end import (
        _TRIVIAL_BP,
        _score,
        _stub_toolkit,
    )

    tk, ran, _events = _stub_toolkit(_TRIVIAL_BP, ["ls app"])
    adapter = WorkflowAdapter("architect", toolkit_factory=lambda metrics, sink: tk)
    await adapter.start(SimpleNamespace(path=tmp_path), _score())
    await adapter._task
    status = adapter.get_status()
    assert status.state == "done"
    report = json.loads(status.output)
    assert report["blueprint"]["size"] == "small" and report["write_free_check"]["passed"] is True
    assert ran == ["ls app", "git status --porcelain"]


@pytest.mark.asyncio
async def test_the_assessor_workflow_runs_behind_the_same_adapter_seam(tmp_path):
    """166: the third consumer of the layer is dispatched exactly like the
    first two: BackendAdapter-shaped, done with a JSON report main.py can parse,
    and its report carries the assessment coordinare lifts."""
    import json
    from types import SimpleNamespace

    from performer.workflows.adapter import WorkflowAdapter
    from performer.workflows.budget import ModelReply

    from tests.unit.workflows.assessor.test_workflow_end_to_end import _CLEAR_ASSESSMENT

    async def model_call(persona, content, max_tokens):
        return ModelReply(content=json.dumps(_CLEAR_ASSESSMENT), finish_reason="stop")

    # Stub toolkit for assessor (no command runner)
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.toolkit import Toolkit

    events = []
    stub_tk = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=model_call,
        command_runner=None,
        screenshot_capture=None,
        dom_reader=None,
        event_sink=events.append,
        call_limit=12,
    )

    def score_for_assessor():
        return SimpleNamespace(
            title="Test card",
            description="A clear card",
            acceptance_criteria=["Criterion 1"],
            clarifications=[],
            issue_number=42,
            workflow_env={},
        )

    adapter = WorkflowAdapter("assessor", toolkit_factory=lambda metrics, sink: stub_tk)
    await adapter.start(SimpleNamespace(path=tmp_path), score_for_assessor())
    await adapter._task
    status = adapter.get_status()
    assert status.state == "done"
    report = json.loads(status.output)
    assert "assessment" in report and report["assessment"]["ready"] is True
    assert report["write_free_check"]["passed"] is True


@pytest.mark.asyncio
async def test_the_reviewer_workflow_runs_behind_the_same_adapter_seam(tmp_path):
    """169: the fifth consumer of the layer is dispatched exactly like the
    others: BackendAdapter-shaped, done with a JSON report main.py can parse,
    and its report carries the review record coordinare lifts. The GitHub
    poster is a recorder here: nothing leaves the test."""
    import json
    from types import SimpleNamespace

    from performer.workflows.adapter import WorkflowAdapter
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.reviewer import ReviewerWorkflow
    from performer.workflows.toolkit import Toolkit

    from tests.unit.workflows.reviewer.test_workflow_end_to_end import (
        DIV_FINDING,
        SURVEY,
        FakeGitHub,
        _score,
    )

    replies = [SURVEY, {"findings": [DIV_FINDING], "dispositions": []}]
    state = {"i": 0}

    async def model_call(persona, content, max_tokens):
        reply = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        return (0, "") if cmd.startswith("git status") else (0, "abc123 add div\n")

    events = []
    stub_tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, event_sink=events.append, call_limit=12)
    gh = FakeGitHub()
    adapter = WorkflowAdapter("reviewer", toolkit_factory=lambda metrics, sink: stub_tk)
    adapter._workflow = ReviewerWorkflow(poster=gh.post)  # the registry's instance would post to GitHub for real
    await adapter.start(SimpleNamespace(path=tmp_path), _score())
    await adapter._task
    status = adapter.get_status()
    assert status.state == "done", status
    report = json.loads(status.output)
    assert report["review"]["verdict"] == "changes_requested"
    assert report["review"]["findings"][0]["path"] == "src/calc.py"
    assert report["write_free_check"]["passed"] is True
    assert [r["event"] for r in gh.reviews] == ["REQUEST_CHANGES"]


@pytest.mark.asyncio
async def test_the_security_workflow_runs_behind_the_same_adapter_seam(tmp_path):
    """170: the sixth consumer of the layer is dispatched exactly like the
    others: BackendAdapter-shaped, done with a JSON report main.py can parse,
    and its report carries the security record coordinare routes. The GitHub
    poster and the scanner are fakes here: nothing leaves the test."""
    import json
    from types import SimpleNamespace

    from performer.workflows.adapter import WorkflowAdapter
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.security import SecurityWorkflow
    from performer.workflows.toolkit import Toolkit

    from tests.eval.security_scenarios.stub_model import answer_tooling_and_reading
    from tests.unit.workflows.security.test_workflow_end_to_end import (
        INJECTION,
        SURVEY,
        FakeGitHub,
        _score,
        fake_scanner,
    )

    replies = [SURVEY, {"findings": [INJECTION]}]
    state = {"i": 0}

    async def model_call(persona, content, max_tokens):
        # 366 added two model calls before the scripted steps. Answering them
        # off this list would advance the ordinal and hand the survey step the
        # findings reply; the shared answerer keeps `replies` meaning what it
        # meant when this test was written.
        answered = answer_tooling_and_reading(persona, content)
        if answered is not None:
            return answered
        reply = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        return (0, "") if cmd.startswith("git status") else (0, "abc123 add lookup\n")

    events = []
    stub_tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, event_sink=events.append, call_limit=12)
    gh = FakeGitHub()
    adapter = WorkflowAdapter("security", toolkit_factory=lambda metrics, sink: stub_tk)
    adapter._workflow = SecurityWorkflow(poster=gh.post, scan_runner=fake_scanner())  # the registry's instance would post and scan for real
    await adapter.start(SimpleNamespace(path=tmp_path), _score())
    await adapter._task
    status = adapter.get_status()
    assert status.state == "done", status
    report = json.loads(status.output)
    assert report["security"]["verdict"] == "security_failed"
    assert report["security"]["blocking"][0]["category"] == "injection"
    assert [r["event"] for r in gh.reviews] == ["REQUEST_CHANGES"]


@pytest.mark.asyncio
async def test_the_documenter_workflow_runs_behind_the_same_adapter_seam(tmp_path):
    """171: the seventh consumer of the layer is dispatched exactly like the
    others: BackendAdapter-shaped, done with a JSON report main.py can parse,
    and its report carries the docs record. The committer is local: nothing is pushed."""
    import json
    from types import SimpleNamespace

    from performer.models import Stand
    from performer.workflows.adapter import WorkflowAdapter
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.budget import ModelReply
    from performer.workflows.documenter import DocumenterWorkflow
    from performer.workflows.toolkit import Toolkit

    from tests.unit.workflows.documenter._repo import add_payments, local_committer, make_repo
    from tests.unit.workflows.documenter.test_workflow_end_to_end import (
        BRIEF,
        PAYMENTS_PAGE,
        _run_command,
    )

    repo = make_repo(tmp_path)
    diff = add_payments(repo)

    async def model_call(persona, content, max_tokens):
        reply = {"action": "write", "content": PAYMENTS_PAGE, "reason": ""} if "payments.md" in persona else {"action": "unchanged", "content": "", "reason": "fine"}
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    events = []
    stub_tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=_run_command, event_sink=events.append, call_limit=20)
    adapter = WorkflowAdapter("documenter", toolkit_factory=lambda metrics, sink: stub_tk)
    adapter._workflow = DocumenterWorkflow(committer=local_committer)  # the registry's instance would push through commit_files
    score = SimpleNamespace(pr_diff=diff, documentation_brief=BRIEF, doc_mode="update", issue_number=7, title="t", description="d", workflow_env={}, owner_repo=("o", "r"), effective_github_token="t")
    await adapter.start(Stand(path=repo, branch="feat/payments"), score)
    await adapter._task
    status = adapter.get_status()
    assert status.state == "done", status
    report = json.loads(status.output)
    assert report["docs"]["verdict"] == "docs_committed" and "docs/wiki/payments.md" in report["docs"]["files_written"]


@pytest.mark.asyncio
async def test_the_closer_workflow_runs_behind_the_same_adapter_seam(tmp_path):
    """172: the eighth consumer of the layer is dispatched exactly like the others,
    and the common path makes no model call at all."""
    import json
    from types import SimpleNamespace

    from performer.workflows.adapter import WorkflowAdapter
    from performer.workflows.base import WorkflowMetrics
    from performer.workflows.closer import CloserWorkflow
    from performer.workflows.toolkit import Toolkit

    from tests.unit.workflows.closer._fakes import FakeGitHub, comment, thread

    async def no_model(*_a, **_k):
        raise AssertionError("a card whose threads are all resolved must make no model call")

    events = []
    stub_tk = Toolkit(metrics=WorkflowMetrics(), model_call=no_model, command_runner=None, event_sink=events.append, call_limit=8)
    gh = FakeGitHub([thread("t1", comment("reviewer", "please guard this"), resolved=True)])
    adapter = WorkflowAdapter("closer", toolkit_factory=lambda metrics, sink: stub_tk)
    adapter._workflow = CloserWorkflow(fetcher=gh.fetcher, resolver=gh.resolver, poster=gh.poster)  # the registry's instance would call GitHub
    score = SimpleNamespace(pr_url="https://github.com/o/r/pull/7", owner_repo=("o", "r"), effective_github_token="t",
                            backend="codex", model="m", workflow_env={}, head_sha="abc")
    await adapter.start(SimpleNamespace(path=tmp_path), score)
    await adapter._task
    status = adapter.get_status()
    assert status.state == "done", status
    report = json.loads(status.output)
    assert report["closing"]["verdict"] == "approved" and report["workflow_metrics"]["model_calls"] == 0
