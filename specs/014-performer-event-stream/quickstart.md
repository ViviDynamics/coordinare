# Quickstart: 014 — Performer Event Stream

**Branch**: `014-performer-event-stream` | **Date**: 2026-03-13

---

## What this feature adds

A real-time activity feed in the dashboard showing what each AI coding agent is doing
while a card is in progress — file reads, edits, shell commands, and cost summaries —
updated on every poll cycle without requiring a page reload.

---

## Status: Mostly Complete

The overwhelming majority of this feature is already implemented and tested on `main`.
Only **secret redaction** on the `detail` field remains to be added.

### What works today

- `performer_events` flows from any backend → `handle_status()` → coordinare state → dashboard
- Activity feed renders in the dashboard under the Performers card (visible when `phase == monitoring_agent`)
- All three backends produce typed events:
  - **opencode**: tool_use, progress, error
  - **claude_code**: progress, tool_use, thinking, cost, error
  - **codex**: progress, tool_use, thinking, cost, error
- Events capped at 200 per backend buffer; 100 in coordinare state
- 292 performer tests passing; 98% coverage

### What remains

- Secret redaction on `BackendEvent.detail` before storage/transmission

---

## Enabling different backends

```yaml
# config.yaml
agent_transport: subprocess
agent_executable: /path/to/performer
```

```bash
# Performer env
AGENT_BACKEND=opencode    # default
AGENT_BACKEND=claude_code # requires `claude` CLI on PATH
AGENT_BACKEND=codex       # requires `codex` CLI on PATH
```

---

## Secret Redaction Implementation

**Location**: `agent/performer/src/performer/models.py`

Add to `BackendEvent`:

```python
from pydantic import model_validator

_SECRET_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{82,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    re.compile(r"ghs_[A-Za-z0-9]{36}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{90,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]

def _redact_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text

class BackendEvent(BaseModel):
    ...
    @model_validator(mode="before")
    @classmethod
    def _redact_sensitive_detail(cls, data: dict) -> dict:
        if isinstance(data, dict) and "detail" in data:
            data["detail"] = _redact_secrets(str(data["detail"]))
        return data
```

---

## Testing Strategy

| Layer | File | What to test |
|---|---|---|
| Unit — redaction | `performer/tests/unit/test_models.py` | Each pattern matches and is replaced; non-secret strings pass through; idempotent |
| Unit — event creation | `performer/tests/unit/test_models.py` | BackendEvent with secret in detail field stores `[REDACTED]` |
| Dashboard coverage | `tests/unit/test_dashboard.py` | performers-card snapshot includes `performer_events`; SSE path with events |

---

## Dashboard Activity Feed Layout

```
┌── Performers ──────────────────────────────────────────────┐
│  ● claude_code  abc123…  running for 4m 12s               │
│  ─────────────────────────────────────────────────────────│
│  TIME      TYPE        TEXT                               │
│  15:42:01  tool use   Read: src/app/breadcrumbs.tsx       │
│  15:42:03  tool use   Edit: src/app/breadcrumbs.tsx       │
│  15:42:08  progress   Running: yarn test breadcrumbs      │
│  15:42:31  tool use   Edit: src/app/breadcrumbs.tsx       │
│  15:42:33  progress   Tests passing (4/4)                 │
│  15:42:34  cost       1,240 tokens · $0.0182              │
└────────────────────────────────────────────────────────────┘
```
