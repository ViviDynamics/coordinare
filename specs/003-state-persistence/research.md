# Research: Workflow State Persistence

**Feature**: 003-state-persistence
**Branch**: `003-state-persistence`
**Date**: 2026-02-22

---

## Storage Format

**Decision**: JSON file, round-tripped via Pydantic v2 `.model_dump_json()` / `model_validate_json()`

**Rationale**:
- Human-readable and directly debuggable without tooling (`cat coordinare.state.json`)
- Zero additional dependencies — Python stdlib `json` module underlies Pydantic's JSON engine
- Supports schema versioning via a top-level `schema_version` integer field
- Pydantic v2 validation on load catches corrupted or incompatible files with informative error messages
- State payload is tiny (< 1 KB for a single active card) — no size or performance pressure

**Alternatives considered**:

| Alternative | Why Rejected |
|-------------|--------------|
| SQLite via `aiosqlite` | Supports transactions and concurrent access, but single-writer assumption makes ACID overkill; binary format requires tooling to inspect; adds a dependency |
| Pickle | Fast, no dependency — but binary, Python-version-sensitive, and unsafe to load from untrusted or corrupted bytes |
| MessagePack / CBOR | Compact binary — but requires an extra dependency; no benefit for sub-1 KB payloads |
| TinyDB | Simple document store — but adds a dependency on top of JSON with no functional benefit for a single-record store |

---

## Atomic Write Strategy

**Decision**: Write to `tempfile.NamedTemporaryFile(dir=target_dir, delete=False)`, then `os.replace(tmp_path, target_path)`

**Rationale**:
- `os.replace()` maps to `rename(2)` on POSIX systems. The kernel atomically updates the directory entry — readers see either the old state or the new state, never a partially written file
- The temp file **must be on the same filesystem** as the target to guarantee atomic rename; using `dir=target_dir` ensures this
- `NamedTemporaryFile(delete=False)` yields a named path suitable for `os.replace()`; if the rename fails (e.g., disk full), the temp file is left behind but the original state file is intact
- On disk-full: `os.replace()` raises `OSError`; the coordinare catches this, logs a warning, and continues running in memory with persistence degraded

**Verification**: Confirmed in Python 3.12 docs and POSIX.1-2017 §4.12. `rename(2)` is atomic with respect to readers on the same filesystem. This is the standard pattern used by editors, databases, and package managers for safe file replacement.

---

## LangGraph Integration Point

**Decision**: Write snapshot when phase changes, immediately after `self._state = await self._graph.ainvoke(self._state)` in `daemon.py`

**Rationale**:
- `ainvoke()` completes a full poll cycle and updates `self._state` — the phase field reflects the new stable state
- Phase transition detection already exists in `daemon.py` (lines 109–119): `if current_phase != previous_phase` triggers a log event. Piggyback the snapshot write on this exact condition
- Writing on every cycle (including idle→idle) would generate unnecessary I/O and inflate metrics counters without durability benefit — idle state is safe to recompute on restart
- For non-idle phases (`monitoring_agent`, `monitoring_pr`, `blocked`), the phase rarely changes cycle-to-cycle after initial entry; a single write on phase entry is sufficient to survive a crash

**Startup recovery sequence**:

```
StateStore.load()
    │
    ├─ None (file absent)         → fresh start, log info("no prior state found")
    │
    ├─ StateLoadError (corrupt,   → log warning(reason), fresh start
    │   schema mismatch, invalid) →
    │
    └─ WorkflowSnapshot (valid)
           │
           query live board (GitHub Projects API)
           │
           ├─ card still In Progress/In Review on board → restore snapshot into self._state
           │
           └─ card missing, Done, or column mismatch   → log warning("board contradicts snapshot"),
                                                           fresh start (board is authoritative)
```

---

## Pydantic Model

**Decision**: `WorkflowSnapshot(BaseModel)` from Pydantic v2

