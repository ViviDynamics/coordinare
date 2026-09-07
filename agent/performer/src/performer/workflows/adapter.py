"""Adapts a RoleWorkflow to the BackendAdapter interface (spec 164 T016).

DESIGN NOTE — deviation from tasks.md T016, which said to branch the dispatch
in ``main.py``.  The performer drives backends through a start / poll
``get_status`` / ``drain_events`` / ``stop`` loop, and a workflow is a coroutine
that runs to completion.  Rather than adding a parallel execution path through
``main.py`` (and with it the two-paths maintenance debt flagged in plan.md's
Complexity Tracking), the workflow is presented AS a BackendAdapter.

``main.py`` then changes by three lines — which adapter to construct — and every
piece of existing machinery (Performance, the monitor loop, role
post-processing, event draining, cleanup) keeps working untouched.  The
coordinare/performer contract is unchanged, as FR-001 requires.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.backends.base import BackendStatus
from performer.workflows import get_workflow
from performer.workflows.base import SchemaViolation, WorkflowMetrics, WorkflowResult
from performer.workflows.budget import ModelReply
from performer.workflows.toolkit import Toolkit

if TYPE_CHECKING:
    from performer.models import BackendEvent, Score, Stand

log = structlog.get_logger(__name__)

# Read timeout for one model call. 300 s killed the first live QA run: a
# reasoning model producing a doubled 6000-token plan budget needs longer
# than that at the Spark's generation rate. Connect stays short so a dead
# gateway still fails fast.
_MODEL_READ_TIMEOUT_S = 900.0
_MODEL_CONNECT_TIMEOUT_S = 30.0
_FORMAT_ERROR_PREFIX = "BACKEND_FORMAT_ERROR:"


def _effective_max_tokens(requested: int, role_cap: object) -> int:
    """A step budget never exceeds the role's configured output cap.

    Budgets double on a truncation retry (plan/judge reach 16000), and an
    operator may have set ``performers.<role>.max_tokens`` lower than that.
    The role cap is the operator's word; 0/None/invalid means no cap.
    """
    try:
        cap = int(role_cap)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return requested
    return min(requested, cap) if cap > 0 else requested


def _command_runner(workspace: "Path"):
    """Adapt the performer's run_command to the Toolkit's (exit_code, output)."""
    from performer.workspace import run_command

    async def _run(cmd: str, cwd, timeout_s: int) -> tuple[int, str]:
        result = await run_command(cmd, Path(cwd or workspace), timeout_s, truncate="tail")
        output = (result.stdout or "") + (result.stderr or "")
        return result.exit_code, output

    return _run


def _model_caller(score: "Score"):
    """Call the model through the LiteLLM gateway.

    All model traffic goes through the gateway -- the standing rule for this
    project -- never directly at a model host.
    """
    import json as _json

    import httpx

    from performer.config import get_settings

    async def _call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        settings = get_settings()
        base = (settings.LITELLM_PROXY_BASE_URL or "").strip().rstrip("/")
        if not base:
            raise RuntimeError(
                "LITELLM_PROXY_BASE_URL is unset; a role workflow cannot reach "
                "the model gateway"
            )
        token = (settings.LITELLM_PROXY_AUTH_TOKEN or "").strip()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        max_tokens = _effective_max_tokens(max_tokens, getattr(score, "max_tokens", None))
        body = {
            "model": score.model or "",
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": persona},
                {"role": "user", "content": content},
            ],
        }
        started = time.monotonic()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_MODEL_READ_TIMEOUT_S, connect=_MODEL_CONNECT_TIMEOUT_S)
        ) as client:
            resp = await client.post(
                f"{base}/chat/completions", headers=headers, content=_json.dumps(body)
            )
            resp.raise_for_status()
            payload = resp.json()
        choice = payload["choices"][0]
        log.info(
            "workflow.model_call",
            max_tokens=max_tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            finish_reason=choice.get("finish_reason"),
            completion_tokens=(payload.get("usage") or {}).get("completion_tokens"),
        )
        message = choice.get("message") or {}
        return ModelReply(
            content=message.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            reasoning_content=message.get("reasoning_content"),
        )

    return _call


def _default_screenshot_path() -> str:
    """A per-capture path, never one shared file for the whole host.

    /tmp/qa_screenshot.png was fine inside a container with its own /tmp. On the
    subprocess transport two concurrent QA runs overwrote each other's evidence,
    and a PR could carry the wrong project's screenshot (round-one finding whose
    verifier failed; dispositioned by hand).
    """
    import tempfile

    fd, path = tempfile.mkstemp(prefix="qa_screenshot_", suffix=".png")
    import os

    os.close(fd)
    return path


def build_agent_turn_runner(
    *,
    stand,
    score,
    backend_name: str,
    model: str | None = None,
    effort: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    forward_progress=None,
    backend_factory=None,
):
    """Build an agent_turn_runner for the implementer workflow (spec 167 R7).

    Arguments:
        stand: Stand object with the cloned workspace path
        score: Score object for this turn
        backend_name: name of the backend (e.g. "claude_code", "junie")
        model: optional model override
        effort: optional effort override
        temperature: optional temperature override
        max_tokens: optional token count override
        forward_progress: callable to forward BackendStatus to outer adapter
        backend_factory: optional factory to instantiate backends (defaults to get_backend)

    Returns:
        An async callable runner(brief: dict, *, timeout_s: float) -> dict
        that runs one turn and returns TurnResult dict.
    """
    import subprocess

    from performer.backends import get_backend as _default_get_backend

    if backend_factory is None:
        backend_factory = _default_get_backend

    async def _runner(brief: dict, *, timeout_s: float) -> dict:
        """Run one agent turn against the backend adapter."""
        # Resolve backend adapter
        backend = backend_factory(backend_name.replace("-", "_").lower())  # main.py normalises kebab-case the same way

        # Record starting state
        git_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(stand.path),
            capture_output=True,
            text=True,
            check=True,
        )
        start_sha = git_result.stdout.strip()

        # Record untracked/dirty snapshot for initial state
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(stand.path),
            capture_output=True,
            text=True,
            check=True,
        )

        # Build per-turn Score with modified persona_instructions
        turn_score = score.model_copy(
            update={
                "persona_instructions": brief.get("persona", ""),
            }
        )

        # Start the backend
        started_at = time.monotonic()
        try:
            await backend.start(
                stand,
                turn_score,
                model=model,
                effort=effort,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            return {
                "exit_state": "error",
                "output_tail": f"Failed to start backend: {exc}",
                "changed_paths": {},
                "wall_ms": int((time.monotonic() - started_at) * 1000),
                "harness_commits": [],
            }

        # Poll for completion
        poll_interval_s = 2.0
        final_status = None

        try:
            while True:
                elapsed = time.monotonic() - started_at
                if elapsed > timeout_s:
                    await backend.stop()
                    return {
                        "exit_state": "timeout",
                        "output_tail": "Turn exceeded wall-clock timeout",
                        "changed_paths": {},
                        "wall_ms": int(elapsed * 1000),
                        "harness_commits": [],
                    }

                status = backend.get_status()
                final_status = status

                # Forward progress and drain events to outer adapter (spec 167 R-e)
                if forward_progress is not None and status is not None:
                    forward_progress(status, backend)

                if status.state in {"done", "error"}:
                    break

                await asyncio.sleep(poll_interval_s)
        except asyncio.CancelledError:
            await backend.stop()
            raise

        # Compute changed files
        # Get working tree changes (git status --porcelain)
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(stand.path),
            capture_output=True,
            text=True,
            check=True,
        )
        working_tree_changes = {}
        for line in status_result.stdout.splitlines():
            if not line.strip():
                continue
            # Format: "XY filename"
            status_code = line[:2]
            path = line[3:]
            if status_code[0] == "A" or status_code[0] == "?":
                working_tree_changes[path] = "added"
            elif status_code[0] == "D":
                working_tree_changes[path] = "deleted"
            else:
                working_tree_changes[path] = "modified"

        # Get committed changes (git diff --name-only --name-status)
        diff_result = subprocess.run(
            ["git", "diff", "--name-status", start_sha],
            cwd=str(stand.path),
            capture_output=True,
            text=True,
            check=True,
        )
        committed_changes = {}
        for line in diff_result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) < 2:
                continue
            status_code = parts[0]
            path = parts[1]
            if status_code == "A":
                committed_changes[path] = "added"
            elif status_code == "D":
                committed_changes[path] = "deleted"
            else:
                committed_changes[path] = "modified"

        # Merge both dictionaries (committed takes precedence as it's more authoritative)
        changed_paths = dict(working_tree_changes)
        changed_paths.update(committed_changes)

        # Get commits made during the turn
        commits_result = subprocess.run(
            ["git", "rev-list", f"{start_sha}..HEAD"],
            cwd=str(stand.path),
            capture_output=True,
            text=True,
            check=True,
        )
        harness_commits = commits_result.stdout.strip().splitlines()

        # Compute output tail
        output_tail = ""
        if final_status and final_status.output:
            output = final_status.output or ""
            output_tail = output[-4000:] if len(output) > 4000 else output

        wall_ms = int((time.monotonic() - started_at) * 1000)

        if final_status and final_status.state == "error":
            return {
                "exit_state": "error",
                "output_tail": final_status.error_reason or output_tail,
                "changed_paths": changed_paths,
                "wall_ms": wall_ms,
                "harness_commits": harness_commits,
            }

        return {
            "exit_state": "done",
            "output_tail": output_tail,
            "changed_paths": changed_paths,
            "wall_ms": wall_ms,
            "harness_commits": harness_commits,
        }

    return _runner


def build_production_toolkit(
    score: "Score",
    *,
    metrics,
    event_sink,
    workflow_name: str = "",
    stand=None,
    model: str | None = None,
    effort: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    inner_status_recorder=None,
    backend_factory=None,
) -> Toolkit:
    """Wire a Toolkit with every primitive a workflow needs at runtime.

    Adversarial review, critical: the previous version constructed
    ``Toolkit(metrics=..., event_sink=...)`` with NO model_call, command_runner,
    screenshot_capture or dom_reader. Every one of those raises on first use, so
    the QA workflow could not run in a real performer at all. The scenario eval
    never caught it because it supplies its own toolkit.

    The assessor workflow has no command_runner (write-free requirement).
    """
    from performer.qa_capture import capture_app_screenshot
    from performer.workflows.qa.dom import read_dom

    workspace = Path(getattr(score, "workspace_path", "") or ".")

    def _capture(*, url: str, out_path: str | None = None) -> str | None:
        import os

        return capture_app_screenshot(
            env=dict(os.environ),
            out_path=out_path or _default_screenshot_path(),
        )

    # Build agent_turn_runner for implementer workflow (spec 167 T012)
    agent_turn_runner = None
    if workflow_name == "implementer" and stand is not None:
        from performer.config import get_settings

        backend_name = score.backend or get_settings().AGENT_BACKEND

        def _forward_progress(status, backend):
            if inner_status_recorder is not None:
                inner_status_recorder(status, backend)

        agent_turn_runner = build_agent_turn_runner(
            stand=stand,
            score=score,
            backend_name=backend_name,
            model=model,
            effort=effort,
            temperature=temperature,
            max_tokens=max_tokens,
            forward_progress=_forward_progress,
            backend_factory=backend_factory,
        )

    # Assessor workflow is write-free: no command_runner, screenshot_capture, or dom_reader
    if workflow_name == "assessor":
        return Toolkit(
            metrics=metrics,
            model_call=_model_caller(score),
            command_runner=None,
            screenshot_capture=None,
            dom_reader=None,
            event_sink=event_sink,
            agent_turn_runner=agent_turn_runner,
        )

    # Reviewer workflow is read-only: has command_runner for survey, but no screenshot_capture or dom_reader
    if workflow_name == "reviewer":
        return Toolkit(
            metrics=metrics,
            model_call=_model_caller(score),
            command_runner=_command_runner(workspace),
            screenshot_capture=None,
            dom_reader=None,
            event_sink=event_sink,
            agent_turn_runner=agent_turn_runner,
        )

    # Security workflow is read-only: has command_runner for survey, but no screenshot_capture or dom_reader
    if workflow_name == "security":
        return Toolkit(
            metrics=metrics,
            model_call=_model_caller(score),
            command_runner=_command_runner(workspace),
            screenshot_capture=None,
            dom_reader=None,
            event_sink=event_sink,
            agent_turn_runner=agent_turn_runner,
        )

    # Documenter workflow is read-only: has command_runner for gather, but no screenshot_capture or dom_reader
    if workflow_name == "documenter":
        return Toolkit(
            metrics=metrics,
            model_call=_model_caller(score),
            command_runner=_command_runner(workspace),
            screenshot_capture=None,
            dom_reader=None,
            event_sink=event_sink,
            agent_turn_runner=agent_turn_runner,
        )

    # Closer workflow is write-free: no command_runner, screenshot_capture, or dom_reader
    if workflow_name == "closer":
        return Toolkit(
            metrics=metrics,
            model_call=_model_caller(score),
            command_runner=None,
            screenshot_capture=None,
            dom_reader=None,
            event_sink=event_sink,
            agent_turn_runner=agent_turn_runner,
        )

    return Toolkit(
        metrics=metrics,
        model_call=_model_caller(score),
        command_runner=_command_runner(workspace),
        screenshot_capture=_capture,
        dom_reader=read_dom,
        event_sink=event_sink,
        agent_turn_runner=agent_turn_runner,
    )


class WorkflowAdapter:
    """Runs a RoleWorkflow behind the BackendAdapter protocol."""

    def __init__(self, workflow_name: str, *, toolkit_factory=None) -> None:
        self.workflow_name = workflow_name
        self._workflow = get_workflow(workflow_name)
        self._toolkit_factory = toolkit_factory
        self._task: asyncio.Task | None = None
        self._result: WorkflowResult | None = None
        self._error: BaseException | None = None
        self._metrics = WorkflowMetrics()
        self._events: list["BackendEvent"] = []
        self._current_step: str | None = None
        #: Progress text from inner harness (spec 167 R-e).
        self._inner_progress: str | None = None
        #: Event sink callback for forwarding events
        self._event_sink = None

    # -- BackendAdapter ---------------------------------------------------

    async def start(
        self,
        stand: "Stand",
        score: "Score",
        *,
        model: str | None = None,
        effort: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._event_sink = self._on_event
        toolkit = (
            self._toolkit_factory(self._metrics, self._on_event)
            if self._toolkit_factory is not None
            else build_production_toolkit(
                score,
                metrics=self._metrics,
                event_sink=self._on_event,
                workflow_name=self.workflow_name,
                stand=stand,
                model=model,
                effort=effort,
                temperature=temperature,
                max_tokens=max_tokens,
                inner_status_recorder=self.record_inner_status,
            )
        )
        log.info(
            "workflow.start", workflow=self.workflow_name, role=getattr(score, "role", None)
        )
        self._task = asyncio.create_task(self._run(stand, score, toolkit))

    def _on_event(self, event) -> None:
        """Buffer for drain_events(), and keep get_status() honest about the
        step that is actually running (round-two review: _current_step was
        declared and never assigned)."""
        self._events.append(event)
        text = getattr(event, "text", "") or ""
        if text.startswith("qa."):
            self._current_step = text[3:]

    def set_inner_progress(self, text: str) -> None:
        """Set progress text from inner harness (spec 167 R-e).

        Called by the agent turn runner to forward the inner harness's status
        progress so the coordinare's stall watchdog sees growth.
        """
        self._inner_progress = text

    def record_inner_status(self, status: BackendStatus, backend=None) -> None:
        """Record progress and events from inner harness (spec 167 R-e).

        Called by the agent turn runner to forward the inner backend's status
        during a turn. Appends any new inner events and updates inner progress.
        """
        if status is None:
            return

        # Forward progress text (update inner progress marker)
        if status.progress:
            self.set_inner_progress(status.progress)

        # Drain and forward events from backend
        if backend is not None:
            events = backend.drain_events()
            for event in events:
                self._events.append(event)
                if self._event_sink is not None:
                    self._event_sink(event)

    async def _run(self, stand, score, toolkit) -> None:
        started = time.monotonic()
        try:
            self._result = await self._workflow.run(stand, score, toolkit)
        except BaseException as exc:  # noqa: BLE001 - recorded, re-read by get_status
            self._error = exc
            log.warning(
                "workflow.failed", workflow=self.workflow_name, error=str(exc),
                error_type=type(exc).__name__,
                total_ms=int((time.monotonic() - started) * 1000),
                step_durations_ms=dict(self._metrics.step_durations_ms),
            )
            return
        # T001b: the container's stderr is the one durable record of a live
        # run (coordinare mounts it under performer_log_dir), so the timing the
        # budgets are supposed to be measured from has to land there.
        log.info(
            "workflow.completed",
            workflow=self.workflow_name,
            total_ms=int((time.monotonic() - started) * 1000),
            step_durations_ms=dict(self._metrics.step_durations_ms),
            model_calls=self._metrics.model_calls,
            truncation_retries=self._metrics.truncation_retries,
            schema_reprompts=self._metrics.schema_reprompts,
            baseline_skipped=self._metrics.baseline_skipped,
            adopted_existing_server=self._metrics.adopted_existing_server,
        )

    def get_status(self) -> BackendStatus:
        if self._task is None:
            return BackendStatus(state="working", progress="not started")
        if not self._task.done():
            # Spec 167 R-e: forward inner progress if available (stall watchdog)
            progress = self._inner_progress or self._current_step or "running"
            return BackendStatus(state="working", progress=progress)
        if self._error is not None:
            error_msg = f"{type(self._error).__name__}: {self._error}"
            # Schema violations route to bounded retry gate (spec 098/119)
            if isinstance(self._error, SchemaViolation):
                error_msg = f"{_FORMAT_ERROR_PREFIX} {error_msg}"
            return BackendStatus(
                state="error",
                error_reason=error_msg,
            )
        if self._result is None:  # pragma: no cover - defensive
            return BackendStatus(state="error", error_reason="workflow produced no result")
        return BackendStatus(
            state="done",
            stop_reason="end_turn",
            output=json.dumps(self._report_payload()),
        )

    def _report_payload(self) -> dict[str, Any]:
        assert self._result is not None
        payload = dict(self._result.report)
        # The repair brief travels with the report so the existing response
        # builder carries it without needing to know about workflows.
        if self._result.findings:
            payload["qa_findings"] = self._result.findings
        # Round-two review: WorkflowMetrics were computed and never left the
        # adapter, so an operator could not see that a run was truncated five
        # times or spent 45s planning. They ride the report to the response.
        payload["workflow_metrics"] = dataclasses.asdict(self._metrics)
        return payload

    def drain_events(self) -> list["BackendEvent"]:
        drained, self._events = list(self._events), []
        return drained

    async def relay_feedback(self, feedback: str) -> None:
        """Workflows are non-interactive: their sequence is fixed by design.

        Mid-run feedback is exactly the coupling FR-001 rules out, so this is a
        no-op rather than a channel back into a running workflow.
        """
        log.info("workflow.relay_feedback_ignored", workflow=self.workflow_name)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected: we asked for it. Swallowed here so stop() is safe to
                # call, but NOT lumped in with real errors -- a bare
                # `except (CancelledError, Exception)` also hides genuine
                # cleanup failures and can suppress cancellation propagation.
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "workflow.stop_failed", workflow=self.workflow_name, error=str(exc)
                )

    # -- extras -----------------------------------------------------------

    @property
    def metrics(self) -> WorkflowMetrics:
        return self._metrics

    @property
    def result(self) -> WorkflowResult | None:
        return self._result
