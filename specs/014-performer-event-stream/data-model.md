# Data Model: Performer Event Stream

**Branch**: `014-performer-event-stream` | **Date**: 2026-03-13 | **Phase**: 1

> **Note**: Most entities are already implemented. This document records the as-built
> model and the one pending addition (secret redaction).

---

## BackendEventType (Enum)

Categorical label for a `BackendEvent`.

| Value | Meaning |
|---|---|
| `progress` | General agent working update (text message) |
| `tool_use` | Agent invoked a tool (file read/write/edit, shell command) |
| `thinking` | Internal reasoning (Claude extended thinking blocks) |
| `cost` | Token and cost accounting at session end |
| `error` | Non-fatal error within the session |
| `output` | Unclassified raw output (fallback) |

**Location**: `agent/performer/src/performer/models.py` — ✓ implemented

---

## BackendEvent (Pydantic BaseModel)

A single normalised activity record emitted by any backend.

| Field | Type | Default | Description |
|---|---|---|---|
| `timestamp` | `datetime` | `datetime.now(UTC)` | When the event occurred |
| `type` | `BackendEventType` | required | Event category |
| `text` | `str` | required | Human-readable summary (≤200 chars) |
| `detail` | `str` | `""` | Extended info (file path, tool args) — **redacted** |

**Pending addition**: `model_validator(mode="before")` that calls `_redact_detail(detail)`
before storing, replacing any matched secret pattern with `[REDACTED]`.

**Redacted patterns**: GitHub PATs (classic/fine-grained/OAuth/App), Anthropic API keys,
generic Bearer tokens, AWS access keys. See `research.md` for full regex list.

**Location**: `agent/performer/src/performer/models.py`

---

## EventBuffer (per-backend deque)

Each backend instance holds its own rolling in-memory buffer.

| Attribute | Type | Description |
|---|---|---|
| `_event_buffer` | `deque[BackendEvent](maxlen=200)` | Rolling buffer; oldest dropped on overflow |

- `drain_events()` atomically returns all buffered events as a list and clears the buffer.
- The drain is synchronous (no I/O — pure memory read + clear).
- All three backends (`OpenCodeAdapter`, `ClaudeCodeBackend`, `CodexBackend`) implement this.

---

## PerformerResponse (extension)

The existing wire response model gains an `events` field.

| Field | Type | Default | Description |
|---|---|---|---|
| `events` | `list[dict]` | `[]` | Serialised `BackendEvent` list (`model_dump()` per event) |

- Populated in `handle_status()` for `working` state only.
- Empty list on `blocked`, `error`, `pr_opened` responses.

**Location**: `agent/performer/src/performer/protocol.py` — ✓ implemented

---

## CoordinareState (extension)

| Field | Type | Initial | Description |
|---|---|---|---|
| `performer_events` | `list[dict]` | `[]` | Rolling accumulation of events from all status polls |

- Merged in `monitor_agent`: `(existing + new_events)[-100:]`
- Reset implicitly when a new card session starts (state is re-initialised).

**Location**: `src/coordinare/graph/state.py` — ✓ implemented

---

## Per-Backend Event Mapping (as-built)

### OpenCodeAdapter

| ACP event type | BackendEvent type | text derivation |
|---|---|---|
| `message.part.updated` / `type=text` | `progress` | content text (first 200 chars) |
| `message.part.updated` / `type=tool-input` | `tool_use` | `"{tool_name}: {input_summary}"` |
| `message.part.updated` / `type=tool-output` | `tool_use` | `"{tool_name} → {output_summary}"` |
| `session.error` | `error` | error message |

### ClaudeCodeBackend

| stream-json type | BackendEvent type | text derivation |
|---|---|---|
| `assistant` / `content[].type=text` | `progress` | content text (first 200 chars) |
| `assistant` / `content[].type=tool_use` | `tool_use` | tool name |
| `assistant` / `content[].type=thinking` | `thinking` | thinking text (first 200 chars) |
| `tool_result` | `tool_use` | `"result: {content}"` |
| `result` / `subtype=success` | `cost` | `"{N} tokens · ${cost:.4f}"` |
| `result` / `subtype=error\|interrupted` | `error` | error message |

### CodexBackend (WebSocket JSON-RPC)

| Notification method | BackendEvent type | text derivation |
|---|---|---|
| `item/agentMessage/delta` | `progress` | delta text |
| `item/commandExecution/outputDelta` | `tool_use` | output delta |
| `item/completed` / `commandExecution` | `tool_use` | command string |
| `item/completed` / `fileChange` | `tool_use` | `"edit {path}"` |
| `thread/tokenUsage/updated` | `cost` | `"{N} tokens total"` |
| `item/reasoning/textDelta` | `thinking` | delta text |
| `error` (non-retrying) | `error` | error message |
| `turn/completed` / status=interrupted\|failed | `error` | error message |
