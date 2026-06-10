# Feature Specification: Anthropic→OpenAI Request-Translating Shim

**Feature Branch**: `084-anthropic-openai-translate`
**Created**: 2026-06-08
**Status**: Draft
**Input**: User description: "Anthropic-to-OpenAI request-translating shim for the self-hosted backend layer (spec 078), so claude_code-backed roles (notably `qa`) can route Ollama-direct and bypass LiteLLM's broken streaming harmony→tool_calls handling."

## Overview

The 078 self-hosted backend layer makes self-hosted models robust for **OpenAI-wire** backends (openclaw, junie, codex, pi). It does this two ways: `reroute` repoints a backend's provider env straight at a clean upstream (e.g. Ollama-direct), and `normalize` launches a loopback shim that rewrites the **response** through declared normalizers (`harmony_tool_calls`, `strip_reasoning`). Both strategies are response-only — neither touches the request body — because every backend on that path already speaks the same wire protocol as the upstream.

`claude_code` is different. The claude CLI speaks the **Anthropic** wire protocol: it POSTs `/v1/messages` with Anthropic-format request bodies and parses Anthropic-format responses. Self-hosted gpt-oss on Ollama speaks **OpenAI** wire only (`/v1/chat/completions`); it has no `/v1/messages` endpoint. Today the only thing that bridges Anthropic↔OpenAI for self-hosted gpt-oss is the LiteLLM proxy — and LiteLLM's *streaming* harmony→tool_calls handling is broken (LiteLLM #17246 / #13300): it leaks raw harmony text instead of emitting structured tool_calls, so tools never execute.

Because the 078 layer never translates the request, a `reroute` or `normalize` entry for `(claude_code, gpt-oss:120b)` would aim or forward an Anthropic-wire request at an OpenAI-only Ollama endpoint with no translation, and the job would break. So the proven-clean Ollama-direct path is unavailable to claude_code roles like `qa`.

This feature adds **request-side Anthropic→OpenAI translation** (plus the matching OpenAI→Anthropic response translation) to the 078 path, so a `(claude_code, <self-hosted model>)` routing entry can reach an OpenAI-wire upstream cleanly, bypassing LiteLLM entirely while still composing with the existing response normalizers, health gating, and fail-closed loading. The first intended beneficiary is the `qa` role, but flipping its live config to this path is explicitly a follow-on (see Out of Scope).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A claude_code role reaches an OpenAI-wire upstream with no LiteLLM in the path (Priority: P1)

An operator wants a claude_code-backed role to run against self-hosted gpt-oss on Ollama-direct, without the LiteLLM proxy. They add a routing entry for `(claude_code, <model>)` declaring the translating strategy and an OpenAI-wire upstream. The claude CLI's Anthropic `/v1/messages` request is translated to an OpenAI `/v1/chat/completions` request, sent to Ollama-direct, and the OpenAI response is translated back to Anthropic wire so the CLI parses it normally.

**Why this priority**: This is the core capability. Without request translation the claude_code role cannot reach an OpenAI-only upstream at all; everything else in the spec depends on this bridge existing.

**Independent Test**: Configure a `(claude_code, <model>)` translating entry pointing at an OpenAI-wire upstream, run a job on that role, and confirm the role completes its task using the response the upstream returned — with no LiteLLM in the path. Testable in isolation by exercising the translation against a stub OpenAI-wire upstream for both non-streaming JSON and streaming SSE.

**Acceptance Scenarios**:

1. **Given** a `(claude_code, model)` entry with the translating strategy and an OpenAI-wire upstream, **When** the claude CLI POSTs an Anthropic `/v1/messages` body, **Then** the upstream receives a well-formed OpenAI `/v1/chat/completions` request (system prompt, message turns, content blocks, tools, tool_choice, stream flag, stop sequences, max_tokens, temperature mapped to their OpenAI equivalents).
2. **Given** a non-streaming OpenAI response from the upstream, **When** it returns, **Then** the CLI receives a well-formed Anthropic `/v1/messages` response (role, content blocks, stop_reason, usage) that it parses without error.
3. **Given** `stream: true`, **When** the upstream emits an OpenAI SSE stream, **Then** the CLI receives an Anthropic-format SSE event sequence (message_start → content_block deltas → message_delta → message_stop) it consumes without error.

---

### User Story 2 - Tool calls survive the round trip with no harmony leak (Priority: P1)

