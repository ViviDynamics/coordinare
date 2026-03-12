# Implementation Plan: Performer Event Stream

**Branch**: `014-performer-event-stream` | **Date**: 2026-03-10 | **Spec**: this document

---

## Summary

Add a unified real-time activity feed that surfaces what each AI coding agent (opencode, Claude Code, Codex, etc.) is doing while a card is in progress. Events from each agent's native JSON stream are normalised into a common `BackendEvent` model, accumulated in a rolling buffer in the performer, returned to the coordinare on every `check_status` poll, stored in coordinare state, and displayed in the dashboard as a live scrolling timeline.

No new protocol message types are required — events piggyback on the existing `status` response. No new dependencies are required. The design is additive: if a backend does not implement `drain_events()` nothing breaks.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: all existing — `pydantic>=2.9`, `structlog>=24.1`, `asyncio` (stdlib)
**Storage**: in-memory rolling buffer only (`collections.deque(maxlen=200)` per backend instance, `deque(maxlen=100)` in coordinare state)
**New Backend**: `ClaudeCodeBackend` — launches `claude --output-format stream-json` and parses the event stream
**Affected Packages**: `agent/performer/` (backend interface + backends + protocol) and `src/coordinare/` (monitor_agent, state, dashboard)

---

## Architecture

### Event normalisation

Each backend emits events in its own format. The adapter is responsible for translating these into a common `BackendEvent`:

```
opencode ACP stdout (nd-JSON)   ──► OpenCodeAdapter._event_reader_task()  ──►┐
claude --output-format stream-json ──► ClaudeCodeAdapter._event_reader_task() ──►┤  BackendEvent deque
codex <json-flag> stdout         ──► CodexAdapter._event_reader_task()     ──►┘
                                                                               │
                                                          drain_events() ◄─────┘
                                                               │
                                                  PerformerResponse.events
                                                               │
                                              coordinare monitor_agent node
                                                               │
                                            CoordinareState["performer_events"]
                                                               │
                                                    dashboard SSE snapshot
```

### `BackendEvent` model (performer)

```python
class BackendEventType(str, Enum):
    progress  = "progress"   # agent is working — general text update
    tool_use  = "tool_use"   # agent called a tool (file read/write, shell, etc.)
    thinking  = "thinking"   # internal reasoning (Claude extended thinking)
    cost      = "cost"       # token / cost accounting
    error     = "error"      # non-fatal error within the agent session
    output    = "output"     # raw output line (fallback for unrecognised events)

class BackendEvent(BaseModel):
    timestamp: datetime
    type: BackendEventType
    text: str        # human-readable one-line summary
    detail: str = "" # optional longer detail (e.g. file path, tool args)
```

### `BackendAdapter` interface change

Add `drain_events()` to the base protocol. Returns all events accumulated since the last call and clears the buffer — a pull-based drain model that fits the existing request/response polling pattern.

```python
class BackendAdapter(Protocol):
    def get_status(self) -> BackendStatus: ...
    def drain_events(self) -> list[BackendEvent]: ...  # NEW
    async def start(self, stand: Stand, score: Score) -> None: ...
    async def stop(self) -> None: ...
    async def relay_feedback(self, feedback: str) -> None: ...
```

`drain_events()` is synchronous — it only reads from an in-memory deque, so there is no I/O on the polling path.

### `PerformerResponse` change

```python
class PerformerResponse(BaseModel):
    ...
    events: list[dict] = []  # serialised BackendEvent list; empty when no new events
```

`handle_status()` in `performer/main.py` calls `backend.drain_events()` and attaches the result:

```python
metrics = collect_metrics(perf.backend, backend_status)
events = perf.backend.drain_events()
return PerformerResponse(
    status="working",
    ...
    events=[e.model_dump() for e in events],
)
```

### Coordinare state change

```python
# CoordinareState (graph/state.py)
performer_events: list[dict]  # rolling buffer, capped at 100 entries
```

`monitor_agent` merges new events from each `check_status` response:

```python
new_events = status.get("events", [])
if new_events:
    existing = list(state.get("performer_events") or [])
    state["performer_events"] = (existing + new_events)[-100:]
```

### Dashboard

