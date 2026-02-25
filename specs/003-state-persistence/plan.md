# Implementation Plan: Workflow State Persistence

**Branch**: `003-state-persistence` | **Date**: 2026-02-22 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/003-state-persistence/spec.md`

---

## Summary

Implement durable workflow state persistence for the coordinare daemon using an atomic JSON file store. On every phase transition the coordinare writes a `WorkflowSnapshot` to disk via temp-file + `os.replace()`. On startup it reads that snapshot, reconciles against the live GitHub board, and resumes the active workflow without human intervention or duplicate dispatch. The health endpoint is extended to surface `phase` and `snapshot_at` immediately after restart, before the first poll cycle completes.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: Pydantic v2 (already present via `pydantic-settings`), Python stdlib (`pathlib`, `tempfile`, `os`, `json`), `prometheus-client` (already present)
**Storage**: Single JSON file on a mounted volume — no external database or network service required
**Testing**: pytest with `anyio` for async tests; `tmp_path` fixture for filesystem isolation
**Target Platform**: Linux container (Docker / Kubernetes), single instance per volume
**Project Type**: Single project (`src/` + `tests/`)
**Performance Goals**: State write completes within 1 second of every phase transition (SC-002); typically < 10 ms on local filesystem
**Constraints**: Atomic writes — a partial or interrupted write must never corrupt the last valid state (FR-004); no external runtime dependencies (FR-005)
**Scale/Scope**: Single active card at a time; state payload < 1 KB; no storage or performance pressure

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

### I. Code Quality First ✅
- `StateStore` has a single responsibility: read/write `WorkflowSnapshot` to disk
- All public interfaces carry full type annotations
- No new external dependencies (uses only Pydantic v2 + Python stdlib, both already present)
- Dead code: none introduced; no commented-out blocks

### II. Testing Discipline ✅
- **Unit tests**: `tests/unit/test_state_store.py` — save round-trip, load missing file (→ None), load corrupted (→ StateLoadError), schema mismatch (→ StateLoadError), disk-full simulation (→ warning, no raise), metrics recording, `verify_writable` pass/fail
- **Integration tests**: `tests/integration/test_crash_recovery.py` — SIGKILL + restart + verify phase/card recovered within one poll interval; no duplicate dispatch
- **Contract tests**: `tests/contract/test_health_schema.py` — assert `phase` and `snapshot_at` present in health response; validate against `contracts/health-response.schema.json`
- Coverage must not regress

### III. User Experience Consistency ✅
- Health endpoint response adds `phase` and `snapshot_at` at the top level, consistent with existing flat response structure
- Error messages are structured and actionable (log reason + path; never expose stack traces to operators via health endpoint)

### IV. Performance by Design ✅
- **Budget**: State write ≤ 1.0 s (SC-002) — aligned with Histogram bucket upper bound
- **Measurement**: `coordinare_state_write_duration_seconds` Histogram measures every write in production
- **Regression prevention**: Unit tests time write operations; integration test verifies recovery within one poll interval
- Atomic rename (`rename(2)`) is O(1) at the filesystem layer — no bulk data movement

### V. Clarity Before Action ✅
- All NEEDS CLARIFICATION items resolved in `speckit.clarify` session (2026-02-22)
- Storage format: JSON ✅
- Default path: `./coordinare.state.json` ✅
- Prometheus metrics: yes, three metrics ✅
- No unresolved placeholders remain in spec, research, data-model, or contracts

---

## Project Structure

### Documentation (this feature)

```text
specs/003-state-persistence/
├── plan.md                                   # This file
├── research.md                               # Phase 0 decisions and rationale
├── data-model.md                             # WorkflowSnapshot model and StateStore interface
├── quickstart.md                             # Developer and operator guide
├── contracts/
│   ├── workflow-snapshot.schema.json         # JSON Schema for on-disk state file
│   └── health-response.schema.json           # JSON Schema for GET /health (v2, adds phase + snapshot_at)
└── tasks.md                                  # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── __init__.py
├── __main__.py          # MODIFY: construct StateStore, call verify_writable(), pass to daemon
├── config.py            # MODIFY: add state_file_path: Path field
├── daemon.py            # MODIFY: accept StateStore, call load() on startup, save() on phase change
├── health.py            # MODIFY: expose phase and snapshot_at from StateStore
├── metrics.py           # MODIFY: add 3 state persistence metrics to CoordinareMetrics
├── state_store.py       # NEW: WorkflowSnapshot model, StateStore class, StateLoadError
├── graph/
│   └── state.py         # NO CHANGE: CoordinareState TypedDict remains authoritative for runtime
├── lib/
├── models/
└── services/

tests/
├── unit/
│   ├── test_state_store.py        # NEW: unit tests for all StateStore behaviours
│   └── (existing tests unchanged)
├── integration/
│   ├── test_crash_recovery.py     # NEW: SIGKILL → restart → verify recovery
│   └── (existing tests unchanged)
└── contract/
    ├── test_health_schema.py      # MODIFY: add phase + snapshot_at assertions
    └── (existing tests unchanged)
```

**Structure Decision**: Single project layout (Option 1). `state_store.py` is a new top-level module in `src/coordinare/` — consistent with `health.py`, `metrics.py`, and `config.py`, each of which own a single cross-cutting concern. No new sub-packages required.

---

## Implementation Phases

### Phase A: New Module — `state_store.py`

Create `src/coordinare/state_store.py` containing:

1. `CURRENT_SCHEMA_VERSION: int = 1`
2. `WorkflowPhase` type alias (Literal union matching `CoordinareState.phase`)
3. `WorkflowSnapshot(BaseModel)` — full Pydantic v2 definition (see `data-model.md`)
4. `StateLoadError(ValueError)` — with `reason: str` and `detail: str`
5. `StateStore` class:
   - `__init__(self, path: Path, metrics: CoordinareMetrics) -> None`
   - `verify_writable(self) -> None` — probe write; raises `OSError`
   - `async save(self, snapshot: WorkflowSnapshot) -> None` — temp-file + `os.replace()`; records metrics; logs warning on failure but does not raise
   - `async load(self) -> WorkflowSnapshot | None` — reads file; raises `StateLoadError` on error

**Key implementation details**:

```python
# Atomic write (inside StateStore.save)
import os, tempfile
from pathlib import Path

