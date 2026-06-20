# Research: Completion-Style Health-Probe Mode

## D1 — Where to declare the probe mode

**Decision**: A new per-target field `health_probe: Literal["tool_call", "completion"] = "tool_call"` on `TargetDescriptor` (`proxy/routing.py`).

**Rationale**: `TargetDescriptor` is a frozen pydantic model with `model_config = ConfigDict(frozen=True, extra="forbid")` and already uses `Literal` for `wire_format`/`strategy`. A `Literal` field gives FR-006 (fail-fast on an invalid value) **for free** — pydantic raises at config-load (`RoutingTable.from_yaml_file`), and `extra="forbid"` already rejects unknown keys. Defaulting to `"tool_call"` makes the change strictly opt-in and byte-for-byte backward compatible (FR-003).

**Alternatives rejected**:
- *Overload an existing value (e.g. a sentinel in `normalizers` or `strategy`)* — conflates orthogonal concerns (probe shape vs transform/route); harder to validate and read.
- *Infer the mode from the backend/strategy* — spec explicitly forbids inference (Out of Scope); a non-tool-calling backend is an operator fact, declared.

## D2 — Completion success criterion

**Decision**: `completion` mode judges **healthy** when, after running the target's declared normalizers, the response carries **non-empty assistant content**:
- OpenAI wire: `choices[0].message.content` is a non-empty string.
- Anthropic wire (or translate): a non-empty text block in `content`.

**Rationale**: This validates the *same* success shape the non-tool-calling client (junie) requires — a usable completion — rather than a tool call. Running normalizers first mirrors the tool-call path's `_translate_round_trip_has_tool_use` (normalize → then judge), so a reasoning-only answer that `strip_reasoning`/#130 would promote into content counts as healthy (FR-004). Empty content / non-200 / timeout → unhealthy, exactly as the tool-call probe (FR-005).

**Alternatives rejected**:
- *Just "HTTP 200"* — a 200 with empty/garbled content is the very failure 098 exists for; too weak.
- *Full JSON-schema validation of the assessor contract* — couples the health gate to the junie persona contract (Out of Scope); a non-empty parseable completion is the right, minimal infra signal.

## D3 — The no-tools probe body

**Decision**: For `completion` mode, `_probe_body` emits a trivial bounded chat-completion with **no `tools`/`tool_choice`** — a single short user message (e.g. "Reply with: ok") and a small `max_tokens`. For `strategy == "translate"` it still routes a representative Anthropic body through `translate_request` (so the translate path is exercised) but omits the tool injection. `_probe_url` is unchanged (same upstream path per wire-format/strategy). `model`/`upstream_model` handling is unchanged (still addresses the real routed model so the upstream does not 404).

**Rationale**: a tools payload is exactly what makes a non-tool-calling model misbehave or refuse; removing it gives an honest completion probe. Reusing `_probe_url` + the `upstream_model` 404-avoidance keeps the probe addressing the real model.

## D4 — Gating + observability unchanged

**Decision**: `gate(target, status, reason)` is untouched — `healthy → proceed`, `unhealthy + reroute_upstream → rerouted`, else `fail_closed`, for **both** modes. The `proxy.health` log record adds `mode` (`tool_call`/`completion`) alongside the existing method/path/status/resolved_action/wire_format/strategy — no tokens, no bodies (FR-008).

**Rationale**: the fail-closed contract and auto-reroute are mode-independent; only the *probe + success criterion* differ. Adding `mode` makes the decision debuggable without leaking content.

## D5 — Scope: this unblocks 098 US2 but does not wire it

**Decision**: The code change is the probe mode only. Activating junie (the routing entry, mounting `routing.yaml` into `junie-ephemeral`, the performer image rebuild) is the deployment step this enables — not code in this spec (FR-007 / Out of Scope). The quickstart documents the activation recipe.

**Rationale**: keeps the code change minimal and testable; the config/ops activation is an operator action gated on this capability landing.
