"""LLM-driven service inference pass for env-cache bootstrap (spec 063).

This module orchestrates the agent → templater → validator loop with bounded
retries. On success it drops `services.json` and the three rendered shell
scripts into ``<output_root>/services/``. On retry-budget exhaustion it writes
``services.json.rejected`` next to where the success artifact would have lived
and raises :class:`InferenceFailed` — env-cache build callers turn this into
the operator-visible failure message described in plan.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from jinja2 import TemplateError, UndefinedError

from coordinare.services.service_inference.agent import (
    AgentError,
    LLMClient,
    ServiceInferenceAgent,
)
from coordinare.services.service_inference.manual_override import SERVICES_SUBDIR
from coordinare.services.service_inference.prompt import render_system_prompt
from coordinare.services.service_inference.schema import (
    ServiceEntry,
    ServicesManifest,
)
from coordinare.services.service_inference.templater import render
from coordinare.services.service_inference.tools import ToolSandbox
from coordinare.services.service_inference.validator import (
    ValidationResult,
    validate,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "REJECTED_FILENAME",
    "InferenceFailed",
    "InferenceResult",
    "RejectedManifest",
    "ServiceEntry",
    "ServicesManifest",
    "infer_services",
]


_log = structlog.get_logger(__name__)

REJECTED_FILENAME = "services.json.rejected"


class InferenceFailed(RuntimeError):  # noqa: N818
    """Raised when the retry budget is exhausted without a passing manifest.

    ``rejected_path`` is the absolute path of the ``services.json.rejected``
    file written for operator inspection. ``attempts`` is the per-attempt log
    of agent/validator outcomes, preserved so the dashboard env-cache panel
    can show the operator exactly what the agent tried and why it failed.
    """

    def __init__(
        self,
        message: str,
        *,
        rejected_path: Path,
        attempts: list[dict[str, Any]],
    ) -> None:
        super().__init__(message)
        self.rejected_path = rejected_path
        self.attempts = attempts


@dataclass(frozen=True)
class RejectedManifest:
    """Snapshot of the last attempt, written to disk on budget exhaustion."""

    manifest: dict[str, Any] | None
    validation_summary: str | None
    agent_error: str | None
    attempts: list[dict[str, Any]]


@dataclass(frozen=True)
class InferenceResult:
    manifest: ServicesManifest
    scripts_dir: Path
    attempts: int


async def infer_services(
    *,
    project_root: Path,
    output_root: Path,
    agent_version: str,
    llm_client: LLMClient,
    retry_budget: int = 3,
    max_tool_calls: int = 50,
    web_search_enabled: bool = False,
    run_validation: bool = True,
) -> InferenceResult:
    """Run the agent → validate loop and drop env-cache artifacts on success.

    On each attempt:
      1. The agent produces a candidate :class:`ServicesManifest`.
      2. The templater renders start/stop/health shell scripts.
      3. The validator runs the scripts (start → health → stop).
      4. On validator failure, the validator's summary is appended to the next
         attempt's user prompt as a recovery hint.

    On exhaustion the function writes ``services.json.rejected`` to
    ``<output_root>/services/`` and raises :class:`InferenceFailed`.
    """
    if retry_budget < 1:
        raise ValueError("retry_budget must be at least 1")

    sandbox = ToolSandbox.for_root(project_root, web_search_enabled=web_search_enabled)
    system_prompt = render_system_prompt(
        agent_version, grep_max_lines=sandbox.grep_max_lines
    )
    base_prompt = (
        "Inspect the project at the sandbox root and emit a ServicesManifest "
        "describing every supportive service the project needs at test/runtime."
    )

    attempts: list[dict[str, Any]] = []
    last_manifest_raw: dict[str, Any] | None = None
    last_validation: ValidationResult | None = None
    last_agent_error: str | None = None
    recovery_hint: str | None = None

    for attempt_idx in range(1, retry_budget + 1):
        user_prompt = base_prompt
        if recovery_hint is not None:
            user_prompt = (
                f"{base_prompt}\n\n"
                "The previous attempt's services failed validation. Use this "
                "diagnostic to refine the manifest:\n"
                f"{recovery_hint}"
            )

        agent = ServiceInferenceAgent(
            sandbox=sandbox,
            client=llm_client,
            max_tool_calls=max_tool_calls,
            system_prompt=system_prompt,
        )

        attempt_log: dict[str, Any] = {"attempt": attempt_idx}

        try:
            manifest = await agent.run(user_prompt)
        except AgentError as exc:
            last_agent_error = f"{type(exc).__name__}: {exc}"
            attempt_log["agent_error"] = last_agent_error
            attempt_log["tool_call_count"] = agent.tool_calls_used
            attempt_log["input_tokens"] = agent.input_tokens
            attempt_log["output_tokens"] = agent.output_tokens
            attempts.append(attempt_log)
            _log.warning(
                "service_inference_agent_error",
                attempt=attempt_idx,
                error=last_agent_error,
                agent_version=agent_version,
                tool_call_count=agent.tool_calls_used,
                input_tokens=agent.input_tokens,
                output_tokens=agent.output_tokens,
            )
            recovery_hint = (
                f"Previous attempt raised {last_agent_error}. Emit a manifest "
                "that satisfies the ServicesManifest schema this time."
            )
            continue

        last_manifest_raw = manifest.model_dump()
        last_agent_error = None
        attempt_log["manifest"] = last_manifest_raw
        attempt_log["tool_call_count"] = agent.tool_calls_used
        attempt_log["input_tokens"] = agent.input_tokens
        attempt_log["output_tokens"] = agent.output_tokens

        if not run_validation:
            scripts_dir = _write_success_artifacts(output_root, manifest, render(manifest))
            attempts.append(attempt_log)
            _log.info(
                "service_inference_succeeded",
                attempt=attempt_idx,
                agent_version=agent_version,
                services=[s.name for s in manifest.services],
                validation_skipped=True,
                tool_call_count=agent.tool_calls_used,
                input_tokens=agent.input_tokens,
                output_tokens=agent.output_tokens,
                retries=attempt_idx - 1,
            )
            return InferenceResult(
                manifest=manifest, scripts_dir=scripts_dir, attempts=attempt_idx
            )

        try:
            scripts = render(manifest)
        except (UndefinedError, TemplateError) as exc:
            attempt_log["render_error"] = str(exc)
            attempts.append(attempt_log)
            recovery_hint = (
                f"Templating the previous manifest failed: {exc}. "
                "Check that every required field is populated."
            )
            _log.warning(
                "service_inference_render_failed",
                attempt=attempt_idx,
                error=str(exc),
                agent_version=agent_version,
                tool_call_count=agent.tool_calls_used,
                input_tokens=agent.input_tokens,
                output_tokens=agent.output_tokens,
            )
            continue

        last_validation = validate(scripts, working_dir=None)
        attempt_log["validation_phase"] = last_validation.phase
        attempt_log["validation_ok"] = last_validation.ok
        attempt_log["validation_returncode"] = last_validation.returncode

        if last_validation.ok:
            scripts_dir = _write_success_artifacts(output_root, manifest, scripts)
            attempts.append(attempt_log)
            if not manifest.cache_inputs:
                # Phase 3 keys cache invalidation off cache_inputs; an empty
                # list means every future bootstrap on this project will
                # cache-miss in compute_inference_cache_key (the sentinel
                # marker forces a miss). Surface so operators can correct it.
                _log.warning(
                    "service_inference_empty_cache_inputs",
                    attempt=attempt_idx,
                    agent_version=agent_version,
                    note="every subsequent bootstrap will cache-miss; verify the agent is reading project manifests",
                )
            _log.info(
                "service_inference_succeeded",
                attempt=attempt_idx,
                agent_version=agent_version,
                services=[s.name for s in manifest.services],
                tool_call_count=agent.tool_calls_used,
                input_tokens=agent.input_tokens,
                output_tokens=agent.output_tokens,
                retries=attempt_idx - 1,
            )
            return InferenceResult(
                manifest=manifest, scripts_dir=scripts_dir, attempts=attempt_idx
            )

        attempts.append(attempt_log)
        recovery_hint = last_validation.summary
        _log.warning(
            "service_inference_validation_failed",
            attempt=attempt_idx,
            phase=last_validation.phase,
            returncode=last_validation.returncode,
            agent_version=agent_version,
            tool_call_count=agent.tool_calls_used,
            input_tokens=agent.input_tokens,
            output_tokens=agent.output_tokens,
        )

    # Exhausted: write rejected manifest, log, raise.
    rejected = RejectedManifest(
        manifest=last_manifest_raw,
        validation_summary=last_validation.summary if last_validation else None,
        agent_error=last_agent_error,
        attempts=attempts,
    )
    rejected_path = _write_rejected_manifest(output_root, rejected, agent_version)
    total_tool_calls = sum(a.get("tool_call_count", 0) for a in attempts)
    total_input_tokens = sum(a.get("input_tokens", 0) for a in attempts)
    total_output_tokens = sum(a.get("output_tokens", 0) for a in attempts)
    _log.error(
        "service_inference_rejected",
        agent_version=agent_version,
        attempts=len(attempts),
        rejected_path=str(rejected_path),
        last_validation_phase=last_validation.phase if last_validation else None,
        last_agent_error=last_agent_error,
        tool_call_count=total_tool_calls,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        retries=len(attempts) - 1,
    )
    raise InferenceFailed(
        f"service inference exhausted retry_budget={retry_budget}; "
        f"see {rejected_path} for the last attempt",
        rejected_path=rejected_path,
        attempts=attempts,
    )


def _write_success_artifacts(
    output_root: Path, manifest: ServicesManifest, scripts: Any
) -> Path:
    scripts_dir = output_root / SERVICES_SUBDIR
    scripts_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "services.json").write_text(
        manifest.model_dump_json(indent=2) + "\n"
    )
    for name, body in (
        ("services-start.sh", scripts.start),
        ("services-stop.sh", scripts.stop),
        ("services-health.sh", scripts.health),
    ):
        path = scripts_dir / name
        path.write_text(body)
        path.chmod(0o755)
    # Sidecar listing the paths whose contents drive cache invalidation (the
    # SHA of these files composes the inference cache key). Operators looking
    # at the env-cache directory shouldn't have to parse services.json to see
    # which project files the agent actually depended on.
    sidecar = "\n".join(manifest.cache_inputs) + ("\n" if manifest.cache_inputs else "")
    (scripts_dir / "cache_manifest.txt").write_text(sidecar)
    # If a prior run's rejected file is sitting here, clean it up.
    stale = scripts_dir / REJECTED_FILENAME
    if stale.exists():
        stale.unlink()
    return scripts_dir


def _write_rejected_manifest(
    output_root: Path, rejected: RejectedManifest, agent_version: str
) -> Path:
    scripts_dir = output_root / SERVICES_SUBDIR
    scripts_dir.mkdir(parents=True, exist_ok=True)
    rejected_path = scripts_dir / REJECTED_FILENAME
    payload = {
        "agent_version": agent_version,
        "manifest": rejected.manifest,
        "validation_summary": rejected.validation_summary,
        "agent_error": rejected.agent_error,
        "attempts": rejected.attempts,
    }
    rejected_path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return rejected_path
