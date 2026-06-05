# Contract: Dual-Model Proxy Wire Behavior

Defines the externally-observable contract of `DualModelProxy` for each strategy. Tests live in `agent/performer/tests/unit/proxy/`.

## Front door

- The proxy binds `127.0.0.1:0`, `start()` returns the loopback base URL; the backend's provider base URL is set to it when `strategy != single`. (FR-009)
- Accepts the wire format the backend's CLI emits to its provider (Anthropic `/v1/messages` for claude_code; OpenAI chat-completions for codex/opencode/junie/pi/openclaw). Normalizes to `LLMTurn`; renders responses back to the same format. (FR-010)
- `strategy: single` → no proxy launched; CLI talks to the endpoint directly (native = own client/no override). (FR-009)

## Per-strategy contract

### `always`
1. Calls `thinking` upstream with tools **hidden**; obtains plan text.
2. Calls `tool` upstream with tools present and the plan injected as a **system message** prepended to the conversation. (FR-012)
3. Returns one response: `tool_calls`/content from the tool call, plan surfaced per `expose_plan_as`. (FR-013)
- On thinking failure: `on_think_error=fall_back_to_act` → proceed act-only + warn; `=fail` → error. (FR-017)

### `conditional`
1. `classifier.score(turn)` → float.
2. score ≥ `threshold` → run `always` path; else → `tool` only. (FR-015)
3. classifier failure → default to think. (FR-015)

### `think_once`
1. First turn of stage (or invalid plan) → think, cache plan in `StageState`. (FR-016)
2. Subsequent turns → act with cached plan injected; no think call.
3. Re-think when `turns_since_plan ≥ invalidate_after_turns`, or `invalidate_on_error` and the latest incoming tool result carries an error marker. (FR-016)

## Transport (both JSON and SSE, each supported format) — FR-014

- **JSON**: single recombined response body.
- **SSE**: think runs internally (not streamed); act phase streamed to CLI; when `expose_plan_as` requires, synthesized plan events are emitted before act events; stream is valid for the CLI parser (reuse 073 stateful SSE handling).

## Cross-cutting (MUST)

- Tool-upstream failure → no fabricated output; surfaced on the existing backend failure/retry path. (FR-017)
- Every upstream call timeout-bounded; composes with the 077 stall watchdog; never black-holes. (FR-018)
- No auth tokens or request/response bodies in INFO logs; INFO = method/path/status/latency. (FR-019)
- Each turn writes an `OrchestrationRecord` (strategy, decision, classifier score, plan, per-call latency/ok) to the job capture dir. (FR-021)
- Agent terminal contract (DONE/PARTIAL_PROGRESS/BLOCKED) unaffected. (FR-020)

## Field Registry (for /speckit.analyze contract check)

The dispatch carries one new structured object, `orchestration`, present only when `strategy != single`. **Implementation note:** rather than fork the dual-mirror `JobInitPayload` schema, it rides in the dispatch `metadata` (coordinare sets `card_context["orchestration"]`, which flows to `JobInitPayload.metadata`), consistent with how `base_url`/`model`/`api_key_env` are already carried. The performer reads `metadata["orchestration"]`. Auth secrets are NOT in this object — they continue through the existing `secrets` dict by env-var name.

| Field | Carrier | Notes |
|---|---|---|
| `orchestration` | dispatch `metadata` (nullable) | absent → no proxy (`single`); present → proxy launched |
| `orchestration.strategy` | `JobInitPayload.orchestration` | `always` \| `conditional` \| `think_once` |
| `orchestration.tool` | `JobInitPayload.orchestration` | UpstreamRef `{base_url, model, wire_format, auth_env}` |
| `orchestration.thinking` | `JobInitPayload.orchestration` | UpstreamRef (multi-model strategies) |
| `orchestration.classifier` | `JobInitPayload.orchestration` | UpstreamRef \| RuleSet \| null (`conditional`) |
| `orchestration.threshold` | `JobInitPayload.orchestration` | float (`conditional`) |
| `orchestration.invalidate_after_turns` / `invalidate_on_error` / `error_pattern` | `JobInitPayload.orchestration` | `think_once` |
| `orchestration.expose_plan_as` / `on_think_error` | `JobInitPayload.orchestration` | recombination / failure policy |
| (auth tokens) | existing `secrets` dict | unchanged mechanism; referenced by `auth_env` name only |
