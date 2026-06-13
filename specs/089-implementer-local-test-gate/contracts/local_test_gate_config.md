# Contract: `LocalTestGateConfig` + self-fix counter

**Modules**: `src/coordinare/config.py`, `state_store.py`, `session.py`, `daemon.py`, `graph/nodes/monitor_performer.py`, `graph/nodes/dispatch_performer.py`, `agent/performer/src/performer/models.py` (`Score`)
**Mirrors**: `CIGateConfig` (config.py lines 1319–1326); `bounce_counter` (state_store/session/daemon/monitor)

## Config model

```python
class LocalTestGateConfig(BaseModel):
    """Implementer local test gate (spec 089), sibling of CIGateConfig."""
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    timeout_seconds: int = Field(default=600, ge=60, le=7200)
    max_fix_attempts: int = Field(default=2, ge=0, le=20)
```

Nested under the same persona/symphony scope surface that hosts `CIGateConfig`. Resolution must yield `enabled=False` when unconfigured.

## Cross-boundary delivery (coordinare → performer)

The gate is configured coordinare-side but executes inside the performer (`_run_test_check` in `main.py`). The performer only sees fields delivered on the dispatch payload: `dispatch_performer` builds `card_context`, whose keys flatten to top-level `msg.payload` keys and are constructed into the `Score` model via `Score(**msg.payload)` (main.py:1218). The `Score` model (`agent/performer/src/performer/models.py`) uses `extra="ignore"` (models.py:135), so any key it does not declare is silently dropped. The gate config must therefore be (1) injected for the `implementing` role and (2) declared on `Score`, or it never reaches the helper.

### Field Registry

| Field | Type | Origin → Consumer | Notes |
|-------|------|-------------------|-------|
| `local_test_gate` | `dict` (`{enabled: bool, timeout_seconds: int}`) | `dispatch_performer.card_context` → `Score.local_test_gate` → `main.py` done-path | Declared on `Score` (else dropped by `extra="ignore"`). Absent/`enabled=False` ⇒ gate skipped (SC-005). |
| `enabled` | `bool` | coordinare `LocalTestGateConfig.enabled` → performer | Gates the whole `_run_test_check` call. |
| `timeout_seconds` | `int` | coordinare `LocalTestGateConfig.timeout_seconds` → performer | Passed to `_run_test_check(timeout_seconds=…)` (FR-010). |

`max_fix_attempts` is **coordinare-only** — consumed by the monitor self-fix loop; never sent to the performer.

## Counter wiring

| Module | Change |
|--------|--------|
| `state_store.py` | `local_fix_counter: dict[str, int] = Field(default_factory=dict)` on `PersistedSession`; `CURRENT_SCHEMA_VERSION` v8 (current v7); v1–v7 load with `{}`. |
| `session.py` | add `local_fix_counter` to the live session dict + the persisted-fields tuple. |
| `daemon.py` | hydrate from snapshot (mirror lines ~133–142) and persist (mirror line ~637). |
| `monitor_performer.py` | on implementer `changes_requested` where `status.local_test_failed`: increment `local_fix_counter[head_sha]`; re-dispatch while `<= max_fix_attempts`; else route to **blocked** with the test output as reason. |

## Decision table (coordinare, implementer changes_requested w/ local_test_failed)

| `local_fix_counter[head]` after increment | `enabled` | Action |
|-------------------------------------------|-----------|--------|
| `<= max_fix_attempts` | True | re-dispatch implementer with failing output |
| `> max_fix_attempts` | True | route card → **blocked**, reason = test output; no push |
| (any) | False | gate dormant — no counter, no escalation; legacy behaviour |

## Invariants

- `local_fix_counter` and `bounce_counter` never read or write each other (SC-004).
- Disabled (`enabled=False`) → no `_run_test_check` call, no counter, no env_blocked route reachable → byte-identical to pre-089 (SC-005).
- Escalation to blocked never pushes (SC-001).

## Test obligations

- `LocalTestGateConfig` defaults: `enabled=False`, `timeout_seconds=600`, `max_fix_attempts=2`; bounds rejected outside range; `extra="forbid"`.
- pre-089 (v7) snapshot loads with `local_fix_counter == {}`.
- counter increments per local_test_failed changes_requested; resets implicitly on new head.
- exceeding `max_fix_attempts` routes to blocked, not re-dispatch, not push.
- `bounce_counter` untouched across local-fix increments and vice versa.
- disabled config: done-path skips the test gate entirely (assert no `detect` test-run, existing tests green).
