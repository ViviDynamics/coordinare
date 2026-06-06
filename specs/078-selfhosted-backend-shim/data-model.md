# Phase 1 Data Model: Self-Hosted Backend Robustness Layer

All entities are in-process config/runtime models (pydantic 2.x where validated).
No persisted coordinare state. Field names are normative for the contract check.

## RoutingTable

The config surface mapping `(backend, model)` → self-hosted target. Decoupled
from per-backend config. A missing entry means native-cloud (no-op, FR-078-1/4).

| Field | Type | Notes |
|---|---|---|
| `entries` | `list[RoutingEntry]` | resolved into a `(backend, model)` → `TargetDescriptor` lookup |

**Behavior**: `resolve(backend, model) -> TargetDescriptor | None`. Returns `None`
(→ no-op, proxy not launched) when no entry matches. Backend name normalized
(kebab→snake, cf. 080 `test_kebab_case_backend_normalized`).

## RoutingEntry

One row of the routing table.

| Field | Type | Validation |
|---|---|---|
| `backend` | `str` | required; normalized backend id (e.g. `openclaw`, `claude_code`) |
| `model` | `str` | required; the self-hosted model the backend is pointed at (e.g. `gpt-oss:120b`) |
| `target` | `TargetDescriptor` | required |

## TargetDescriptor

A resolved self-hosted upstream a `(backend, model)` pair is routed at.

| Field | Type | Validation |
|---|---|---|
| `base_url` | `str` | required; the self-hosted upstream base URL |
| `wire_format` | `"openai" \| "anthropic"` | required; reuses 080 `upstreams.py` render/parse |
| `strategy` | `"normalize" \| "reroute"` | required |
| `normalizers` | `list[str]` | required iff `strategy == "normalize"`; **must be empty** for `reroute`; each key MUST exist in `NORMALIZER_REGISTRY` |
| `reroute_upstream` | `str \| None` | optional clean upstream base URL; used as the auto-reroute fallback on smoke-test failure |

**Validation rules**:
- `strategy == "reroute"` ⇒ `normalizers == []` (reroute is not a shim, FR-078-4/SC-004).
- `strategy == "normalize"` ⇒ at least one `normalizers` key; each must resolve in the registry (FR-078-2).
- Unknown `normalizers` key → config validation error (fail at load, not at runtime).

## Normalizer (protocol)

A pure, reusable transform keyed by response format / model-family. Registered in
`NORMALIZER_REGISTRY: dict[str, Normalizer]`.

| Member | Signature | Notes |
|---|---|---|
| `key` | `str` | registry key, e.g. `harmony_tool_calls`, `strip_reasoning` |
| `normalize_json` | `(body: dict) -> dict` | non-streaming path |
| `sse_filter` | `() -> StatefulSSEFilter` | stateful; buffers across chunk boundaries; never emits half-parsed tool calls (FR-078-3) |

**Launch normalizers**:
- `harmony_tool_calls` — reassembles leaked `<|channel|>commentary …` into structured `tool_calls`; MUST NOT leak raw harmony markers (FR-078-7, US1).
- `strip_reasoning` — removes thinking/reasoning blocks (claude, qwen) from JSON + SSE; generalizes the 073 fix (FR-078-8).

**Unknown-format rule**: a normalizer that does not recognize the response shape
returns it **unchanged** (fail-open on normalization, FR-078-9). Health gating is
the only fail-*closed* surface.

## HealthResult

Outcome of the startup tool-calling smoke probe for a target; gates routing.

| Field | Type | Notes |
|---|---|---|
| `target` | `TargetDescriptor` | the probed target |
| `status` | `"healthy" \| "unhealthy"` | unhealthy includes probe timeout |
| `reason` | `str \| None` | populated when unhealthy; surfaced in the fail-closed error |
| `resolved_action` | `"proceed" \| "rerouted" \| "fail_closed"` | gating decision (FR-078-5) |

**State transitions** (FR-078-5, US3):
- `healthy` → `proceed` (routing gated open).
- `unhealthy` + `reroute_upstream` declared → `rerouted` (auto-reroute, job proceeds).
- `unhealthy` + no `reroute_upstream` → `fail_closed` (clear error, card not accepted).
- Never → fail-open onto the unhealthy path.
