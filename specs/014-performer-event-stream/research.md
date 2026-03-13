# Research: Performer Event Stream

**Branch**: `014-performer-event-stream` | **Date**: 2026-03-13 | **Phase**: 0

---

## Codebase Audit: What Is Already Implemented

A full read of the performer and coordinare packages revealed that the majority of
this feature is already implemented on `main`. The plan focuses on what remains.

### Already complete (verified by code + tests)

| Component | File | Status |
|---|---|---|
| `BackendEvent` + `BackendEventType` models | `performer/models.py` | ✓ 100% coverage |
| `drain_events()` on `BackendAdapter` Protocol | `performer/backends/base.py` | ✓ 100% coverage |
| `events: list[dict]` on `PerformerResponse` | `performer/protocol.py` | ✓ 100% coverage |
| `OpenCodeAdapter._event_buffer` + `drain_events()` | `performer/backends/opencode.py` | ✓ 97% coverage |
| `OpenCodeAdapter._handle_event()` event emission | `performer/backends/opencode.py` | ✓ (progress, tool_use, error) |
| `ClaudeCodeBackend` — full stream-json implementation | `performer/backends/claude_code.py` | ✓ 100% coverage |
| `CodexBackend` — full WebSocket JSON-RPC implementation | `performer/backends/codex.py` | ✓ 97% coverage |
| `get_backend()` factory — all three backends registered | `performer/backends/__init__.py` | ✓ 100% coverage |
| `handle_status()` drains events into response | `performer/main.py` | ✓ 98% coverage |
| `performer_events: list[dict]` in `CoordinareState` | `src/coordinare/graph/state.py` | ✓ |
| `performer_events: []` in `initial_state()` | `src/coordinare/graph/state.py` | ✓ |
| `monitor_agent` event accumulation (capped at 100) | `src/coordinare/graph/nodes/monitor_agent.py` | ✓ 100% coverage |
| `performer_events` in dashboard SSE snapshot | `src/coordinare/dashboard.py` | ✓ |
| Activity feed HTML + JavaScript rendering | `src/coordinare/dashboard.py` | ✓ |
| Unit tests for all three backends | `performer/tests/unit/backends/` | ✓ 292 tests pass |
| Unit tests for `monitor_agent` event accumulation | `tests/unit/graph/nodes/test_monitor_agent.py` | ✓ |

**Performer test coverage: 98.15%** (threshold 90%)
**Coordinare test coverage: 94%** (threshold 90%)

---

## What Remains: Secret Redaction (FR-010)

### Finding

No secret redaction pass exists on `BackendEvent.detail`. The `workspace.py` file
has a `_redact_auth_headers()` helper for git stderr, but this does not cover the
event buffer. The `models.py` `BackendEvent` class has no redaction logic.

### Decision: Add a `_redact_detail(text: str) -> str` function to `performer/models.py`

**Rationale**:
- Single responsibility: models know their own validation rules.
- Centralised: all three backends call `_emit(type, text, detail=...)` which
  creates a `BackendEvent` directly — adding a `model_validator` or a module-level
  helper function guarantees all paths are covered.
- No new dependencies: `re` module is already imported in `models.py`.

**Approach**: `model_validator(mode="before")` on `BackendEvent` that calls
`_redact_detail(detail)` before storing. This is simpler and more correct than
adding redaction at every call site.

**Patterns to redact** (replace with `[REDACTED]`):

| Pattern | Regex | Covers |
|---|---|---|
| GitHub PAT (classic) | `ghp_[A-Za-z0-9]{36}` | `ghp_…` tokens |
| GitHub PAT (fine-grained) | `github_pat_[A-Za-z0-9_]{82}` | fine-grained PATs |
| GitHub OAuth | `gho_[A-Za-z0-9]{36}` | OAuth app tokens |
| GitHub App token | `ghs_[A-Za-z0-9]{36}` | installation tokens (FR-006 linkage with 015) |
| Anthropic API key | `sk-ant-[A-Za-z0-9\-_]{90,}` | Claude API keys |
| Generic Bearer token | `Bearer\s+[A-Za-z0-9\-._~+/]{20,}` | Authorization header values |
| AWS access key | `AKIA[0-9A-Z]{16}` | AWS credentials |

**Alternatives rejected**:
- Per-callsite redaction in each `_emit()` — error-prone, misses future backends.
- Allowlist (only permit known-safe patterns) — too broad, would redact tool output like file paths.
- Redaction at the coordinare side — too late; secrets would traverse the wire.

---

## Dashboard Coverage Gap

`dashboard.py` is at 76% coverage. The missing lines (91–93, 139, 163, 204–205,
234–235, 976, 991–1006, 1010) include error-handling paths and the `performers-card`
rendering logic. Tests for the dashboard SSE snapshot and performers-card section
should be added alongside the redaction work.

---

## ClaudeCodeBackend — relay_feedback Resolution

The open question from the original plan.md ("does claude support mid-session
input?") is resolved: `claude --resume <session_id>` preserves full conversation
history. The implementation in `claude_code.py` already uses `--resume`:

```python
async def relay_feedback(self, feedback: str) -> None:
    await self.stop()
    await self._launch(feedback, resume_session_id=self._session_id)
    self._status = BackendStatus(state="working")
```

The `session_id` is captured from the `system/init` event and also the `result`
event. No change needed.

---

## Summary

The only remaining implementation work for this feature is:
1. **Secret redaction** — add `_redact_detail()` to `BackendEvent` in `models.py`
2. **Tests for redaction** — unit tests in `test_models.py`
3. **Dashboard coverage** — targeted tests for the performers-card snapshot path