A claude_code role that uses tools (like `qa`, which emits a structured contract result) runs over the translating path. Tool definitions and tool-choice intent reach the upstream, the model's tool calls come back as structured Anthropic `tool_use` blocks, and a follow-up turn carrying `tool_result` content is translated back to the OpenAI shape — across both streaming and non-streaming, with no raw harmony markers leaking into the tool-call payloads.

**Why this priority**: Eliminating the streaming harmony→tool_calls leak is the entire motivation. A translating path that delivered text but dropped or corrupted tool calls would not be parity-or-better than today's LiteLLM-shim path and would not unblock `qa`.

**Independent Test**: Run a tool-using exchange over the translating path against a stub upstream that emits gpt-oss/harmony-style tool calls; assert the CLI sees structured `tool_use` blocks with no harmony text, and that a subsequent `tool_result` turn is accepted by the upstream.

**Acceptance Scenarios**:

1. **Given** an Anthropic request with `tools` and a `tool_choice`, **When** it is translated, **Then** the OpenAI request carries the equivalent `tools` and `tool_choice`.
2. **Given** an upstream streaming response containing harmony-style tool calls, **When** translation composes with the `harmony_tool_calls` normalizer, **Then** the CLI receives structured `tool_use` blocks and zero raw harmony markers appear in the output.
3. **Given** a multi-turn exchange where the prior assistant turn made a tool call, **When** the CLI sends a turn containing a `tool_result` content block, **Then** it is translated to the OpenAI tool-result message shape the upstream expects.

---

### User Story 3 - Opt-in per pair; nothing else changes (Priority: P2)

An operator enables translation for exactly one `(claude_code, model)` pair. Every other backend/model pair — including all current openclaw/junie/codex/pi assignments and any unrouted claude_code pair — behaves exactly as before: native cloud pairs stay byte-for-byte no-ops, and existing `reroute`/`normalize` entries keep their current semantics.

**Why this priority**: The layer's safety contract is that activation is per `(backend, model)` and additive. A regression here could silently break working roles, so it must be guaranteed — but it gates nothing in US1/US2, hence P2.

**Independent Test**: With a single translating entry present, resolve a variety of other pairs and confirm they return the same no-op / reroute / normalize behavior they did before this feature existed.

**Acceptance Scenarios**:

1. **Given** a routing table whose only entry is a translating `(claude_code, model)` pair, **When** an openclaw or codex job resolves its pair, **Then** its behavior is identical to the pre-feature behavior (no-op or its existing reroute/normalize).
2. **Given** a claude_code pair with no routing entry, **When** it resolves, **Then** it is a byte-for-byte no-op (the native path is untouched).

---

### Edge Cases

