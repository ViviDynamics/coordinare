# Contract: Response Translation — OpenAI → Anthropic `/v1/messages` (JSON + SSE)

**Feature**: 084-anthropic-openai-translate | **Date**: 2026-06-08
**Source FRs**: FR-002 (non-streaming response translation), FR-003 (streaming SSE translation
composing with normalizers + filter chain), FR-004 (structured `tool_use` out), FR-005
(`finish_reason` ↔ `stop_reason`), FR-011 (no body/token logging), FR-012 (surface upstream non-2xx).

Two pure components implement this contract:
- `translate_response(openai_body: dict) -> dict` in `proxy/translate/response.py` (non-streaming).
- A `StatefulSSEFilter` subclass in `proxy/translate/sse.py` (streaming).

**Composition order (Decision 4):** upstream OpenAI bytes → **normalizers** (`harmony_tool_calls`,
`strip_reasoning`) → **wire-format translation** (this contract) → CLI. The translator is the
**outermost** response transform and always runs on already-normalized OpenAI shape.

**Non-2xx passthrough (FR-012):** any upstream status outside 2xx is surfaced verbatim (status, body,
headers) — the translator is **not** invoked. Only successful OpenAI responses are translated.

---

## `finish_reason` ↔ `stop_reason` map (FR-005) — single source of truth

Applied **identically** in non-streaming (`response.py`) and streaming (`sse.py` closing
`message_delta`). Centralized in one helper to prevent drift (Decision 6).

| OpenAI `finish_reason` | Anthropic `stop_reason` |
|------------------------|--------------------------|
| `stop` | `end_turn` |
| `length` | `max_tokens` |
| `tool_calls` | `tool_use` |
| `content_filter` | `end_turn` (closest safe terminal) |
| unknown / absent / `null` | `end_turn` (default) |

---

## Non-streaming JSON (`translate_response`)

OpenAI chat completion object → Anthropic `/v1/messages` response object.

| OpenAI | Anthropic | Rule |
|--------|-----------|------|
| `id` | `id` | Passthrough (or synthesized `msg_…` if absent). |
| — | `type: "message"` | Constant. |
| `choices[0].message.role` | `role: "assistant"` | Constant assistant. |
| `choices[0].message.content` (string) | `content: [{"type": "text", "text": <string>}]` | Wrap as one text block (omitted if empty and tool_calls present). |
| `choices[0].message.tool_calls[]` | `content: [{"type": "tool_use", ...}]` | One `tool_use` block per call (below). Consumes normalized harmony output (FR-004). |
| `choices[0].finish_reason` | `stop_reason` | Per map above. |
| — | `stop_sequence: null` | Constant unless a stop sequence matched (then the matched string). |
| `model` | `model` | Passthrough. |
| `usage.prompt_tokens` | `usage.input_tokens` | Rename. |
| `usage.completion_tokens` | `usage.output_tokens` | Rename. |

### `tool_calls[]` entry → `tool_use` block

| OpenAI `tool_calls[]` | Anthropic `tool_use` block |
|-----------------------|-----------------------------|
| `id` | `id` |
| `function.name` | `name` |
| `function.arguments` (JSON string) | `input` (parsed object) |
| — | `type: "tool_use"` (constant) |

If `function.arguments` is not valid JSON, emit `input: {}` and record the raw string in
metadata-level observability (never the value in logs). This is the documented degenerate case, not a
silent drop.

---

## Streaming SSE (`proxy/translate/sse.py`)

Consumes the OpenAI chat-completion SSE stream (`data: {chunk}` frames, terminated by `data: [DONE]`)
and emits the Anthropic `/v1/messages` event sequence. Subclasses `StatefulSSEFilter`, so it inherits
`\n\n`-boundary buffering and is chunk-boundary-safe (FR-003). It is appended to the `_FilterChain`
**after** the normalizer filters, so it sees already-reassembled OpenAI `tool_calls` deltas.

### Emitted Anthropic event sequence

```text
event: message_start          data: {message: {role: assistant, usage:{input_tokens,…}}}
event: content_block_start    data: {index, content_block: {type: text|tool_use, …}}
event: content_block_delta    data: {index, delta: {type: text_delta|input_json_delta, …}}   (repeats)
event: content_block_stop     data: {index}
…(more blocks: one per text run / tool_use)…
event: message_delta          data: {delta: {stop_reason}, usage:{output_tokens}}
event: message_stop           data: {}
```

### OpenAI chunk → Anthropic event mapping

| OpenAI SSE delta | Anthropic event(s) | Rule |
|------------------|--------------------|------|
| first chunk seen | `message_start` | Emit exactly once (tracked by `emitted_message_start` flag). |
| `delta.content` (text, first of a run) | `content_block_start` (type `text`) + `content_block_delta` (`text_delta`) | Open a text block lazily on first text token. |
| `delta.content` (text, continuation) | `content_block_delta` (`text_delta`) | Append to the open text block. |
| `delta.tool_calls[]` (first of a call) | `content_block_start` (type `tool_use`, with `id`/`name`) | Open a tool_use block; remember its index. |
| `delta.tool_calls[].function.arguments` (chunk) | `content_block_delta` (`input_json_delta`, `partial_json`) | Stream argument fragments; accumulate for completeness. |
| block changes (text→tool / tool→tool) | `content_block_stop` (prev) before next `content_block_start` | Close the prior block. |
| `choices[0].finish_reason` (on final chunk) | captured → `message_delta.delta.stop_reason` | Mapped via the FR-005 table. |
| `data: [DONE]` | `content_block_stop` (last open) → `message_delta` → `message_stop` | Flush and close the message. |

### Per-stream state (discarded at stream end)

| State | Purpose |
|-------|---------|
| `emitted_message_start` | Emit `message_start` exactly once. |
| open block index / type | Track the currently-streaming Anthropic content block. |
| accumulated tool-call args | Reassemble OpenAI argument deltas into `input_json_delta`. |
| captured `finish_reason` | Map to `stop_reason` in the closing `message_delta`. |

---

## Determinism & security

- The `finish_reason`↔`stop_reason` map is the only place the two response paths could drift; both
  import the same helper (Decision 6) — `test_translate_response.py` and `test_translate_sse.py`
  assert against the same table.
- Both components are pure (no network/disk). No response body, token text, or `usage` values are
  logged (FR-011); only "response/SSE translator ran" + which normalizers ran is emitted at metadata
  level.
- Upstream non-2xx is surfaced verbatim and never translated (FR-012).
