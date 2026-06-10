# Contract: Request Translation — Anthropic `/v1/messages` → OpenAI `/v1/chat/completions`

**Feature**: 084-anthropic-openai-translate | **Date**: 2026-06-08
**Source FRs**: FR-001 (request translation), FR-004 (tool round-trip incl. `tool_result`),
FR-011 (no body/token logging). Implemented by the pure function
`translate_request(anthropic_body: dict) -> dict` in `proxy/translate/request.py`.

This contract is the **testable artifact** for `test_translate_request.py`. Every mapping below is
deterministic; anything intentionally dropped is recorded here (no silent drops — Decision 5).

---

## Top-level field map

| Anthropic (`/v1/messages`) | OpenAI (`/v1/chat/completions`) | Rule |
|-----------------------------|----------------------------------|------|
| `model` | `model` | Passthrough (the upstream resolves the model name). |
| `system` (string) | leading `{"role": "system", "content": <string>}` | Prepended as the first message. |
| `system` (block array) | leading `{"role": "system", "content": <joined text>}` | Concatenate block `text` values in order, `"\n"`-joined. |
| `messages[]` | `messages[]` | Per-turn mapping (below). |
| `tools[]` | `tools[]` | Per-tool mapping (below). |
| `tool_choice` | `tool_choice` | Shape map (below). |
| `max_tokens` (required) | `max_tokens` | Passthrough. |
| `temperature` | `temperature` | Passthrough when present. |
| `top_p` | `top_p` | Passthrough when present. |
| `stop_sequences` | `stop` | Rename; array passthrough. |
| `stream` | `stream` | Passthrough boolean (drives SSE vs JSON response path). |
| `metadata` | — | **Dropped** (no OpenAI equivalent; not forwarded). |
| `thinking` | — | **Dropped** at translation (the upstream has no Anthropic thinking concept; reasoning cleanup is handled response-side by `strip_reasoning`). |

---

## Message turn mapping (`messages[]`)

Each Anthropic turn has `role` ∈ {`user`, `assistant`} and `content` that is either a string or a
block array. Output is one or more OpenAI messages.

| Anthropic turn | OpenAI message(s) | Rule |
|----------------|-------------------|------|
| `role: user`, `content: <string>` | `{"role": "user", "content": <string>}` | Direct. |
| `role: user`, `content: [text blocks]` | `{"role": "user", "content": <joined text or content-parts>}` | Text blocks joined; multimodal parts mapped to OpenAI content-parts if present. |
| `role: assistant`, `content: [text]` | `{"role": "assistant", "content": <text>}` | Direct. |
| `role: assistant`, `content: [...tool_use...]` | `{"role": "assistant", "content": <text or null>, "tool_calls": [...]}` | Each `tool_use` block → one `tool_calls[]` entry (below). |
| `role: user`, `content: [...tool_result...]` | one `{"role": "tool", ...}` message **per** `tool_result` block | Back-translation (FR-004, below). Non-tool_result blocks in the same turn become a separate `user` message. |

### `tool_use` block → `tool_calls[]` entry

| Anthropic `tool_use` | OpenAI `tool_calls[]` entry |
|----------------------|------------------------------|
| `id` | `id` |
| `name` | `function.name` |
| `input` (object) | `function.arguments` (JSON-stringified) |
| — | `type: "function"` (constant) |

### `tool_result` block → `tool` message (FR-004)

| Anthropic `tool_result` | OpenAI `tool` message |
|-------------------------|------------------------|
| `tool_use_id` | `tool_call_id` |
| `content` (string) | `content` (string) |
| `content` (block array) | `content` (joined text) |
| `is_error: true` | content preserved as-is (OpenAI has no error flag; the textual content carries the error) |
| — | `role: "tool"` (constant) |

---

## Tools mapping (`tools[]`)

| Anthropic tool | OpenAI tool |
|----------------|-------------|
| `name` | `function.name` |
| `description` | `function.description` |
| `input_schema` (JSON Schema) | `function.parameters` |
| — | `type: "function"` (constant) |

## `tool_choice` shape map

| Anthropic `tool_choice` | OpenAI `tool_choice` |
|--------------------------|----------------------|
| `{"type": "auto"}` | `"auto"` |
| `{"type": "any"}` | `"required"` |
| `{"type": "tool", "name": N}` | `{"type": "function", "function": {"name": N}}` |
| absent | omit (upstream default) |

---

## Determinism & security

- **No silent drops**: the only dropped top-level fields are `metadata` and `thinking`, recorded above.
  Any future Anthropic-only field with no OpenAI counterpart MUST be added to this table (dropped or
  mapped), never passed through opaquely (Decision 5 — avoids the upstream rejecting unknown fields).
- **Pure / no I/O**: `translate_request` performs no network or disk I/O and no logging of body content
  (FR-011). Observability of "request translator ran" is emitted by the shim at metadata level only.
