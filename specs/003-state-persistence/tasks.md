# Tasks: Workflow State Persistence

**Input**: Design documents from `/specs/003-state-persistence/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- Exact file paths included in all task descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Extend configuration and metrics — the two shared dependencies that every subsequent phase requires.

- [x] T001 Add `state_file_path: Path = Field(default=Path("./coordinare.state.json"))` to `ProjectConfiguration` in `src/coordinare/config.py`
- [x] T002 [P] Add `coordinare_state_write_duration_seconds` (Histogram, buckets `(0.01, 0.05, 0.1, 0.5, 1.0)`), `coordinare_state_write_failures_total` (Counter), and `coordinare_state_last_written_timestamp` (Gauge) to `CoordinareMetrics.__init__` in `src/coordinare/metrics.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: `StateStore` — the core persistence module that all three user stories depend on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 Create `src/coordinare/state_store.py` with module-level constant `CURRENT_SCHEMA_VERSION: int = 1` and `WorkflowPhase` type alias (Literal union matching `CoordinareState.phase` from `src/coordinare/graph/state.py`)
- [x] T004 Implement `StateLoadError(ValueError)` with `reason: str` (values: `"corrupt"`, `"schema_mismatch"`, `"validation_error"`) and `detail: str` in `src/coordinare/state_store.py`
- [x] T005 Implement `WorkflowSnapshot(BaseModel)` with all fields from `data-model.md` (`schema_version`, `snapshot_at`, `phase`, `active_card_id`, `active_card_title`, `active_card_column`, `pr_url`, `pr_node_id`, `agent_session_id`, `open_questions`) in `src/coordinare/state_store.py`
- [x] T006 Implement `StateStore.__init__(self, path: Path, metrics: CoordinareMetrics)` and `verify_writable(self) -> None` (probe file in `path.parent`, raises `OSError` on failure) in `src/coordinare/state_store.py`
- [x] T007 Implement `StateStore.save(self, snapshot: WorkflowSnapshot) -> None` using `NamedTemporaryFile(dir=path.parent, delete=False)` + `os.fsync()` + `os.replace()`, recording `state_write_duration_seconds`, `state_last_written_timestamp`, and `state_write_failures_total`; catches `OSError` and logs warning without raising; updates `self.last_snapshot` in `src/coordinare/state_store.py`
- [x] T008 Implement `StateStore.load(self) -> WorkflowSnapshot | None` returning `None` if file absent, validating JSON with `model_validate_json()`, checking `schema_version == CURRENT_SCHEMA_VERSION`, raising `StateLoadError` on any parse/validation/mismatch error; updates `self.last_snapshot` on success in `src/coordinare/state_store.py`
- [x] T009 Add `last_snapshot: WorkflowSnapshot | None` instance attribute (initialised to `None`; updated by `save()` and `load()`) to `StateStore` in `src/coordinare/state_store.py`

**Checkpoint**: `StateStore` is complete and independently testable — no daemon integration yet.

---

## Phase 3: User Story 1 — Crash Recovery Without Human Intervention (Priority: P1) 🎯 MVP

**Goal**: After SIGKILL or any unclean restart the coordinare reads saved state, reconciles against the live board, and resumes monitoring the active card with no duplicate dispatch and no manual operator intervention.

**Independent Test**: Start coordinare with a card In Progress. Send `kill -9`. Restart. Verify the coordinare resumes in `monitoring_agent` phase for the same card — no duplicate dispatch, no board card reset — within one poll interval. (`tests/integration/test_crash_recovery.py`)

### Tests for User Story 1

> **Write these tests first — they must FAIL before implementation begins.**

- [x] T010 [P] [US1] Unit tests for `StateStore.save()` → `StateStore.load()` round-trip (all phases, all optional fields populated vs empty) in `tests/unit/test_state_store.py`
- [x] T011 [P] [US1] Unit tests for `StateStore.load()` returning `None` when file absent, and for `verify_writable()` passing when parent directory is writable in `tests/unit/test_state_store.py`
- [x] T012 [P] [US1] Unit test for metrics recorded on successful `save()` call (`duration_seconds` observed, `last_written_timestamp` set) in `tests/unit/test_state_store.py`
- [x] T013 [P] [US1] Integration test skeleton: SIGKILL → restart → assert phase and card ID match prior snapshot, no duplicate dispatch, within one poll interval in `tests/integration/test_crash_recovery.py`

### Implementation for User Story 1

