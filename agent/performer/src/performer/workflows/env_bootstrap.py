"""174: bounded harness installation followed by deterministic gates and repair."""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from performer.models import BackendEvent, BackendEventType
from performer.workflows.base import WorkflowResult

if TYPE_CHECKING:
    from performer.models import Score, Stand
    from performer.workflows.toolkit import Toolkit


class BootstrapRun(BaseModel):
    """Strict terminal report; the harness output is never interpreted as this."""

    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["complete", "error"]
    reason: str = Field(max_length=1000)
    repairs: int = Field(ge=0, le=2)
    inference: dict[str, Any]


def _budget(env: dict[str, str], key: str, default: int, low: int, high: int) -> int:
    value = int(env.get(key, str(default)))
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high}")
    return value


#: 343: ordered steps. env_bootstrap emits them with the workflow prefix
#: retained ("env_bootstrap.install"), unlike the role workflows, and the
#: adapter's pre-existing latch depends on that; see WorkflowBackendAdapter.
STEPS: tuple[str, ...] = (
    "snapshot", "install", "repair", "integrity", "inference", "readiness",
    "verify", "report",
)


class EnvBootstrapWorkflow:
    """Keep the install harness; bound retries and never trust its success claim."""

    name = "env_bootstrap"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STEPS

    async def run(self, stand: Stand, score: Score, toolkit: Toolkit) -> WorkflowResult:
        inference: dict[str, Any] = {}
        repairs = 0

        async def step(name: str) -> dict[str, Any]:
            toolkit.emit(BackendEvent(type=BackendEventType.progress, text=f"env_bootstrap.{name}"))
            started = time.monotonic()
            try:
                return await toolkit.run_bootstrap_step(name)
            finally:
                durations = toolkit.metrics.step_durations_ms
                durations[name] = durations.get(name, 0) + int((time.monotonic() - started) * 1000)

        def report(status: str, reason: str = "") -> WorkflowResult:
            toolkit.metrics.reached_green = status == "complete"
            toolkit.emit(BackendEvent(type=BackendEventType.progress, text=f"env_bootstrap.report {status}"))
            return WorkflowResult(report={"env_bootstrap_run": {
                "status": status, "reason": reason[-1000:], "repairs": repairs,
                "inference": inference,
            }}, metrics=toolkit.metrics)

        try:
            max_repairs = _budget(score.workflow_env, "ENV_BOOTSTRAP_MAX_REPAIRS", 1, 0, 2)
            timeout = _budget(score.workflow_env, "ENV_BOOTSTRAP_TIMEOUT_SECONDS", 1800, 1, 3600)
            async with asyncio.timeout(timeout):
                snapshot = await step("snapshot")
                if not snapshot["passed"]:
                    return report("error", snapshot["reason"])
                feedback = ""
                for attempt in range(max_repairs + 1):
                    kind = "install" if attempt == 0 else "repair"
                    repairs = attempt
                    toolkit.emit(BackendEvent(type=BackendEventType.progress, text=f"env_bootstrap.{kind}"))
                    started = time.monotonic()
                    try:
                        turn = await toolkit.run_agent_turn({
                            "kind": kind,
                            "persona": score.persona_instructions + (
                                "\n\nRepair only the failed environment checks. Preserve coordinare-owned "
                                "verify.sh and activate.sh. Failure details:\n" + feedback if attempt else ""
                            ),
                        }, timeout_s=timeout)
                    finally:
                        durations = toolkit.metrics.step_durations_ms
                        durations[kind] = durations.get(kind, 0) + int((time.monotonic() - started) * 1000)
                    integrity = await step("integrity")
                    if not integrity["passed"]:
                        return report("error", integrity["reason"])
                    if turn["exit_state"] != "done":
                        return report("error", turn.get("output_tail") or "install turn failed")
                    inferred = await step("inference")
                    inference = inferred.get("inference", {})
                    checked = await step("readiness")
                    integrity = await step("integrity")
                    if not integrity["passed"]:
                        return report("error", integrity["reason"])
                    if checked["passed"]:
                        checked = await step("verify")
                    if checked.get("integrity_failure"):
                        return report("error", checked["reason"])
                    if checked["passed"]:
                        return report("complete")
                    feedback = checked.get("reason") or "environment verification failed"
                return report("error", "repair budget exhausted: " + feedback)
        except TimeoutError:
            return report("error", "bootstrap workflow timeout")
        except (ValueError, OSError) as exc:
            return report("error", str(exc))