A new "Activity Feed" card appears below the flowchart when `phase == "monitoring_agent"`. It is a scrolling table of the last 50 events showing timestamp, type badge, and text. Updates in real-time via the existing SSE stream.

```
TIME      TYPE        TEXT
────────────────────────────────────────────────────────────
15:42:01  tool_use   Read: src/app/breadcrumbs.tsx
15:42:03  tool_use   Edit: src/app/breadcrumbs.tsx (42 lines changed)
15:42:08  progress   Running: yarn test breadcrumbs
15:42:31  tool_use   Edit: src/app/breadcrumbs.tsx (3 lines changed)
15:42:33  progress   Tests passing (4/4)
15:42:34  cost       1,240 tokens · $0.018
```

---

## Per-backend event mapping

### opencode (existing)

opencode ACP emits nd-JSON to stdout. The existing `_event_reader_task` already parses this stream — it currently only updates `self._status`. The change is to also emit `BackendEvent` objects into the drain buffer.

| ACP event type | BackendEvent type | text derivation |
|---|---|---|
| `message.part.updated` where role=assistant | `progress` | content text (truncated to 120 chars) |
| `tool.input` | `tool_use` | `"{tool_name}: {input_summary}"` |
| `tool.output` | `tool_use` | `"{tool_name} → {output_summary}"` |
| `session.error` | `error` | error message |
| other | `output` | raw JSON string |

### Claude Code (new backend)

Launch: `claude --output-format stream-json --print <prompt_file>`

Claude Code's `stream-json` format emits one JSON object per line. Relevant types:

| stream-json type | BackendEvent type | text derivation |
|---|---|---|
| `assistant` with text content | `progress` | content text (first 120 chars) |
| `assistant` with `tool_use` content | `tool_use` | `"{tool_name}: {input_summary}"` |
| `assistant` with `thinking` content | `thinking` | thinking text (first 120 chars) |
| `result` subtype=`success` | `cost` | `"{input_tokens}+{output_tokens} tokens · ${cost_usd:.4f}"` |
| `result` subtype=`error` | `error` | error message |

Terminal condition: `result` event → set `BackendStatus(state="done")` or `state="error"`.

The `Score` fields map directly to the `claude` invocation:
- `score.repo_url` / `score.branch` — workspace already cloned by the time `start()` is called
- `score.title` + `score.description` + `score.acceptance_criteria` + `score.clarifications` → written to a temp prompt file passed as `--print`

### Codex (future — needs format research)

The OpenAI Codex CLI is flagged as a future backend. Its JSON output format needs to be verified against the published CLI docs before implementation. A stub `CodexBackend` returning `UnsupportedBackendError` will be added as a placeholder.

---

## Project Structure

### New / changed files

```text
agent/performer/src/performer/
├── models.py                    ← ADD BackendEvent, BackendEventType
├── backends/
│   ├── base.py                  ← ADD drain_events() to BackendAdapter Protocol
│   │                               ADD BackendEvent import
│   ├── opencode.py              ← MODIFY _event_reader_task to emit BackendEvent
│   │                               ADD drain_events() implementation
│   ├── claude_code.py           ← NEW ClaudeCodeBackend
│   ├── codex.py                 ← NEW CodexBackend (stub — UnsupportedBackendError)
│   └── __init__.py              ← ADD "claude_code", "codex" to get_backend()
├── main.py                      ← MODIFY handle_status() to call drain_events()
└── protocol.py                  ← ADD events: list[dict] = [] to PerformerResponse

src/coordinare/
├── graph/
│   ├── state.py                 ← ADD performer_events: list[dict]
│   └── nodes/monitor_agent.py  ← MODIFY to accumulate events from status response
├── dashboard.py                 ← ADD activity feed section + performer_events to snapshot

agent/performer/tests/
├── unit/backends/
│   ├── test_base.py             ← ADD drain_events() compliance check
│   ├── test_opencode.py         ← ADD drain_events() tests
│   └── test_claude_code.py      ← NEW unit tests for ClaudeCodeBackend
└── integration/
    └── test_event_stream.py     ← NEW: end-to-end drain_events → PerformerResponse → dashboard snapshot

tests/integration/
└── test_performer_events.py     ← NEW: coordinare-side accumulation in monitor_agent
```

