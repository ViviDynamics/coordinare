# Contract: Completion-Style Health-Probe Mode

Adds an opt-in per-target `health_probe` selector to the 078 self-hosted health
gate. Default (`tool_call`) is unchanged; `completion` judges health on a
non-empty normalized completion. `gate()` and all existing routed targets are
untouched.

## Mode → probe + success criterion

| `health_probe` | Probe request | Healthy criterion (after normalizers) | Notes |
|---|---|---|---|
| `tool_call` (default) | tools + tool_choice (existing `_probe_body`) | structured `tool_call`/`tool_use` present | byte-for-byte unchanged |
| `completion` | trivial no-tools chat-completion | non-empty assistant `content` | new; for non-tool-calling backends |

## Gating (both modes, unchanged `gate()`)

| Probe status | `reroute_upstream` | Resolved action |
|---|---|---|
| healthy | — | `proceed` |
| unhealthy | declared | `rerouted` |
| unhealthy | none | `fail_closed` |

Unhealthy triggers: non-200, timeout, connection error, or — per mode — no tool call (`tool_call`) / empty content (`completion`).

## Invariants (MUST)

1. **Opt-in, default unchanged (FR-003, SC-002):** a target without `health_probe` uses the tool-call probe with identical success criterion + gating; no existing routed target's decision changes.
2. **Completion success = non-empty normalized content (FR-002, SC-001):** `completion` mode gates healthy only on a well-formed, non-empty completion after the target's normalizers run.
3. **Normalize-then-judge (FR-004):** the completion probe runs the declared normalizer chain before judging (a promoted reasoning-only answer counts healthy), mirroring the tool-call path.
4. **Fail-closed preserved (FR-005, SC-003):** an unhealthy completion-mode target is never admitted — `fail_closed`, or `rerouted` if a fallback is declared. Timeout-bounded.
5. **Fail-fast config (FR-006, SC-006):** an invalid `health_probe` value (or unknown key) fails at config-load, never a silent default-through.
6. **Unblocks 098 US2 (FR-007, SC-004):** a `completion`-mode normalize target makes the junie assessor routable through the 098 normalizers by config alone.
7. **Secret-free record (FR-008, SC-005):** the `proxy.health` record carries `mode` + method/path/status/resolved_action only — never tokens or bodies.

## Observability record: `proxy.health` (extended)

```json
{
  "event": "proxy.health",
  "method": "POST",
  "path": "http://…/v1/chat/completions",
  "status": "healthy | unhealthy",
  "resolved_action": "proceed | rerouted | fail_closed",
  "wire_format": "openai | anthropic",
  "strategy": "normalize | reroute | translate",
  "mode": "tool_call | completion"
}
```