**Rationale**:
- Pydantic v2 is already a transitive dependency via `pydantic-settings` (used in `config.py`) — no new dependency
- `.model_dump_json()` and `model_validate_json()` provide clean, type-safe round-trip serialisation
- `ValidationError` on load provides a structured, loggable reason for discarding corrupted state
- `Field(default=...)` and `Optional` fields cleanly model the phase-conditional presence of PR URLs and agent session IDs

**Schema version strategy**:
- `schema_version: int = Field(default=1)` embedded in the model
- On load: compare `snapshot.schema_version` against `CURRENT_SCHEMA_VERSION = 1`; if different, raise `StateLoadError(reason="schema_mismatch")`
- No migration tooling required for this feature; future specs may add migration as a separate concern

---

## Prometheus Metrics

**Decision**: Add three metrics to `CoordinareMetrics` in `metrics.py`, following the existing `CollectorRegistry` pattern

| Metric name | Type | Buckets / Labels | Purpose |
|---|---|---|---|
| `coordinare_state_write_duration_seconds` | Histogram | `(0.01, 0.05, 0.1, 0.5, 1.0)` | Latency of each atomic write (temp+rename) |
| `coordinare_state_write_failures_total` | Counter | — | Write attempts that raised `OSError` |
| `coordinare_state_last_written_timestamp` | Gauge | — | Unix epoch of the last successful write |

**Rationale**:
- Consistent with the `coordinare_` prefix convention used by all existing metrics
- The Histogram buckets cover the normal range (< 10 ms on local SSD) up to the 1.0 s budget from SC-002
- `state_last_written_timestamp` as a Gauge lets operators alert on stale persistence (e.g., `time() - last_written > poll_interval * 3`)
- `StateStore` receives a `CoordinareMetrics` instance at construction, matching the pattern where services receive dependencies rather than importing the `METRICS` singleton directly (improves testability)

---

## Startup Write-Permission Validation

**Decision**: `StateStore.verify_writable()` — creates and immediately removes a probe file in the state file's parent directory

**Rationale**:
- FR-011: if the configured path is not writable the coordinare must log a structured error and exit with a non-zero code
- A probe write confirms both directory existence and write permission without touching the actual state file
- Called once in `__main__.py` before building the LangGraph and starting the daemon — fail-fast before any graph compilation or service initialisation

---

## Health Endpoint Extension

**Decision**: Add `phase` and `snapshot_at` fields to the `GET /health` response, sourced from the `StateStore`

**Rationale**:
- FR-007: the health endpoint must report the active card and phase immediately on startup, before the first poll cycle
- Currently `phase` is not exposed in the health response at all — this feature adds it
- `snapshot_at` (the timestamp of the last successful write) gives operators visibility into persistence freshness
- The `CoordinareDaemon` holds a reference to the `StateStore`; the health app accesses it via `daemon.state_store`, consistent with how it already accesses `daemon.state`

---

## Source Code Impact Summary

| File | Change |
|---|---|
| `src/coordinare/state_store.py` | **New** — `WorkflowSnapshot` model, `StateStore` class, `StateLoadError` |
| `src/coordinare/config.py` | Add `state_file_path: Path = Path("./coordinare.state.json")` |
| `src/coordinare/metrics.py` | Add 3 state persistence metrics to `CoordinareMetrics` |
| `src/coordinare/daemon.py` | Accept `StateStore`, call `load()` on startup, call `save()` on phase transition |
| `src/coordinare/health.py` | Expose `phase` and `snapshot_at` from `StateStore` |
| `src/coordinare/__main__.py` | Construct `StateStore`, call `verify_writable()`, pass to daemon |
| `tests/unit/test_state_store.py` | **New** — unit tests for save/load/corrupt/missing/mismatch/disk-full |
| `tests/integration/test_crash_recovery.py` | **New** — SIGKILL → restart → verify phase/card recovery |
| `tests/contract/test_health_schema.py` | Add contract assertions for `phase` and `snapshot_at` fields |
