"""078 US3 — startup health / smoke-test gating (T024, T025).

On startup the resolved self-hosted path is smoke-tested with a *bounded*
tool-calling probe before any card is accepted. The probe is the only
fail-*closed* surface in the layer (normalizers fail-open, FR-078-9): a broken
or wedged upstream surfaces as ``unhealthy`` and is gated per FR-078-5 rather
than black-holing a card mid-lifecycle.

The probe is timeout-bounded (the 077 spark/qwen runner-wedge edge): a hung
upstream raises a timeout that we map to ``unhealthy`` instead of hanging
startup. Connection refusal (the 077 hermes can't-connect edge) and a non-200
likewise surface as ``unhealthy``.

Gating state machine (:func:`gate`, FR-078-5, US3):

* ``healthy`` → ``proceed`` (routing gated open).
* ``unhealthy`` + ``reroute_upstream`` declared → ``rerouted`` (auto-reroute,
  the Ollama-direct fix; the job proceeds against the clean upstream).
* ``unhealthy`` + no ``reroute_upstream`` → ``fail_closed`` (clear error, card
  not accepted).
* An ``unhealthy`` target NEVER resolves to ``proceed`` — that would be
  fail-open onto a known-broken path.

Observability (FR-078-10): the decision is summarized to method/path/status and
the resolved action; never tokens or bodies.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import httpx
import structlog

from .routing import TargetDescriptor

_log = structlog.get_logger(__name__)

HealthStatus = Literal["healthy", "unhealthy"]
ResolvedAction = Literal["proceed", "rerouted", "fail_closed"]

# A trivial tool-calling probe: a model that genuinely emits structured
# tool_calls answers it with a call; a broken harmony/reasoning path answers
# with leaked content and no tool_calls (the 077 openclaw failure shape).
_DEFAULT_TIMEOUT = 10.0
_PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "ping",
        "description": "Health probe. Call this with no arguments.",
        "parameters": {"type": "object", "properties": {}},
    },
}


@dataclass(frozen=True)
class HealthResult:
    """Outcome of the startup smoke probe for a target; gates routing."""

    target: TargetDescriptor
    status: HealthStatus
    reason: str | None
    resolved_action: ResolvedAction


def gate(
    target: TargetDescriptor, status: HealthStatus, reason: str | None
) -> HealthResult:
    """Pure gating decision (no probe, no network) — the FR-078-5 state machine.

    ``healthy`` proceeds; an ``unhealthy`` target auto-reroutes when a
    ``reroute_upstream`` is declared, else fails closed. An ``unhealthy`` target
    NEVER resolves to ``proceed`` (no fail-open onto a known-broken path).
    """
    if status == "healthy":
        return HealthResult(
            target=target,
            status="healthy",
            reason=reason,
            resolved_action="proceed",
        )

    if target.reroute_upstream:
        action: ResolvedAction = "rerouted"
    else:
        action = "fail_closed"
    return HealthResult(
        target=target,
        status="unhealthy",
        reason=reason,
        resolved_action=action,
    )


def _probe_url(target: TargetDescriptor) -> str:
    base = target.base_url.rstrip("/")
    if target.wire_format == "anthropic":
        return f"{base}/v1/messages"
    return f"{base}/chat/completions"


def _probe_body(target: TargetDescriptor) -> dict[str, Any]:
    if target.wire_format == "anthropic":
        return {
            "model": "probe",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "Call the ping tool."}],
            "tools": [
                {
                    "name": "ping",
                    "description": "Health probe. Call this with no arguments.",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        }
    return {
        "model": "probe",
        "messages": [{"role": "user", "content": "Call the ping tool."}],
        "tools": [_PROBE_TOOL],
        "tool_choice": "auto",
    }


def _has_structured_tool_call(target: TargetDescriptor, body: dict[str, Any]) -> bool:
    """True iff the probe response carries a structured tool call.

    A leaked harmony/reasoning path (the 077 openclaw failure) puts the call in
    free-text ``content`` and emits no structured ``tool_calls`` / ``tool_use``
    block — that reads as no structured call and gates unhealthy.
    """
    if target.wire_format == "anthropic":
        content = body.get("content")
        return isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return False
    tool_calls = message.get("tool_calls")
    return isinstance(tool_calls, list) and len(tool_calls) > 0


async def check_health(
    target: TargetDescriptor,
    *,
    client: httpx.AsyncClient | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> HealthResult:
    """Run the bounded tool-calling smoke probe, then gate the result.

    A 200 carrying a structured tool call is ``healthy``. A timeout (wedged
    upstream), connection error (can't connect), non-200, or a 200 lacking a
    structured tool call (harmony/reasoning leak) is ``unhealthy``. The result
    is then passed through :func:`gate` for the proceed/reroute/fail-closed
    decision.
    """
    owns_client = client is None
    if client is None:
        # The per-request ``timeout`` below bounds every probe (owned or
        # injected); the constructor default would be redundant with it.
        client = httpx.AsyncClient()
    status: HealthStatus
    reason: str | None
    try:
        try:
            response = await client.post(
                _probe_url(target), json=_probe_body(target), timeout=timeout
            )
        except httpx.TimeoutException:
            status, reason = "unhealthy", f"probe timed out after {timeout}s"
        except httpx.HTTPError as exc:
            status, reason = "unhealthy", f"probe connection error: {exc!r}"
        else:
            if response.status_code != 200:
                status = "unhealthy"
                reason = f"probe returned HTTP {response.status_code}"
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and _has_structured_tool_call(
                    target, payload
                ):
                    status, reason = "healthy", None
                else:
                    status = "unhealthy"
                    reason = "probe response carried no structured tool_calls"
    finally:
        if owns_client:
            await client.aclose()

    result = gate(target, status, reason)
    _log.info(
        "proxy.health",
        method="POST",
        path=_probe_url(target),
        status=result.status,
        resolved_action=result.resolved_action,
        wire_format=target.wire_format,
        strategy=target.strategy,
    )
    return result