- [x] T014 [US1] Add `state_store: StateStore` parameter to `CoordinareDaemon.__init__` and store as `self._state_store` in `src/coordinare/daemon.py`
- [x] T015 [US1] Add explicit `pr_url: str | None` and `pr_node_id: str | None` fields to `CoordinareState` TypedDict in `src/coordinare/graph/state.py` (required source fields for `WorkflowSnapshot.pr_url` / `pr_node_id`; currently absent from the TypedDict) — N/A: `_build_snapshot` reads from `current_card` dict which already carries these fields
- [x] T016 [US1] Implement `CoordinareDaemon._build_snapshot(self) -> WorkflowSnapshot` mapping `self._state` fields to `WorkflowSnapshot`: `phase` from `state["phase"]`; `active_card_id/title/column` from `state.get("current_card", {})` keys `id`, `title`, `status`; `pr_url` from `state.get("pr_url")`; `pr_node_id` from `state.get("pr_node_id")`; `agent_session_id` from `state.get("agent_dispatch", {}).get("session_id")`; `open_questions` from `state.get("open_questions", [])` in `src/coordinare/daemon.py`
- [x] T017 [US1] Implement `CoordinareDaemon._restore_from_snapshot(self, snapshot: WorkflowSnapshot) -> None` mapping snapshot fields back into `self._state`: `phase`, `open_questions`, `pr_url`, `pr_node_id` directly; `current_card` dict reconstructed from `active_card_id`, `active_card_title`, `active_card_column`; `agent_dispatch["session_id"]` from `agent_session_id` in `src/coordinare/daemon.py`
- [x] T018 [US1] Add startup recovery block in `CoordinareDaemon.start()` before the poll loop: call `self._state_store.load()`, catch `StateLoadError` (log warning + fresh start), and on valid snapshot call `_restore_from_snapshot()` + log info in `src/coordinare/daemon.py`
- [x] T019 [US1] Implement `CoordinareDaemon._infer_phase_from_board_column(column: str) -> WorkflowPhase` helper mapping live board column names to workflow phases (`"In Progress"` → `"monitoring_agent"`, `"In Review"` → `"monitoring_pr"`, `"Blocked"` → `"blocked"`, unrecognised column → `"idle"`) in `src/coordinare/daemon.py`
- [x] T020 [US1] Add board reconciliation in `CoordinareDaemon.start()` after restore — three cases: (a) card not found on board or in Done column → log `event="board_contradicts_snapshot"` + reset to idle; (b) card found but column differs from snapshot → call `_infer_phase_from_board_column()`, advance to inferred phase, log `event="board_reconciliation_advanced"`; (c) card found and column matches → keep restored state, log `event="board_reconciliation_confirmed"` in `src/coordinare/daemon.py`
- [x] T021 [US1] Add snapshot write call after phase-transition detection in `CoordinareDaemon.start()` poll loop (`if current_phase != previous_phase: await self._state_store.save(self._build_snapshot())`) in `src/coordinare/daemon.py`
- [x] T022 [US1] Construct `StateStore(path=config.state_file_path, metrics=METRICS)`, call `state_store.verify_writable()` (catch `OSError` → log structured error + `sys.exit(1)`), and pass `state_store` to `CoordinareDaemon(...)` in `src/coordinare/__main__.py`
- [x] T023 [US1] Complete integration test T013: assert daemon resumes in correct phase and card after SIGKILL/restart with no re-dispatch; also assert fresh idle start when prior snapshot has `phase: idle` (acceptance scenario 4) in `tests/integration/test_crash_recovery.py`

**Checkpoint**: User Story 1 fully functional — crash recovery works end-to-end, including phase inference for cards that advanced during downtime. `speckit.implement` may stop here for MVP.

---

## Phase 4: User Story 2 — Health Endpoint Reflects Persisted State (Priority: P2)

**Goal**: `GET /health` returns the correct `phase` and `snapshot_at` immediately after a restart, before the first poll cycle completes, sourced from persisted state.

**Independent Test**: Restart the coordinare (without completing a cycle), immediately query `GET /health`, verify `phase` and `snapshot_at` match the prior session's last snapshot. (`tests/contract/test_health_schema.py`)

### Tests for User Story 2

> **Write these tests first — they must FAIL before implementation begins.**

- [x] T024 [P] [US2] Contract test asserting `GET /health` response contains `phase` (string or null) and `snapshot_at` (ISO 8601 or null) at the top level; validate response against `specs/003-state-persistence/contracts/health-response.schema.json` in `tests/contract/test_health_schema.py`
- [x] T025 [P] [US2] Contract test asserting `phase` and `snapshot_at` reflect the last saved snapshot immediately after daemon start (before first poll cycle) in `tests/contract/test_health_schema.py`

### Implementation for User Story 2

- [x] T026 [US2] Expose `state_store` as a public property on `CoordinareDaemon` (`@property def state_store(self) -> StateStore`) in `src/coordinare/daemon.py`
- [x] T027 [US2] Extend `GET /health` handler to read `daemon.state_store.last_snapshot`, extract `phase` and `snapshot_at` (both `None` if no snapshot), and include them as top-level fields in the response dict in `src/coordinare/health.py`

