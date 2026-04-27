# Contract: Session Eligibility State Fields

**Feature**: 054-async-multi-card-orchestration  
**Type**: State schema extension (additive)  
**Files affected**: `src/coordinare/graph/state.py`, `src/coordinare/daemon.py`, `src/coordinare/dashboard.py`

## New State Fields

### `session_skip_reasons`

Added to `CoordinareState` TypedDict.

```python
session_skip_reasons: dict[str, dict]
```

**Lifecycle**:
- Cleared to `{}` at the start of each cycle (before eligibility computation).
- Populated by `_invoke_multi_session()` for every session classified as ineligible in that cycle.
- Exposed via snapshot in `dashboard.py` under key `"session_skip_reasons"`.

**Value shape per entry** (`card_id → SkipReasonEntry`):

```python
{
    "reason": str,         # "blocked_column" | "dependency_blocked" | "missing_card"
    "detail": str | None,  # Human-readable explanation
    "blockers": list[int]  # Dependency issue numbers; empty for non-dependency skips
}
```

**Reason values**:

| Reason | Trigger |
|--------|---------|
| `eligible` | Session invoked (not stored in skip map) |
| `blocked_column` | Session's card is in the BLOCKED board column |
| `dependency_blocked` | Session has one or more unresolved dependency blockers (`PENDING` or `UNRESOLVABLE`) |
| `missing_card` | Session has no current card — treated as ineligible for the cycle |

## Internal Types

### `SessionEligibility` (daemon.py, not persisted)

Transient per-cycle derived value; not stored in state.

```python
@dataclass
class SessionEligibility:
    card_id: str
    eligible: bool
    reason: str
    blockers: list[int] = field(default_factory=list)
```

### `AsyncSessionTickResult` (daemon.py, not persisted)

Per-session result of one async invocation; collected by `asyncio.gather`.

```python
@dataclass
class AsyncSessionTickResult:
    card_id: str
    ok: bool
    session_state: dict
    skipped: bool = False
    error: str | None = None
    duration_ms: int = 0
```

## Compatibility

- `max_concurrent_cards=1` path does not populate `session_skip_reasons` (single-session mode invokes the graph directly).
- `session_skip_reasons` defaults to `{}` in `CoordinareState.defaults`; existing state missing the field is handled by `.get("session_skip_reasons", {})` at cycle start.
- No existing fields are modified or removed.