tmp = tempfile.NamedTemporaryFile(
    dir=self._path.parent, delete=False, suffix=".tmp"
)
try:
    tmp.write(snapshot.model_dump_json(indent=2).encode("utf-8"))
    tmp.flush()
    os.fsync(tmp.fileno())  # ensure bytes reach disk before rename
    tmp.close()
    os.replace(tmp.name, self._path)
except OSError:
    tmp.close()
    Path(tmp.name).unlink(missing_ok=True)
    raise
```

```python
# Schema version check (inside StateStore.load)
if snapshot.schema_version != CURRENT_SCHEMA_VERSION:
    raise StateLoadError(
        reason="schema_mismatch",
        detail=f"expected {CURRENT_SCHEMA_VERSION}, got {snapshot.schema_version}",
    )
```

---

### Phase B: Config Extension

In `src/coordinare/config.py`, add:

```python
state_file_path: Path = Field(default=Path("./coordinare.state.json"))
```

Environment variable: `COORDINARE_STATE_FILE_PATH`

---

### Phase C: Metrics Extension

In `src/coordinare/metrics.py`, add to `CoordinareMetrics.__init__`:

```python
self.state_write_duration_seconds = Histogram(
    "coordinare_state_write_duration_seconds",
    "Duration of atomic state file write operations",
    registry=self.registry,
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0),
)
self.state_write_failures_total = Counter(
    "coordinare_state_write_failures_total",
    "Total number of state write failures",
    registry=self.registry,
)
self.state_last_written_timestamp = Gauge(
    "coordinare_state_last_written_timestamp",
    "Unix timestamp of the last successful state write",
    registry=self.registry,
)
```

---

### Phase D: Daemon Integration

In `src/coordinare/daemon.py`:

1. Add `state_store: StateStore` parameter to `CoordinareDaemon.__init__`
2. Store as `self._state_store = state_store`
3. Add startup recovery in `start()` before the poll loop:

```python
# Startup: load persisted state
try:
    snapshot = await self._state_store.load()
    if snapshot is not None:
        self._restore_from_snapshot(snapshot)
        self._emit(category="startup", message="prior state loaded", phase=snapshot.phase)
    else:
        self._emit(category="startup", message="no prior state found")
except StateLoadError as exc:
    self._emit(category="warning", message="state load failed", reason=exc.reason, detail=exc.detail)
    # Fresh start — self._state already initialised by initial_state()
```

4. Add `_restore_from_snapshot(self, snapshot: WorkflowSnapshot) -> None` private method mapping snapshot fields to `self._state`
5. Add board reconciliation call after restore (query board → if card missing/Done → discard restore, log discrepancy)
6. After phase-transition detection (lines 110–119), add snapshot write:

```python
if current_phase != previous_phase:
    # ... existing log event ...
    snapshot = self._build_snapshot()
    await self._state_store.save(snapshot)
```

7. Add `_build_snapshot(self) -> WorkflowSnapshot` private method mapping `self._state` to `WorkflowSnapshot`

---

### Phase E: Health Endpoint Extension

In `src/coordinare/health.py`:

1. Access `daemon.state_store` to get the last loaded/saved snapshot
2. Add `phase` and `snapshot_at` to the response:

```python
snapshot = daemon.state_store.last_snapshot  # new property returning WorkflowSnapshot | None
phase = snapshot.phase if snapshot else None
snapshot_at = snapshot.snapshot_at if snapshot else None

return {
    "status": health_status,
    "phase": phase,
    "snapshot_at": snapshot_at,
    "uptime_seconds": ...,
    "current_card": card_payload,
    "services": {...},
    "timestamp": datetime.now(UTC),
}
```

`StateStore` exposes `last_snapshot: WorkflowSnapshot | None` — a cached in-memory reference to the last successfully saved or loaded snapshot. Updated on every `save()` and after a successful `load()`.

---

### Phase F: Entry Point Integration

In `src/coordinare/__main__.py`:

1. After loading config, construct `StateStore`:
   ```python
   state_store = StateStore(path=config.state_file_path, metrics=METRICS)
   ```

2. Call `verify_writable()` before any graph compilation:
   ```python
   try:
       state_store.verify_writable()
   except OSError as exc:
       logger.error("state_path_not_writable", path=str(config.state_file_path), error=str(exc))
       sys.exit(1)
   ```

3. Pass `state_store` to `CoordinareDaemon(...)`.

---

## Complexity Tracking

No constitution violations requiring justification.

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Torn write on power loss | Low | Low | `os.fsync()` before `os.replace()` ensures bytes reach disk |
| Reconciliation false positive (board API flake) | Low | Medium | Board reconciliation only discards snapshot if API call succeeds and definitively contradicts snapshot; transient failures leave snapshot intact |
| Snapshot written mid-phase (not on transition boundary) | N/A | N/A | Writes gated on `current_phase != previous_phase` — never written on idle cycles |
| `StateLoadError` masking real crashes | Low | Medium | Only `StateLoadError` (explicit) triggers graceful discard; `Exception` subtypes propagate normally and surface as startup failures |