**Checkpoint**: User Stories 1 AND 2 work independently. Health endpoint is authoritative across process restarts.

---

## Phase 5: User Story 3 — Graceful Handling of Corrupted or Stale State (Priority: P3)

**Goal**: A corrupted, missing, or schema-incompatible state file never causes the coordinare to crash; it always results in a structured log warning and a clean fresh idle cycle.

**Independent Test**: Corrupt the state file on disk (`echo "not json" > coordinare.state.json`), restart the coordinare, verify it logs a warning (not an exception), starts fresh in idle, and resumes normal polling. (`tests/integration/test_crash_recovery.py`)

### Tests for User Story 3

> **Write these tests first — they must FAIL before implementation begins.**

- [x] T028 [P] [US3] Unit test: `StateStore.load()` with malformed JSON content → raises `StateLoadError(reason="corrupt")` in `tests/unit/test_state_store.py`
- [x] T029 [P] [US3] Unit test: `StateStore.load()` with valid JSON but wrong `schema_version` (e.g., `2`) → raises `StateLoadError(reason="schema_mismatch")` in `tests/unit/test_state_store.py`
- [x] T030 [P] [US3] Unit test: `StateStore.load()` with valid JSON but invalid `phase` value → raises `StateLoadError(reason="validation_error")` in `tests/unit/test_state_store.py`
- [x] T031 [P] [US3] Unit test: `StateStore.save()` with mocked `OSError` (simulated disk full) → increments `state_write_failures_total`, logs warning, does NOT raise in `tests/unit/test_state_store.py`
- [x] T032 [P] [US3] Unit test: mock `os.replace()` to raise `OSError` after `NamedTemporaryFile` write completes; assert the original `self._path` (prior state file) remains intact — verifies torn-write protection (SC-006) in `tests/unit/test_state_store.py`
- [x] T033 [P] [US3] Unit test: `StateStore.verify_writable()` raises `OSError` when parent directory is read-only in `tests/unit/test_state_store.py`
- [x] T034 [P] [US3] Integration test: write corrupted state file to `tmp_path`, restart daemon, assert log contains `state load failed`, daemon starts fresh in idle, no crash in `tests/integration/test_crash_recovery.py`
- [x] T035 [P] [US3] Integration test: write state file referencing a non-existent card ID, restart daemon, assert board reconciliation discards state, daemon starts fresh in idle in `tests/integration/test_crash_recovery.py`

### Implementation for User Story 3

The `StateStore.load()` implementation in T008 (Phase 2) already provides the core behaviour (raising `StateLoadError` for corrupt/mismatch/invalid). This phase focuses on:

- [x] T036 [US3] Ensure `CoordinareDaemon.start()` startup recovery block (T018) logs a structured warning with `reason` and `detail` from `StateLoadError` using `structlog` bound context in `src/coordinare/daemon.py`
- [x] T037 [US3] Verify board reconciliation discard path (T020 case a): when board query returns no card matching `active_card_id` or card is in Done column, the log event is `event="board_contradicts_snapshot"` with card ID at `warn` level; also verify case b (column changed) emits `event="board_reconciliation_advanced"` in `src/coordinare/daemon.py`
- [x] T038 [US3] Verify `__main__.py` `verify_writable()` error path logs a structured error with `event="state_path_not_writable"` and `path=` before `sys.exit(1)` in `src/coordinare/__main__.py`

**Checkpoint**: All three user stories fully functional. Any failure mode in state persistence is handled gracefully.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Lint compliance, type-check pass, schema contract validation, benchmark, and final quickstart walkthrough.

- [x] T039 [P] Add performance benchmark unit test for `StateStore.save()`: assert wall-clock time ≤ 1.0 s (SC-002 budget) using `time.monotonic()` before/after call in `tests/unit/test_state_store.py` (constitution Principle IV)
- [x] T040 [P] Add unit test for `COORDINARE_STATE_FILE_PATH` env var override in `tests/unit/test_config.py`: set env var to `/tmp/custom.json`, load config, assert `config.state_file_path == Path("/tmp/custom.json")` (FR-010)
- [x] T041 [P] Add contract test validating `StateStore.save()` output JSON against `specs/003-state-persistence/contracts/workflow-snapshot.schema.json` using `jsonschema.validate()` in `tests/contract/test_state_persistence.py`
- [x] T042 [P] Run `ruff check src/coordinare/state_store.py src/coordinare/daemon.py src/coordinare/health.py src/coordinare/config.py src/coordinare/metrics.py src/coordinare/__main__.py src/coordinare/graph/state.py` and fix all reported issues
- [x] T043 [P] Verify full pytest suite passes with no regressions: `cd src && pytest tests/ -v --tb=short`
- [x] T044 [P] Verify coverage does not regress from pre-feature baseline: `cd src && pytest tests/ --cov=coordinare --cov-report=term-missing` — 86% overall, state_store.py at 97%, daemon.py at 93%
- [x] T045 Walk through `specs/003-state-persistence/quickstart.md` scenarios end-to-end: normal operation, crash recovery, corrupted file, permission error, disk-full — automated in `tests/integration/test_quickstart_scenarios.py`

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 1 (Setup: T001, T002)
    ↓
