# Quickstart: Anthropic→OpenAI Request-Translating Shim

**Feature**: 084-anthropic-openai-translate | **Date**: 2026-06-08

This quickstart lists the Phase 1 integration scenarios that validate the `translate` strategy
end-to-end. Each scenario maps to a user story / FRs and is the basis for the integration tests named
in `plan.md`. All scenarios run against a **deterministic stub OpenAI-wire upstream** (no live network
in CI, per Constitution II).

---

## Prerequisites

- `.venv` synced (`uv sync`); tests via `.venv/bin/pytest`, lint via `.venv/bin/ruff check`.
- A stub OpenAI-wire upstream fixture that returns canned `/v1/chat/completions` JSON and SSE streams
  (including a harmony-leak streaming case to exercise the `harmony_tool_calls` normalizer).
- A routing table with one `translate` entry for `(claude_code, gpt-oss:120b)` (see
  `contracts/routing-table.md`).

---

## Scenario 1 — claude_code reaches an OpenAI-wire upstream (US1, FR-001/FR-002)

**Goal**: An Anthropic `/v1/messages` request is translated and answered with no LiteLLM in the path.

1. Load the routing table; resolve `(claude_code, gpt-oss:120b)` → `strategy: translate`.
2. `launch.py` launches the `SelfHostedShim` on `127.0.0.1:0` and sets `ANTHROPIC_BASE_URL` to it.
3. POST a representative Anthropic `/v1/messages` body (system + user turn) to the shim.
4. **Assert**: the shim forwarded an OpenAI `/v1/chat/completions` body to the stub upstream (system
   message prepended, `max_tokens` present, `stop_sequences`→`stop`), and the CLI-facing response is a
   valid Anthropic message object (`role: assistant`, `content[]` text block, `stop_reason: end_turn`,
   `usage.input_tokens`/`output_tokens`).

## Scenario 2 — tool round-trip with no harmony leak (US2, FR-003/FR-004/FR-005)

**Goal**: Tools survive the full streaming round-trip; zero harmony text leaks into tool calls.

1. Send an Anthropic request that includes `tools[]` and a `tool_choice`.
2. The stub upstream replies with a **streaming** response that leaks harmony commentary instead of
   structured `tool_calls` (the LiteLLM #17246 failure shape).
3. **Assert**: `harmony_tool_calls` reassembles structured OpenAI `tool_calls` (runs first), then the
   SSE translator emits Anthropic `content_block_start`/`_delta`/`_stop` for a `tool_use` block with
   `name` + parsed `input`, a closing `message_delta` with `stop_reason: tool_use`, and `message_stop`.
4. Send a follow-up Anthropic turn containing a `tool_result` block.
5. **Assert**: the request translator emits an OpenAI `{role: "tool", tool_call_id, content}` message
   (back-translation, FR-004).
6. **Assert**: no raw harmony markers appear anywhere in the CLI-facing stream (SC-002).

## Scenario 3 — opt-in per pair, no regression (US3, FR-006/FR-007)

**Goal**: Activation is per `(backend, model)`; every other pair is byte-for-byte unchanged.

1. Load a table containing the `translate` entry **plus** existing `reroute`/`normalize` entries for
   OpenAI-wire backends (e.g. `(openclaw, gpt-oss:120b)` reroute).
2. **Assert**: resolving an unrelated pair with no entry yields a no-op (no shim, no translation).
3. **Assert**: the `reroute`/`normalize` entries resolve and behave identically to pre-084 (snapshot
   the `TargetDescriptor`s and the launch dispatch).

## Scenario 4 — contradictory wire/strategy rejected at load (FR-008)

1. Load a table with `strategy: translate` + `wire_format: anthropic`.
2. **Assert**: job start fails closed with an actionable error naming the pair and both fields
   (Rule T1). No shim is launched.

## Scenario 5 — broken/missing table fails closed (FR-009)

1. Point the loader at a missing / malformed routing table.
2. **Assert**: the job does not start; the error is surfaced; no partial/degraded translate path runs.

## Scenario 6 — health gate exercises the full round-trip, no fail-open (FR-010)

1. Configure the stub upstream to be reachable but return an **untranslatable** response.
2. **Assert**: the translate health probe sends a representative Anthropic body through the request
   translator, forwards it, runs the reply back through normalizers + response translator, and finds no
   valid Anthropic terminal/`tool_use` output → `gate()` returns `fail_closed` (or `rerouted` when
   `reroute_upstream` is set). It MUST NOT fail open.
3. With a healthy upstream: **assert** `gate()` returns `proceed` and the job starts.

## Scenario 7 — upstream non-2xx surfaced verbatim (FR-012)

1. Stub upstream returns a 4xx/5xx with a body.
2. **Assert**: the shim surfaces the status + body verbatim to the CLI; the response translator is
   **not** invoked.

## Scenario 8 — no secrets / bodies logged (FR-011, SC-004)

1. Run Scenarios 1–2 with log capture.
2. **Assert**: logs contain only method/path/status/latency + which translator ran + which normalizers
   ran. No request/response body, no token text, no `auth_env` value (only the env-var **name** may
   appear).

---

## Mapping to Success Criteria

| Scenario | Success Criteria |
|----------|------------------|
| 1 | SC-001 (claude_code reaches OpenAI-wire upstream, no LiteLLM) |
| 2 | SC-002 (tool calls execute, zero harmony leak) |
| 3 | SC-003 (no regression on unrouted/other pairs) |
| 6 | SC-005 (deterministic, testable translation behavior; fail-closed health) |
| 8 | SC-004 (no secrets/bodies logged) |
