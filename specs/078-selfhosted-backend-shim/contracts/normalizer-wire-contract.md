# Contract: Self-Hosted Robustness Layer Wire Behavior

Defines the externally-observable contract of the self-hosted robustness layer:
routing-table resolution, the normalize/reroute strategies, normalizer transforms,
and health gating. Tests live in `agent/performer/tests/unit/proxy/`.

## Activation (front door)

- The layer resolves `RoutingTable.resolve(backend, model)` at job start (the
  `maybe_launch_proxy` seam, `main.py:~1107`). (FR-078-1, FR-078-4)
- **No matching entry → no-op**: no proxy launched, no env override, bytes pass
  unchanged. Byte-for-byte identical to layer-disabled. (FR-078-1, SC-003)
- Matching entry, `strategy: reroute` → backend provider base URL set to the
  clean upstream; **no proxy/shim, no normalizer**. (FR-078-4, SC-004)
- Matching entry, `strategy: normalize` → loopback shim launched (`127.0.0.1:0`),
  backend provider base URL set to it; shim forwards to `target.base_url` and
  applies the target's **explicitly-declared** normalizers only. (FR-078-2)
- Backend with no provider-base-URL env mapping (e.g. hermes) → clear "cannot
  route" error, never a silent no-op into a broken path. (FR-078-4, Edge Case)

## Normalizer contract (both JSON and SSE) — FR-078-3

- **JSON**: `normalize_json(body) -> body'` — single recombined response body.
- **SSE**: stateful filter buffers across chunk boundaries; never emits a
  half-parsed tool call; emits valid events for the CLI parser (reuse 073 SSE).
- **`harmony_tool_calls`**: leaked `<|channel|>commentary to=<tool> …` →
  structured `tool_calls`; zero raw `<|channel|>` / harmony markers downstream.
  (FR-078-7, US1, SC-002)
- **`strip_reasoning`**: removes thinking/reasoning blocks (claude, qwen) from
  JSON + SSE. (FR-078-8, generalizes 073)
- **Unknown format / no matching normalizer**: response passes through
  **unchanged** (fail-open on normalization). (FR-078-9, Edge Case)

## Health gating — FR-078-5

- Startup tool-calling smoke test is **timeout-bounded**; a wedged upstream →
  `unhealthy` (not a hung startup). (Edge Case, SC-005)
- `healthy` → routing gated open, job proceeds.
- `unhealthy` + declared `reroute_upstream` → **auto-reroute**, job proceeds.
- `unhealthy` + no `reroute_upstream` → **fail-closed** with a clear, specific
  error before any card is assigned. Never fail-open onto the unhealthy path.
  (SC-005)

## Cross-cutting (MUST)

- Generalizes the 073 `ClaudeCodeShim` / 080 `DualModelProxy` seam — one shared
  framework, not N independent shims. (FR-078-6)
- Reuses 080 env-restore lifecycle so no provider-env leaks across jobs.
- No auth tokens or request/response bodies in logs; INFO = method/path/status/
  latency + normalizer/strategy/health decision. (FR-078-10)
- Composes with (does not replace) the 077 stall watchdog.

## Field Registry (for /speckit.analyze contract check)

This feature adds **no new dispatch-payload field**. The routing table is a
performer-side config surface (loaded in-container), not carried per-card on the
dispatch payload. The existing provider-override env vars in `PROVIDER_BASE_URL_ENV`
(set by the layer) are the only cross-boundary mechanism, and they are unchanged
from 080.

| Field | Carrier | Notes |
|---|---|---|
| (none) | — | no new dispatch-payload field; routing table is performer-side config |
| `<BACKEND>_PROVIDER_BASE_URL` / `ANTHROPIC_BASE_URL` | process env (set by layer) | reused from 080 `PROVIDER_BASE_URL_ENV`; reroute repoints these; unchanged contract |
| (auth tokens) | existing `secrets` dict | unchanged; referenced by env-var name only, never logged |