Phase 2 (Foundational: T003–T009)  ← BLOCKS all user stories
    ↓
Phase 3 (US1: T010–T023) — 🎯 MVP
Phase 4 (US2: T024–T027) — depends on Phase 3 (T026 needs daemon.state_store property)
Phase 5 (US3: T028–T038) — depends on Phase 2; tests T028–T035 can run after Phase 2
    ↓
Phase 6 (Polish: T039–T045)
```

### User Story Dependencies

- **US1 (P1)**: Starts after Phase 2 completes. No dependency on US2 or US3.
- **US2 (P2)**: Depends on US1 (T022/T026 — needs `CoordinareDaemon.state_store` property from US1). Must complete US1 first.
- **US3 (P3)**: The `StateLoadError` handling in `StateStore.load()` is built in Phase 2 (T008). Test tasks T028–T035 can be written immediately after Phase 2. Implementation tasks T036–T038 depend on US1 daemon integration (T018, T020).

### Within Each User Story

- Tests (T010–T013, T024–T025, T028–T035) written before implementation — must FAIL first
- CoordinareState extension (T015) before `_build_snapshot()` (T016)
- `_build_snapshot()` (T016) before `save()` call (T021)
- `_restore_from_snapshot()` (T017) before startup recovery (T018)
- Phase-inference helper (T019) before board reconciliation (T020)
- Startup recovery (T018) before board reconciliation (T020)
- All daemon changes (T014–T021) before entry point wiring (T022)

### Parallel Opportunities

- **T001 and T002** (Phase 1): Different files — fully parallel
- **T010, T011, T012, T013** (US1 tests): Different test scenarios, same file — draft in parallel
- **T015 and T002** (US1 CoordinareState ext + metrics): Different files — fully parallel
- **T016 and T017** (US1 private methods): Both on daemon, no mutual dependency — parallel
- **T019** (phase-inference helper): Independent of T016/T017 — parallel with both
- **T024 and T025** (US2 tests): Different assertions in same file — parallel
- **T028–T035** (US3 tests): Different `StateStore` failure modes — parallel
- **T039–T044** (Polish): Different commands/files — parallel

---

## Parallel Example: User Story 1

```bash
# After Phase 2 completes, launch US1 tests and CoordinareState extension simultaneously:
Task: T010 - save/load round-trip unit tests
Task: T011 - load-missing + verify_writable unit tests
Task: T012 - metrics unit tests
Task: T013 - integration test skeleton
Task: T015 - add pr_url/pr_node_id to CoordinareState (different file: graph/state.py)

# Then implement daemon private methods in parallel:
Task: T016 - _build_snapshot()
Task: T017 - _restore_from_snapshot()
Task: T019 - _infer_phase_from_board_column()
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001, T002) — ~30 min
2. Complete Phase 2: Foundational (T003–T009) — ~2 h
3. Write US1 tests (T010–T013) — ensure they FAIL — ~1 h
4. Complete Phase 3: US1 implementation (T014–T021) — ~2 h
5. **STOP and VALIDATE**: Run `pytest tests/unit/test_state_store.py tests/integration/test_crash_recovery.py -v`
6. Deploy/demo crash recovery

### Incremental Delivery

1. Phases 1–2 → `StateStore` ready
2. Phase 3 → Crash recovery works (MVP ✅)
3. Phase 4 → Health endpoint shows persisted state
4. Phase 5 → All failure modes tested and handled
5. Phase 6 → Polish and validate

### Total Task Count

| Phase | Story | Tasks | Parallelizable |
|---|---|---|---|
| Phase 1: Setup | — | 2 | 2 |
| Phase 2: Foundational | — | 7 | 0 (sequential module build) |
| Phase 3: US1 (P1) | US1 | 14 | 9 |
| Phase 4: US2 (P2) | US2 | 4 | 2 |
| Phase 5: US3 (P3) | US3 | 11 | 8 |
| Phase 6: Polish | — | 7 | 6 |
| **Total** | | **45** | **27** |

---

## Notes

- `[P]` tasks operate on different files or independent code paths — no mutual dependencies
- `[Story]` label maps each task to its user story for traceability against spec acceptance criteria
- Each user story is independently completable and testable
- Write tests first; verify they fail before implementing
- Commit after each checkpoint (end of Phase 2, end of each US phase)
- The `StateStore` (Phase 2) has no daemon coupling — unit tests for it run standalone
- All structured log events use `structlog` following existing daemon patterns