---

## Implementation Phases

### Phase 1 — Common model + interface (no behaviour change)

1. Add `BackendEvent` + `BackendEventType` to `performer/models.py`
2. Add `drain_events() -> list[BackendEvent]` to `BackendAdapter` Protocol in `backends/base.py`
3. Add `events: list[dict] = []` to `PerformerResponse` in `performer/protocol.py`
4. Add `performer_events: list[dict]` to `CoordinareState` in `graph/state.py`
5. Add `performer_events: []` to `initial_state()`

All tests pass — nothing calls `drain_events()` yet, `events` defaults to `[]`.

### Phase 2 — opencode backend wires drain_events()

6. Add `self._event_buffer: deque[BackendEvent]` to `OpenCodeAdapter.__init__()`
7. In `_event_reader_task`, emit `BackendEvent` objects to the buffer alongside the existing `_status` updates
8. Implement `drain_events()` — atomically return and clear the buffer
9. In `performer/main.py` `handle_status()`, call `drain_events()` and attach to response

Tests: unit tests for the drain buffer; verify existing status tests still pass.

### Phase 3 — Coordinare accumulates events

10. In `monitor_agent`, merge `status.get("events", [])` into `state["performer_events"][-100:]`
11. In `dashboard.py` `build_snapshot()`, include `performer_events` in the snapshot payload
12. Add activity feed card to `_DASHBOARD_HTML` — visible only when `phase == "monitoring_agent"` and events exist
13. Wire `performer_events` into `renderState()` JS

Tests: integration test verifying events flow from mock status response → coordinare state → dashboard snapshot.

### Phase 4 — ClaudeCodeBackend

14. Implement `ClaudeCodeBackend` in `backends/claude_code.py`:
    - `start()`: write prompt to temp file, launch `claude --output-format stream-json --print <file>`
    - `_event_reader_task()`: parse stream-json lines, emit `BackendEvent`, update `_status`
    - `drain_events()`, `get_status()`, `relay_feedback()`, `stop()`
15. Register `"claude_code"` in `backends/__init__.py` `get_backend()`
16. Unit tests with a mock `claude` subprocess emitting fixture JSON lines
17. Update `README.md` and `config.py` to document `AGENT_BACKEND=claude_code`

### Phase 5 — Codex stub + cleanup

18. Add `CodexBackend` stub in `backends/codex.py` — raises `UnsupportedBackendError` with a message pointing to the spec for future implementation
19. Register `"codex"` in `get_backend()` with the stub
20. Final coverage check — maintain ≥ 90% threshold

---

## Testing Strategy

| Layer | What is tested |
|---|---|
| Unit — `BackendEvent` | Serialisation round-trip; all `BackendEventType` values |
| Unit — `drain_events()` | Buffer fills during event reader; drain clears buffer; concurrent drain is safe |
| Unit — `ClaudeCodeBackend` | Fixture JSON lines → correct `BackendEvent` types and text; terminal detection on `result` event |
| Unit — `PerformerResponse` | `events` field defaults to `[]`; serialises/deserialises correctly |
| Integration — performer | `handle_status()` returns non-empty `events` when backend has buffered events |
| Integration — coordinare | `monitor_agent` accumulates events; capped at 100; empty events are a no-op |
| Contract — dashboard snapshot | `performer_events` key present; each entry has `timestamp`, `type`, `text` |

---

## Open Questions

1. **Claude Code prompt delivery**: `--print` accepts a prompt string or a file path. For long prompts (with full clarification history), a temp file is safer. Verify max prompt length limits.
2. **Claude Code `relay_feedback`**: When the coordinare sends review feedback, does `claude` support follow-up input mid-session, or does it require a new invocation? If a new invocation, the `relay_feedback` handler starts a second `claude` process with the feedback appended to the original prompt.
3. **Codex JSON format**: Needs research before Phase 5 can be implemented. Defer until format is confirmed.
4. **Event PII / secrets**: Ensure `drain_events()` does not surface github_token or other secrets embedded in tool arguments. Add a redaction pass on `detail` field for known secret patterns.
