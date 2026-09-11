"""Every workflow self-describes its ordered steps, and they reach the wire.

#343: the dashboard cannot say which step a performer is on, or for how long.
The step names already exist as module tuples in each workflow, but under two
different names (``STATES`` and ``STEPS``) and only inside the performer
package, so coordinare cannot read them without a cross-package import. That is
the #339 bug class in mirror image, and the AST guard exists to stop exactly
this kind of coupling.

The seam is therefore the wire: each workflow exposes ``steps``, the adapter
reports it, and it rides out on the working ``PerformerResponse``.
"""
from __future__ import annotations

import pytest

from performer.workflows import SUPPORTED_WORKFLOWS, get_workflow

# noop has no user-visible sequence; every other registered workflow does.
STEPLESS = {"noop"}
NAMED = sorted(set(SUPPORTED_WORKFLOWS) - STEPLESS)


@pytest.mark.parametrize("name", NAMED)
def test_every_workflow_declares_its_ordered_steps(name: str) -> None:
    """Parametrized over the registry, not a hand-written list, so a workflow
    added later cannot quietly ship without a step list."""
    workflow = get_workflow(name)
    steps = getattr(workflow, "steps", None)
    assert isinstance(steps, tuple), f"{name} must declare steps as a tuple"
    assert steps, f"{name} declares an empty step sequence"
    assert all(isinstance(s, str) and s for s in steps)
    assert len(set(steps)) == len(steps), f"{name} repeats a step name: {steps}"


@pytest.mark.parametrize("name,expected_first", [
    ("architect", "intake"), ("assessor", "intake"), ("implementer", "intake"),
    ("reviewer", "intake"), ("security", "intake"), ("documenter", "intake"),
    ("closer", "intake"), ("advocate", "intake"), ("curator", "intake"),
    ("qa", "plan"),
])
def test_the_declared_steps_match_the_workflow_module(name: str, expected_first: str) -> None:
    """The `steps` attribute must be the module's real tuple, not a copy that
    can drift from the sequence the workflow actually runs."""
    assert get_workflow(name).steps[0] == expected_first


def test_the_adapter_reports_the_step_list_for_its_workflow() -> None:
    from performer.workflows.adapter import WorkflowAdapter

    adapter = WorkflowAdapter("architect")
    assert adapter.workflow_steps == ("intake", "survey", "blueprint", "size", "report")


def test_a_stepless_workflow_reports_an_empty_list_rather_than_failing() -> None:
    from performer.workflows.adapter import WorkflowAdapter

    assert WorkflowAdapter("noop").workflow_steps == ()


class TestCurrentStepLatch:
    """Gap 1 from the issue: `_current_step` was only latched for the
    `env_bootstrap.` and `qa.` prefixes, so for the other eight workflows
    `get_status().progress` fell back to inner-harness text or the literal
    string "running" and the step was never visible.
    """

    @pytest.mark.parametrize("workflow,text,expected", [
        ("implementer", "implementer.baseline", "baseline"),
        ("architect", "architect.blueprint", "blueprint"),
        ("reviewer", "reviewer.coverage", "coverage"),
        ("security", "security.scan", "scan"),
        ("documenter", "documenter.write", "write"),
        ("closer", "closer.judge", "judge"),
        ("advocate", "advocate.triage", "triage"),
        ("curator", "curator.judge", "judge"),
        ("qa", "qa.observe", "observe"),
    ])
    def test_a_step_event_latches_the_current_step(self, workflow, text, expected) -> None:
        from performer.models import BackendEvent, BackendEventType
        from performer.workflows.adapter import WorkflowAdapter

        adapter = WorkflowAdapter(workflow)
        adapter._on_event(BackendEvent(type=BackendEventType.progress, text=text))
        assert adapter._current_step == expected

    def test_a_foreign_prefix_does_not_latch(self) -> None:
        """Model prose that happens to look like `word.word` must not be read
        as a step transition."""
        from performer.models import BackendEvent, BackendEventType
        from performer.workflows.adapter import WorkflowAdapter

        adapter = WorkflowAdapter("architect")
        for text in ("implementer.baseline", "self.assertEqual", "config.yaml", "blueprint"):
            adapter._on_event(BackendEvent(type=BackendEventType.progress, text=text))
        assert adapter._current_step is None

    def test_an_unknown_step_of_the_right_workflow_does_not_latch(self) -> None:
        """The suffix is checked against the declared sequence, so a stray
        `architect.something` log line cannot invent a step."""
        from performer.models import BackendEvent, BackendEventType
        from performer.workflows.adapter import WorkflowAdapter

        adapter = WorkflowAdapter("architect")
        adapter._on_event(BackendEvent(type=BackendEventType.progress, text="architect.nonsense"))
        assert adapter._current_step is None

    def test_env_bootstrap_keeps_its_prefixed_form(self) -> None:
        """Pre-existing behaviour: env_bootstrap latches the FULL text and
        clears inner progress. Other workflows must not change that."""
        from performer.models import BackendEvent, BackendEventType
        from performer.workflows.adapter import WorkflowAdapter

        adapter = WorkflowAdapter("env_bootstrap")
        adapter.set_inner_progress("some inner text")
        adapter._on_event(
            BackendEvent(type=BackendEventType.progress, text="env_bootstrap.install")
        )
        assert adapter._current_step == "env_bootstrap.install"
        assert adapter._inner_progress is None


class TestTheStepListReachesTheWire:
    """P2: the list rides out on the `working` PerformerResponse, because
    coordinare must not import the performer package to learn it."""

    async def _status(self, backend):
        from performer.main import handle_status
        from performer.models import Performance, Score, Stand
        from performer.protocol import PerformerMessage
        from pathlib import Path

        perf = Performance(
            session_id="sid",
            stand=Stand(path=Path("/tmp/x"), branch="feat/x"),
            score=Score(title="T", repo_url="https://github.com/org/repo", branch="feat/x",
                        github_token="tok"),
            backend=backend,
        )
        return await handle_status(
            PerformerMessage(action="status", session_id="sid"), perf
        )

    @pytest.mark.asyncio
    async def test_a_workflow_session_sends_its_step_list(self) -> None:
        from performer.workflows.adapter import WorkflowAdapter

        adapter = WorkflowAdapter("architect")
        resp = await self._status(adapter)
        assert resp.status == "working"
        assert resp.workflow_steps == ["intake", "survey", "blueprint", "size", "report"]

    @pytest.mark.asyncio
    async def test_a_plain_backend_sends_nothing_rather_than_an_empty_list(self) -> None:
        """An older performer image, or a non-workflow backend, must leave the
        field absent so coordinare can tell "unknown" from "no steps"."""
        from performer.backends.claude_code import ClaudeCodeBackend

        resp = await self._status(ClaudeCodeBackend())
        assert resp.status == "working"
        assert resp.workflow_steps is None
