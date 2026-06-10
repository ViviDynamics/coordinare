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

from .normalizers import NORMALIZER_REGISTRY
from .routing import TargetDescriptor
from .translate import translate_request, translate_response

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


#: A representative Anthropic ``/v1/messages`` probe body. For the ``translate``
#: strategy this is routed THROUGH :func:`translate_request` so the probe
#: exercises the exact request-translation path a real card would (quickstart S6).
_ANTHROPIC_PROBE_BODY = {
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


def _probe_url(target: TargetDescriptor) -> str:
    base = target.base_url.rstrip("/")
    if target.strategy == "translate":
        # The translate shim rewrites the Anthropic ``/v1/messages`` front door
        # to the OpenAI ``/v1/chat/completions`` upstream; the probe forwards to
        # the same upstream path the live request-translation path would.
        # base_url may already end with ``/v1`` (e.g. Ollama-direct configs);
        # avoid double-appending it.
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"
    if target.wire_format == "anthropic":
        return f"{base}/v1/messages"
    return f"{base}/chat/completions"


def _probe_body(target: TargetDescriptor, model: str | None) -> dict[str, Any]:
    # The probe MUST address the real routed model: Ollama-direct (and most
    # OpenAI-compatible servers) validate the ``model`` field and answer an
    # unknown name with HTTP 404, which would gate every routed path unhealthy at
    # startup. ``model`` is the ``(backend, model)`` routing key being gated; fall
    # back to the placeholder only when a caller does not supply one.
    model_name = model or "probe"
    # 084: if the target declares an upstream_model, the routing key (model_name)
    # is a valid Anthropic name (e.g. claude-sonnet-4-5) that the client CLI
    # accepts, but the upstream (e.g. Ollama) only knows the real model name.
    # Use upstream_model for the probe body so the upstream doesn't 404.
    upstream_model_name = target.upstream_model or model_name
    if target.strategy == "translate":
        # Route a representative Anthropic body through the real request
        # translator, then ensure the tools survive so the upstream is actually
        # asked to emit a structured call. ``translate_request`` does not yet map
        # ``tools`` (US2/T016), so ``setdefault`` injects the probe tool now and
        # becomes a no-op once tool translation lands — keeping the probe honest
        # either way.
        body = translate_request({**_ANTHROPIC_PROBE_BODY, "model": model_name})
        body["model"] = upstream_model_name
        body.setdefault("tools", [_PROBE_TOOL])
        body.setdefault("tool_choice", "auto")
        return body
    if target.wire_format == "anthropic":
        return {**_ANTHROPIC_PROBE_BODY, "model": model_name}
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": "Call the ping tool."}],
        "tools": [_PROBE_TOOL],
        "tool_choice": "auto",
    }


def _has_anthropic_tool_use(body: dict[str, Any]) -> bool:
    """True iff an Anthropic ``/v1/messages`` body carries a ``tool_use`` block."""
    content = body.get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def _translate_round_trip_has_tool_use(
    target: TargetDescriptor, openai_body: dict[str, Any]
) -> bool:
    """Run the upstream OpenAI reply through the full response path and check it.

    Mirrors the live translate response path (Decision 4): the upstream OpenAI
    bytes are first passed through the declared, format-keyed normalizers (e.g.
    ``harmony_tool_calls`` reassembles a leaked call), THEN wire-format translated
    to an Anthropic body, and finally checked for a structured ``tool_use`` block.
    A harmony/reasoning leak with no matching normalizer keeps the call in
    free-text ``content`` → no ``tool_use`` survives → gates unhealthy.
    """
    body = openai_body
    for key in target.normalizers:
        normalizer = NORMALIZER_REGISTRY.get(key)
        if normalizer is not None:
            body = normalizer.normalize_json(body)
    anthropic_body = translate_response(body)
    return _has_anthropic_tool_use(anthropic_body)


def _has_structured_tool_call(target: TargetDescriptor, body: dict[str, Any]) -> bool:
    """True iff the probe response carries a structured tool call.

    A leaked harmony/reasoning path (the 077 openclaw failure) puts the call in
    free-text ``content`` and emits no structured ``tool_calls`` / ``tool_use``
    block — that reads as no structured call and gates unhealthy.
    """
    if target.strategy == "translate":
        return _translate_round_trip_has_tool_use(target, body)
    if target.wire_format == "anthropic":
        return _has_anthropic_tool_use(body)
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
    model: str | None = None,
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
                _probe_url(target), json=_probe_body(target, model), timeout=timeout
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
