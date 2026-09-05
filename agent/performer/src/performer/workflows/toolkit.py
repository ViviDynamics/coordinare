"""The execution primitives a workflow is allowed to use (spec 164 FR-002).

A workflow reaches the outside world ONLY through this object.  A workflow that
imports ``subprocess`` or ``httpx`` directly is a contract violation
(contracts/role_workflow.md), because the guarantees below — real exit codes,
verified screenshot paths, budget enforcement, schema enforcement — are what
make a step's output trustworthy, and a step that bypasses them is unverifiable.

Everything here is injectable so steps are testable on the host with no
container and no gateway.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel

from performer.backends import get_backend as _get_backend
from performer.workflows.budget import Budget, CallCeiling, ModelReply, call_with_budget
from performer.workflows.models import ExecutedCheck
from performer.workflows.schema_guard import schema_instruction, validate_with_reprompt

if TYPE_CHECKING:
    from performer.models import BackendEvent
    from performer.workflows.base import WorkflowMetrics

M = TypeVar("M", bound=BaseModel)

#: (cmd, cwd, timeout) -> (exit_code, output)
CommandRunner = Callable[[str, Any, int], Awaitable[tuple[int, str]]]
#: (persona, content, max_tokens) -> ModelReply
ModelCall = Callable[[str, list[dict], int], Awaitable[ModelReply]]


class Toolkit:
    """Execution primitives handed to a workflow's steps."""

    def __init__(
        self,
        *,
        metrics: "WorkflowMetrics",
        model_call: ModelCall | None = None,
        command_runner: CommandRunner | None = None,
        screenshot_capture: Callable[..., str | None] | None = None,
        dom_reader: Callable[[str], list[dict]] | None = None,
        event_sink: Callable[["BackendEvent"], None] | None = None,
        call_limit: int = 12,
    ) -> None:
        self.metrics = metrics
        self._model_call = model_call
        self._command_runner = command_runner
        self._screenshot_capture = screenshot_capture
        self._dom_reader = dom_reader
        self._events: list["BackendEvent"] = []
        self._event_sink = event_sink
        self._ceiling = CallCeiling(limit=call_limit)

    # -- commands ---------------------------------------------------------

    async def run_command(
        self,
        cmd: str,
        *,
        cwd: Path | None = None,
        timeout_s: int = 300,
        plan_check_id: str = "",
    ) -> ExecutedCheck:
        """Run *cmd* and return its real result.

        Success is derived from the exit code and nothing else.  Output that
        claims success while the process failed does not change the verdict.
        """
        if self._command_runner is None:  # pragma: no cover - wiring error
            raise RuntimeError("Toolkit has no command runner configured")
        exit_code, output = await self._command_runner(cmd, cwd, timeout_s)
        return ExecutedCheck.from_result(
            command=cmd, exit_code=exit_code, output=output, plan_check_id=plan_check_id
        )

    # -- captures ---------------------------------------------------------

    async def capture_screenshot(self, *, url: str, path: Path | None) -> str | None:
        """Capture a screenshot, returning a VERIFIED path or None.

        Never returns a path it did not confirm on disk — an invented path is
        worse than none, and is auto-rejected by the evidence floor downstream.
        """
        if self._screenshot_capture is None:  # pragma: no cover - wiring error
            raise RuntimeError("Toolkit has no screenshot capture configured")
        return self._screenshot_capture(url=url, out_path=str(path) if path else None)

    async def dom_snapshot(self, url: str) -> list[dict]:
        """Read elements from the DOM.

        Labels come from here, never from a model (FR-011).
        """
        if self._dom_reader is None:  # pragma: no cover - wiring error
            raise RuntimeError("Toolkit has no DOM reader configured")
        result = self._dom_reader(url)
        # A real browser reader must be async: Playwright's sync API refuses to
        # run inside a running event loop, which is where every step lives.
        # Sync readers stay supported so tests need no browser.
        if inspect.isawaitable(result):
            return await result
        return result

    # -- model ------------------------------------------------------------

    async def call_model(
        self,
        *,
        persona: str,
        schema: type[M],
        content: list[dict],
        budget: Budget,
    ) -> M:
        """One scoped model call, budgeted and schema-checked.

        Consumes one unit of the run's call ceiling BEFORE dispatching, so a
        looping workflow fails loudly rather than hammering a gateway that
        serves every role.
        """
        if self._model_call is None:  # pragma: no cover - wiring error
            raise RuntimeError("Toolkit has no model call configured")

        # The model is told the schema up front. Enforcing a shape the model was
        # never shown produces confidently wrong field names -- which is exactly
        # how the first live eval run failed.
        base_persona = f"{persona}\n\n{schema_instruction(schema)}"

        async def _attempt(correction: str | None) -> str:
            effective_persona = (
                base_persona if correction is None else f"{persona}\n\n{correction}"
            )

            async def _budgeted(max_tokens: int) -> ModelReply:
                # Every REAL request counts. Consuming once per call_model let a
                # truncation retry plus a schema reprompt (each of which may
                # retry) make up to four requests per unit -- a ceiling of 12
                # permitted 48 against a gateway shared by every role.
                self._ceiling.consume()
                return await self._model_call(effective_persona, content, max_tokens)

            reply = await call_with_budget(_budgeted, budget, self.metrics)
            return reply.content

        return await validate_with_reprompt(_attempt, schema, self.metrics)

    # -- backends and events ---------------------------------------------

    def get_backend(self, name: str):
        """An agent turn, for steps that genuinely need one."""
        return _get_backend(name)

    def emit(self, event: "BackendEvent") -> None:
        """Publish a step transition (FR-008: visibility without control)."""
        self._events.append(event)
        if self._event_sink is not None:
            self._event_sink(event)

    @property
    def events(self) -> list["BackendEvent"]:
        return list(self._events)