- **Meaningless wire-format combination**: an entry that asks to translate but whose declared upstream wire format makes the translation a no-op or contradiction (e.g. translate strategy with `wire_format: anthropic`) MUST be rejected at load, not accepted then break a card mid-lifecycle.
- **Broken or missing routing table**: a non-empty but missing/malformed/unreadable table FAILS FAST at job start (consistent with FR-078-5), with an actionable message — never black-holes a card.
- **Upstream returns a non-2xx / error body**: the real upstream status and an error surface to the caller; the failure is not masked as a success or a silent empty response.
- **Field with no clean equivalent**: when an Anthropic request field has no direct OpenAI counterpart (or vice versa), the translator's documented mapping/default applies deterministically rather than silently dropping data without record.
- **finish_reason ↔ stop_reason**: OpenAI `finish_reason` values (`stop`, `length`, `tool_calls`, …) map to the Anthropic `stop_reason` the CLI expects (`end_turn`, `max_tokens`, `tool_use`, …), in both streaming and non-streaming.
- **Health gate exercises the translating path**: the startup smoke test for a translating entry validates the actual translate-and-forward round trip (request translation + response translation), not just upstream reachability; an unhealthy translating target follows the same fail-closed / `reroute_upstream` fallback rules as other strategies.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The layer MUST translate an Anthropic `/v1/messages` request body into an OpenAI `/v1/chat/completions` request body, mapping at minimum: the system prompt, the ordered message turns, content blocks (text and tool-related blocks), `tools`, `tool_choice`, the `stream` flag, stop sequences, `max_tokens`, and `temperature`.
- **FR-002**: The layer MUST translate the upstream OpenAI response back into an Anthropic `/v1/messages` response for the non-streaming (JSON) case, including role, content blocks, `stop_reason`, and token usage.
- **FR-003**: The layer MUST translate a streaming OpenAI SSE response into the Anthropic SSE event sequence the claude CLI consumes, as a stateful chunk-boundary-safe transform that composes with the existing response normalizers and the layer's SSE filter-chain model.
- **FR-004**: Tool calls MUST survive the full round trip: request `tools`/`tool_choice` reach the upstream, the model's tool calls return as structured Anthropic `tool_use` blocks with no leaked harmony markers (composing with the `harmony_tool_calls` normalizer), and a follow-up turn carrying `tool_result` content is translated to the OpenAI tool-result message shape — in both streaming and non-streaming.
- **FR-005**: The layer MUST map OpenAI `finish_reason` values to the corresponding Anthropic `stop_reason` values in both streaming and non-streaming responses.
- **FR-006**: The routing table MUST let a `(backend, model)` entry select the translating behavior as a per-entry choice, leaving the existing `reroute` and `normalize` semantics intact and unchanged for OpenAI-wire backends.
- **FR-007**: Activation MUST remain per `(backend, model)`: a pair with no entry is a byte-for-byte no-op, and enabling a translating entry MUST NOT change the behavior of any other pair (no openclaw/junie/codex/pi regression).
- **FR-008**: A translating entry whose declared upstream wire format / strategy combination is meaningless or contradictory MUST be rejected at config load (fail fast), not at runtime.
- **FR-009**: A non-empty routing table that is missing, malformed, or otherwise unloadable MUST fail closed at job start with an actionable message (the offending path and the env var that points at it), never silently black-holing a card.
- **FR-010**: The startup health gate for a translating entry MUST exercise the translating path end-to-end (request translation through response translation); an unhealthy target MUST follow the same fail-closed behavior and `reroute_upstream` fallback rules as the other strategies, and MUST NOT fail open.
- **FR-011**: The layer MUST NOT log auth tokens or request/response bodies. Observability is limited to method, path, status, latency, and which translator/normalizers ran (consistent with FR-078-10 / FR-011).
- **FR-012**: An upstream non-2xx or error response MUST be surfaced to the caller with its real status; the failure MUST NOT be masked as a success or a silent empty body.

### Key Entities *(include if feature involves data)*

- **Translating routing entry**: a `(backend, model)` → target descriptor that declares the translating behavior and an OpenAI-wire upstream, alongside the existing reroute/normalize entries in the same table.
- **Request translator**: the component that maps an Anthropic `/v1/messages` request body to an OpenAI `/v1/chat/completions` request body (system, messages, content blocks, tools, tool_choice, stream, stop, max_tokens, temperature).
- **Response translator**: the component that maps an OpenAI response back to Anthropic wire for both non-streaming JSON and streaming SSE, composing with the existing response normalizers and SSE filter chain.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A qa-equivalent exchange run over the claude_code → OpenAI-wire (Ollama-direct) translating path produces valid contract output (`criteria_passed` JSON) with tool calls executing and zero raw harmony markers leaking into tool calls — parity-or-better versus the current LiteLLM-shim qa path.
- **SC-002**: With a single translating entry active, 100% of other resolved `(backend, model)` pairs (including every current openclaw/junie/codex/pi assignment and any unrouted claude_code pair) exhibit identical behavior to before the feature.
- **SC-003**: Every malformed or contradictory translating entry (meaningless wire-format/strategy combo; missing/unreadable table) is rejected at load or job start with an actionable message — none are accepted and then fail mid-job.
- **SC-004**: No auth token, request body, or response body appears in any log emitted by the translating path; emitted records are limited to method/path/status/latency plus translator/normalizer decisions.
- **SC-005**: The FR-001/FR-002/FR-004/FR-005 fields round-trip correctly for both streaming and non-streaming exchanges in the test suite (request fields reach the upstream; response role, content blocks, stop_reason, usage, and tool_use blocks reach the CLI).
- **SC-006** (performance budget): The in-process translation overhead the translate path adds — request translation plus response/SSE translation, **excluding** upstream and network time — is **≤ 5 ms median and ≤ 15 ms p99** per request for a qa-representative exchange (system + user turn, tools present, streaming), measured by a deterministic benchmark in the test suite against the stub OpenAI-wire upstream (no live network). Because the translate path removes the LiteLLM network hop rather than adding one, total end-to-end latency for a qa-equivalent exchange MUST be at parity-or-better versus the LiteLLM-shim path (the qualitative claim in SC-001), and the added in-process transform cost MUST stay within this budget.
