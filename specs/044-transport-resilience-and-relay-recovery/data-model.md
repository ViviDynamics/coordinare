# 044 — Transport Resilience & Relay Recovery — Data Model

## State Changes

No new fields on `CoordinareState`. The existing `system_error_count` (int) is reused with a threshold check for relay retry budget.

## Modified Behavior

### system_error_count threshold

| Condition | Before | After |
|---|---|---|
| Transport error during relay, count < 3 | Increment + re-dispatch | Same (no change) |
| Transport error during relay, count >= 3 | Increment + re-dispatch (loop forever) | Transition to blocked with diagnostic |
| Successful dispatch | Reset to 0 | Same (no change) |

### Transport parsing

| Input | Before | After |
|---|---|---|
| Valid JSON line | Parse → ProtocolResponse | Same |
| Non-JSON line (ANSI, logs, bare text) | TransportError immediately | Skip, log at debug, try next line |
| No valid JSON in buffer | N/A (single-line parse) | TransportError (same outcome) |
| Empty response | TransportError | Same |

## New Helper

### commit_files (performer workspace)

```python
async def commit_files(
    stand: Stand,
    files: list[dict[str, str]],  # [{"path": "docs/wiki/setup.md", "content": "..."}]
    message: str,
) -> list[str]:  # returns list of committed file paths
```

Stages all files via `git add`, commits with single message, pushes once. Returns the list of successfully committed paths.
