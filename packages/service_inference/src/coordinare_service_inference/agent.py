"""LLM service-inference agent (spec 063 T011).

The agent owns the multi-turn conversation between the LLM and the read-only
sandboxed tools defined in :mod:`tools`. It is deliberately decoupled from any
particular LLM client by way of the :class:`LLMClient` protocol — production
code wires this to ``ClaudeService`` while tests inject a stubbed step
function so we can assert on budget enforcement, sandbox propagation, and
manifest validation without burning tokens.

Per plan.md the agent must enforce three budgets:

* ``max_tool_calls`` — hard cap on tool invocations across the run.
* ``max_tokens`` — passed through to the underlying LLM and surfaced in the
  budget envelope so cost telemetry can tag inference traffic separately.
* ``retry_budget`` — applied at the retry-loop layer in
  :mod:`service_inference.__init__` (T013) rather than here; the agent itself
  is a single attempt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from .schema import ServicesManifest
from .tools import SandboxViolation, ToolSandbox


class AgentError(RuntimeError):
    """Base class for agent-side failures the caller can act on."""


class ToolCallBudgetExceeded(AgentError):  # noqa: N818
    """Raised when the agent attempts more tool calls than ``max_tool_calls``."""


class IterationBudgetExceeded(AgentError):  # noqa: N818
    """Raised when the agent loops more times than ``max_iterations`` without emitting a manifest."""


class ManifestValidationError(AgentError):
    """Raised when the LLM's final structured output fails schema validation."""


class UnknownToolError(AgentError):
    """Raised when the LLM requests a tool the agent does not expose."""


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation emitted by the LLM in one conversation turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """Server-side result fed back to the LLM as a ``tool_result`` message."""

    id: str
    content: dict[str, Any]
    is_error: bool = False


@dataclass(frozen=True)
class LLMStep:
    """One step in the agent loop.

    Exactly one of ``tool_calls`` or ``manifest`` is populated. If ``manifest``
    is set, the loop exits and that value is validated against
    :class:`ServicesManifest`.

    ``input_tokens`` / ``output_tokens`` are the per-step usage reported by the
    underlying LLM. The agent sums these across steps so the orchestrator can
    emit them on the structured-log success event for cost telemetry.
    """

    tool_calls: list[ToolCall] = field(default_factory=list)
    manifest: dict[str, Any] | None = None
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    """Pluggable LLM transport.

    The agent calls ``step`` after every tool-result batch with the running
    message history; the implementation decides what model/tools/structured
    output schema to bind. Tests use a stub that walks a pre-baked list of
    steps; production wires this to Anthropic with tool definitions matching
    :class:`ToolSandbox`'s public surface.
    """

    async def step(self, messages: list[dict[str, Any]]) -> LLMStep: ...


# Public catalogue of tool names exposed to the LLM. Keeping this explicit
# means an LLM hallucination ("call_database") raises UnknownToolError rather
# than silently doing nothing.
SUPPORTED_TOOLS: frozenset[str] = frozenset(
    {"read_file", "list_dir", "which", "probe_version", "grep_repo", "web_search"}
)


@dataclass
class ServiceInferenceAgent:
    """Runs one LLM attempt to produce a :class:`ServicesManifest`."""

    sandbox: ToolSandbox
    client: LLMClient
    max_tool_calls: int = 50
    max_iterations: int = 100
    system_prompt: str = ""
    # When set, the agent stamps this value onto the manifest before validation
    # so the LLM cannot fail the run by omitting the required `agent_version`
    # field. Mirrors the prompt instruction "copy it verbatim".
    agent_version: str | None = None
    # Cumulative usage across all steps in the most recent ``run()``. Populated
    # as the loop progresses so the orchestrator can read these after a
    # successful (or failed) run for telemetry.
    tool_calls_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    async def run(self, user_prompt: str) -> ServicesManifest:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_prompt},
        ]
        self.tool_calls_used = 0
        self.input_tokens = 0
        self.output_tokens = 0

        for _ in range(self.max_iterations):
            step = await self.client.step(messages)
            self.input_tokens += step.input_tokens
            self.output_tokens += step.output_tokens

            if step.manifest is not None:
                if self.agent_version is not None and isinstance(step.manifest, dict):
                    step.manifest["agent_version"] = self.agent_version
                try:
                    manifest = ServicesManifest.model_validate(step.manifest)
                except ValidationError as exc:
                    raise ManifestValidationError(str(exc)) from exc
                self._verify_binaries_resolvable(manifest)
                return manifest

            if not step.tool_calls:
                raise AgentError(
                    "LLM step returned neither tool_calls nor manifest"
                )

            # Enforce the tool-call cap *before* dispatching so an LLM that
            # spams calls cannot exceed the budget by even one.
            if self.tool_calls_used + len(step.tool_calls) > self.max_tool_calls:
                raise ToolCallBudgetExceeded(
                    f"requested {self.tool_calls_used + len(step.tool_calls)} tool "
                    f"calls; budget is {self.max_tool_calls}"
                )

            assistant_content: list[dict[str, Any]] = []
            tool_results: list[dict[str, Any]] = []
            for call in step.tool_calls:
                assistant_content.append(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                )
                result = self._dispatch(call)
                # Anthropic's tool_result.content rejects raw dicts — it
                # requires a string or a list of content blocks. JSON-encode
                # the structured payload to a plain string; this is the
                # lowest-common-denominator shape that both Anthropic and the
                # OpenAI-compat strategy accept without per-provider branching.
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.id,
                        "content": json.dumps(result.content),
                        "is_error": result.is_error,
                    }
                )

            self.tool_calls_used += len(step.tool_calls)
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})

        raise IterationBudgetExceeded(
            f"agent exceeded max_iterations={self.max_iterations} without producing a manifest"
        )

    # ------------------------------------------------------------------ helpers

    def _verify_binaries_resolvable(self, manifest: ServicesManifest) -> None:
        """Reject manifests whose in-container service binaries cannot be resolved.

        External-required services are skipped (their binary lives in another
        container/host). Absolute paths are accepted as-is so the LLM can pin
        a known location. Otherwise we run the sandbox's ``which`` check; an
        unresolved binary is surfaced as :class:`ManifestValidationError` so the
        retry loop in the orchestrator feeds the failure back to the next attempt.
        """
        import shutil as _shutil

        missing: list[str] = []
        for svc in manifest.services:
            if svc.external_required:
                continue
            binary = svc.binary
            if binary.startswith("/"):
                # Absolute path — trust the LLM (validator will catch a bad one).
                continue
            if _shutil.which(binary) is None:
                missing.append(f"{svc.name}:{binary}")
        if missing:
            raise ManifestValidationError(
                "binary not resolvable on PATH for services: "
                + ", ".join(missing)
                + " — emit an external_required entry or a valid binary"
            )

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self, call: ToolCall) -> ToolResult:
        if call.name not in SUPPORTED_TOOLS:
            raise UnknownToolError(f"unsupported tool: {call.name!r}")
        method = getattr(self.sandbox, call.name)
        try:
            content = method(**call.arguments)
        except SandboxViolation as exc:
            # Surface the violation back to the LLM rather than crashing the
            # run — the agent can self-correct on the next turn. The reason
            # string is the only thing the LLM sees; keep it concise.
            return ToolResult(
                id=call.id,
                content={"error": "sandbox_violation", "reason": str(exc)},
                is_error=True,
            )
        except TypeError as exc:
            # Bad argument shape — also recoverable on the next turn.
            return ToolResult(
                id=call.id,
                content={"error": "bad_arguments", "reason": str(exc)},
                is_error=True,
            )
        return ToolResult(id=call.id, content=content)
