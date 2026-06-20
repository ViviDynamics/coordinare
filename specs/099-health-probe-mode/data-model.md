# Data Model: Completion-Style Health-Probe Mode

**No persisted coordinare state and no schema change.** The probe mode is a config attribute of a self-hosted routing target (the `SELFHOSTED_ROUTING_CONFIG` YAML), consumed entirely within the performer's startup health gate.

## 1. New config field (routing target)

| Field | Type | Default | Validation | Use |
|---|---|---|---|---|
| `health_probe` | `Literal["tool_call", "completion"]` on `TargetDescriptor` | `"tool_call"` | pydantic `Literal` + frozen + `extra="forbid"` → invalid value / unknown key fails at config-load (`RoutingTable.from_yaml_file`), FR-006 | selects the startup probe shape + health success criterion |

Backward compatible: a target omitting `health_probe` keeps the tool-call probe with identical gating (FR-003).

## 2. Probe behavior by mode (not persisted)

| Mode | Probe request | Healthy when | Unhealthy when |
|---|---|---|---|
| `tool_call` (default) | tools/tool_choice payload (unchanged) | structured `tool_call`/`tool_use` survives normalizers | no structured call / non-200 / timeout / empty (unchanged) |
| `completion` | trivial no-tools chat-completion | **non-empty** assistant content **after normalizers** | empty content / non-200 / timeout / unparseable |

Both modes feed the existing `gate()`: `healthy → proceed`; `unhealthy + reroute_upstream → rerouted`; else `fail_closed`.

## 3. Reused mechanisms (unchanged)

- `gate(target, status, reason)` — the proceed / reroute / fail_closed state machine.
- `_probe_url(target)` — upstream path per wire-format/strategy.
- `_probe_body` model/`upstream_model` 404-avoidance.
- `NORMALIZER_REGISTRY` chain — run before judging (both modes).

## 4. Observability record (extended, secret-free)

`proxy.health` log record adds **`mode`** (`tool_call` | `completion`); existing fields (`method`, `path`, `status`, `resolved_action`, `wire_format`, `strategy`) unchanged. Never tokens or response bodies (FR-008).
