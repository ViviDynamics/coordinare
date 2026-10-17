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
    # 265: which script decided the verify ("daemon_rendered" or "cache_file"); None when it did not pass.
    verify_basis: str | None = Field(default=None, max_length=32)
    environment_verification: dict[str, Any] | None = None


def _budget(env: dict[str, str], key: str, default: int, low: int, high: int) -> int:
    value = int(env.get(key, str(default)))
    if not low <= value <= high:
        reason = f"{key} must be between {low} and {high}"
        raise ValueError(reason)
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
    steps = STEPS

    async def run(self, stand: Stand, score: Score, toolkit: Toolkit) -> WorkflowResult:
        return await _BootstrapSession(score, toolkit).run()


class _BootstrapSession:
    """One bounded installation/repair sequence and its parent-collected evidence."""

    def __init__(self, score: Score, toolkit: Toolkit) -> None:
        self.score = score
        self.toolkit = toolkit
        self.inference: dict[str, Any] = {}
        self.repairs = 0
        self.verification: dict[str, Any] | None = None

    async def step(self, name: str) -> dict[str, Any]:
        self.toolkit.emit(BackendEvent(type=BackendEventType.progress, text=f"env_bootstrap.{name}"))
        started = time.monotonic()
        try:
            return await self.toolkit.run_bootstrap_step(name)
        finally:
            self._duration(name, started)

    def _duration(self, name: str, started: float) -> None:
        durations = self.toolkit.metrics.step_durations_ms
        durations[name] = durations.get(name, 0) + int((time.monotonic() - started) * 1000)

    def report(self, status: str, reason: str = "", verify_basis: str | None = None) -> WorkflowResult:
        self.toolkit.metrics.reached_green = status == "complete"
        self.toolkit.emit(BackendEvent(type=BackendEventType.progress, text=f"env_bootstrap.report {status}"))
        return WorkflowResult(report={"env_bootstrap_run": {
            "status": status, "reason": reason[-1000:], "repairs": self.repairs,
            "inference": self.inference, "verify_basis": verify_basis,
            "environment_verification": self.verification,
        }}, metrics=self.toolkit.metrics)

    async def _install(self) -> dict[str, Any]:
        installed = await self.step("install")
        self.verification = installed.get("environment_verification")
        if not installed["passed"] or installed.get("integrity_failure"):
            return installed
        checked = await self.step("verify")
        self.verification = checked.get("environment_verification")
        return checked

    async def _repair(self, attempt: int, feedback: str, execution_budget_seconds: int) -> dict[str, Any]:
        kind = "install" if attempt == 0 else "repair"
        self.toolkit.emit(BackendEvent(type=BackendEventType.progress, text=f"env_bootstrap.{kind}"))
        started = time.monotonic()
        try:
            turn = await self.toolkit.run_agent_turn({
                "kind": kind,
                "persona": self.score.persona_instructions + (
                    "\n\nRepair only the failed environment checks. Preserve coordinare-owned "
                    "verify.sh and activate.sh. Failure details:\n" + feedback if attempt else ""
                ),
            }, timeout_s=execution_budget_seconds)
        finally:
            self._duration(kind, started)
        integrity = await self.step("integrity")
        if not integrity["passed"]:
            return {**integrity, "terminal": True}
        if turn["exit_state"] != "done":
            return {"passed": False, "reason": turn.get("output_tail") or "install turn failed", "terminal": True}
        inferred = await self.step("inference")
        self.inference = inferred.get("inference", {})
        checked = await self.step("readiness")
        integrity = await self.step("integrity")
        if not integrity["passed"]:
            return {**integrity, "terminal": True}
        if checked["passed"]:
            checked = await self.step("verify")
            self.verification = checked.get("environment_verification")
        return checked

    async def _execute(self, max_repairs: int, execution_budget_seconds: int) -> WorkflowResult:
        snapshot = await self.step("snapshot")
        if not snapshot["passed"]:
            return self.report("error", snapshot["reason"])
        feedback = ""
        for attempt in range(max_repairs + 1):
            self.repairs = attempt
            checked = (await self._install() if attempt == 0 and self.score.environment_manifest is not None
                       else await self._repair(attempt, feedback, execution_budget_seconds))
            if checked.get("integrity_failure") or checked.get("terminal"):
                return self.report("error", checked["reason"])
            if checked["passed"]:
                return self.report("complete", verify_basis=checked.get("verify_basis"))
            feedback = checked.get("reason") or "environment verification failed"
        return self.report("error", "repair budget exhausted: " + feedback)

    async def run(self) -> WorkflowResult:
        try:
            max_repairs = _budget(self.score.workflow_env, "ENV_BOOTSTRAP_MAX_REPAIRS", 1, 0, 2)
            timeout = _budget(self.score.workflow_env, "ENV_BOOTSTRAP_TIMEOUT_SECONDS", 1800, 1, 3600)
            async with asyncio.timeout(timeout):
                return await self._execute(max_repairs, timeout)
        except TimeoutError:
            return self.report("error", "bootstrap workflow timeout")
        except (ValueError, OSError) as exc:
            return self.report("error", str(exc))
