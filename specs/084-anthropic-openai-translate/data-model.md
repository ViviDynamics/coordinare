# Phase 1 Data Model: Anthropic→OpenAI Request-Translating Shim

**Feature**: 084-anthropic-openai-translate | **Date**: 2026-06-08

This feature adds **no persisted state**. The "entities" below are (a) the extended config/routing
model and (b) the in-memory translator components. The config model is the only durable, operator-facing
surface; the translators are pure transforms with no stored state beyond per-stream SSE buffering.

---

## Entity 1: `TargetDescriptor` (EXTENDED — `proxy/routing.py`)

The existing frozen, `extra="forbid"` pydantic model gains one new legal `strategy` value. No new
fields are added.

| Field | Type | Change | Rule |
|-------|------|--------|------|
| `base_url` | `str` (min_length=1) | unchanged | Required; the OpenAI-wire upstream for a translate entry. |
| `wire_format` | `Literal["openai", "anthropic"]` | unchanged | For `translate`, MUST be `openai` (the upstream is OpenAI-wire). `anthropic` + `translate` is contradictory → reject at load (FR-008). |
| `strategy` | `Literal["normalize", "reroute", "translate"]` | **+ `"translate"`** | New value selects request+response translation via the loopback shim. |
| `normalizers` | `list[str]` (default `[]`) | semantics extended | For `translate`: optional; when present every key MUST be registered in `NORMALIZER_REGISTRY` (same rule as `normalize`). Empty is legal (translation alone, no harmony/reasoning cleanup). |
| `reroute_upstream` | `str \| None` (default `None`) | unchanged | Health-gated fallback target when the primary translate target fails the startup probe (FR-010). |

### Validation rules (`_validate_strategy`, extended)

1. `strategy == "reroute"` ⇒ `normalizers` MUST be empty/absent. *(unchanged)*
2. `strategy == "normalize"` ⇒ `normalizers` non-empty; every key registered. *(unchanged)*
3. **`strategy == "translate"` ⇒ `wire_format` MUST be `"openai"`** — `translate` + `wire_format == "anthropic"`
   is meaningless (nothing to translate) and MUST be rejected at load with an actionable message (FR-008).
4. `strategy == "translate"` ⇒ `normalizers` (if present) MUST all be registered in `NORMALIZER_REGISTRY`
   (reuses rule 2's registry check; empty list allowed).
5. `base_url` missing, or `wire_format ∉ {openai, anthropic}` → load error. *(unchanged)*

### State transitions

None — `TargetDescriptor` is frozen and immutable once loaded. The routing table is read once at job
start and fails fast if missing/malformed (FR-009 / FR-078-5).

---

## Entity 2: Request Translator (`proxy/translate/request.py`)

Pure function: `translate_request(anthropic_body: dict) -> dict` (Anthropic `/v1/messages` →
OpenAI `/v1/chat/completions`). No state. Mapping detailed in `contracts/request-translation.md`.

| Input (Anthropic) | Output (OpenAI) | Notes |
|-------------------|-----------------|-------|
| `system` (str or block array) | leading `{role: "system", content}` message | Prepended to `messages`. |
| `messages[]` turns | `messages[]` turns | Role + content-block mapping (text, `tool_use`, `tool_result`). |
| content block `text` | string content / content part | |
| content block `tool_use` | assistant `tool_calls[]` entry | id, function name, JSON-stringified args. |
| content block `tool_result` | `{role: "tool", tool_call_id, content}` | Back-translation (FR-004). |
| `tools[]` (with `input_schema`) | `tools[]` (`function` + `parameters`) | |
| `tool_choice` | `tool_choice` | shape map (`auto`/`any`/`tool` → `auto`/`required`/named). |
| `stream` | `stream` | passthrough boolean. |
| `stop_sequences` | `stop` | |
| `max_tokens` (required in Anthropic) | `max_tokens` | |
| `temperature` | `temperature` | |

---

## Entity 3: Non-Streaming Response Translator (`proxy/translate/response.py`)

Pure function: `translate_response(openai_body: dict) -> dict` (OpenAI chat completion →
Anthropic `/v1/messages` response). Runs **after** JSON normalizers. Detailed in
`contracts/response-translation.md`.

| Input (OpenAI) | Output (Anthropic) | Notes |
|----------------|--------------------|-------|
| `choices[0].message.role` | `role: "assistant"` | |
| `choices[0].message.content` | `content[]` text block | |
| `choices[0].message.tool_calls[]` | `content[]` `tool_use` blocks | FR-004; consumes normalized harmony output. |
| `choices[0].finish_reason` | `stop_reason` | per FR-005 map (see contract). |
| `usage.prompt_tokens` / `completion_tokens` | `usage.input_tokens` / `output_tokens` | |

---

## Entity 4: Streaming SSE Translator (`proxy/translate/sse.py`)

A `StatefulSSEFilter` subclass: consumes OpenAI SSE chunk deltas, emits the Anthropic SSE event
sequence (`message_start` → `content_block_start`/`_delta`/`_stop` → `message_delta` → `message_stop`).
Buffers on `\n\n` (inherited), so it is chunk-boundary-safe (FR-003) and slots into the existing
`_FilterChain` **after** the normalizer filters.

| Per-stream state | Purpose |
|------------------|---------|
| open content-block index / type | Track which Anthropic content block is currently streaming. |
| accumulated tool-call args | Reassemble OpenAI `tool_calls` argument deltas into Anthropic `input_json_delta`. |
| emitted-`message_start` flag | Emit Anthropic `message_start` exactly once. |
| captured `finish_reason` | Map to `stop_reason` in the closing `message_delta` (FR-005). |

State is per-stream and discarded at stream end; nothing persists.

---

## Relationships

```text
RoutingTable (1) ──contains──> (N) RoutingEntry ──has──> (1) TargetDescriptor
                                                              │ strategy == "translate"
                                                              ▼
                              SelfHostedShim (loopback, per job)
                                  inbound  /v1/messages
                                    └─> Request Translator ──> OpenAI /v1/chat/completions ──> upstream
                                  response
                                    upstream ──> Normalizers (_FilterChain) ──> Response/SSE Translator ──> CLI
```

Translate reuses `SelfHostedShim`, `_FilterChain`, and `NORMALIZER_REGISTRY` unchanged; it adds the two
translator hooks at the shim's inbound/outbound edges.
