# Tasks: Performer Environment Caching (060)

**Input**: Design documents from `/specs/060-performer-env-caching/`
**Branch**: `060-performer-env-caching`

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US7)
- File paths are relative to repository root

---

## Phase 1: Setup

No new project or dependency scaffolding required — this feature extends the existing `src/coordinare/` layout with no new external packages.

- [X] T001 Confirm branch `060-performer-env-caching` is checked out and clean

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Data layer, config, and state changes required by every user story.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 Add `GET_FILE_BLOB_SHA_QUERY` GraphQL constant to `src/coordinare/services/github.py` (mirrors `GET_FILE_CONTENT_QUERY` but returns `oid` instead of `text` on the `Blob` fragment)
- [X] T003 Add `get_file_blob_sha(owner, repo, path, ref) -> str | None` method to `GitHubClient` in `src/coordinare/services/github.py` (follows same pattern as `get_file_content()`)
- [X] T004 [P] Create `src/coordinare/models/env_cache.py` with `EnvCacheState` and `BootstrapJobPayload` models (copy field definitions verbatim from `specs/060-performer-env-caching/contracts/env_cache_state.py` and `bootstrap_job.py`)
- [X] T005 [P] Add `env_cache_root: str` (default `~/.coordinare/env-caches/`) to `GlobalConfig` in `src/coordinare/config.py`; expand `~` with `Path.expanduser()` at load time
- [X] T006 Add `env_bootstrap_performer_id: str | None` and `env_spec_file: str` (default `README.md`) to `SymphonyConfig` in `src/coordinare/config.py`
- [X] T007 Add `env_cache: dict[str, Any]` field and matching entry in `initial_state()` to `CoordinareState` in `src/coordinare/graph/state.py`

**Checkpoint**: Data layer complete — all user story phases can now begin.

---

## Phase 3: User Story 1 — Operator Provisions a Fresh Environment on First Start (Priority: P1) 🎯 MVP

**Goal**: On first coordinare start with `env_bootstrap` configured, detect no prior SHA and immediately dispatch an `env_bootstrap` performer that populates the host-path cache volume.

**Independent Test**: Configure a symphony with an `env_bootstrap` performer. Start coordinare. Observe a bootstrap dispatch on the first cycle. Confirm a directory is created at `{env_cache_root}/{symphony_name}/`. Dispatch a regular performer on the next cycle and confirm the env volume is mounted inside its container.

- [X] T008 [US1] Create `src/coordinare/services/env_cache.py` with `sanitise_symphony_name(name: str) -> str` pure function (`re.sub(r"[^a-z0-9_-]", "-", name.lower())`)
- [X] T009 [US1] Add `EnvCacheService.__init__` storing `github_client`, `config`, and `dispatch_fn` dependencies in `src/coordinare/services/env_cache.py`
- [X] T010 [US1] Implement `EnvCacheService.initialise(state)` in `src/coordinare/services/env_cache.py`: seed `EnvCacheState` entries in `state["env_cache"]` for each symphony that has `env_bootstrap_performer_id` set; create `{env_cache_root}/{sanitised_name}/` directories; fetch and store initial blob SHAs via `get_file_blob_sha()`
- [X] T011 [US1] Implement `EnvCacheService.check_and_trigger(symphony, state, dispatch_fn)` in `src/coordinare/services/env_cache.py`: (a) **FR-008 guard** — return immediately if `symphony.env_bootstrap_performer_id is None`; (b) no-prior-SHA path — fetch README content, build `BootstrapJobPayload`, call `dispatch_fn(performer_id, payload)`, set `bootstrap_in_flight = True`. Note: `dispatch_fn` is a closure constructed in the daemon (see T014) that pre-injects the rw volume before calling `start_ephemeral` — `check_and_trigger` itself does not handle volume mounting.
- [X] T012 [US1] Instantiate `EnvCacheService` in `CoordinareDaemon.__init__` in `src/coordinare/__main__.py`
- [X] T013 [US1] Call `env_cache_service.initialise(state["env_cache"])` during daemon startup (before first poll cycle) in `src/coordinare/__main__.py`
- [X] T014 [US1] Call `env_cache_service.check_and_trigger(symphony, state, dispatch_fn)` for each symphony at the top of each poll cycle (before card dispatch) in `src/coordinare/__main__.py`. Construct `dispatch_fn` as a closure here: it calls `get_env_volume_for_symphony(symphony.name, state["env_cache"], is_bootstrap=True)` to get the rw `VolumeMount`, then calls `start_ephemeral(config_with_volume)` directly (bypassing `HTTPPerformerService.dispatch_card` for bootstrap dispatches).

**Checkpoint**: User Story 1 fully functional — first-start bootstrap dispatches correctly and cache dir is created.

---

## Phase 4: User Story 2 — Environment is Automatically Refreshed When README Changes (Priority: P1)

**Goal**: Each poll cycle, compare the current blob SHA to the stored SHA. Dispatch a new bootstrap on mismatch, do nothing on match. Serialize concurrent bootstraps per symphony.

**Independent Test**: Record current README SHA in `CoordinareState`. Edit README on GitHub. Wait one poll cycle. Observe a new `env_bootstrap` dispatch. Confirm the `env_readme_sha` in `CoordinareState` is updated to the new SHA.

- [X] T015 [US2] Implement the SHA-changed path in `EnvCacheService.check_and_trigger()` in `src/coordinare/services/env_cache.py`: compare fetched SHA to `env_cache_state.readme_sha`; if different, fetch content and trigger bootstrap; update stored SHA only after dispatch
- [X] T016 [US2] Implement `EnvCacheService.on_bootstrap_complete(symphony_name, success, state)` in `src/coordinare/services/env_cache.py`: clear `bootstrap_in_flight`, set `last_bootstrap_at` and `last_bootstrap_succeeded`
- [X] T017 [US2] Wire `on_bootstrap_complete` into the bootstrap performer's session-end handler in `src/coordinare/__main__.py` (use existing `session_ended` or equivalent completion callback)
- [X] T018 [US2] Implement `bootstrap_in_flight` guard in `check_and_trigger()` in `src/coordinare/services/env_cache.py`: if `bootstrap_in_flight` is True and SHA differs, store the pending SHA and skip dispatch; on `on_bootstrap_complete()` re-invoke if pending SHA is present
- [X] T019 [US2] Handle GitHub blob SHA fetch failure in `check_and_trigger()` in `src/coordinare/services/env_cache.py`: log warning with `structlog`, preserve last known SHA, do not dispatch

**Checkpoint**: User Stories 1 + 2 fully functional — initial provisioning and README change detection both work; concurrent bootstraps are serialized.

---

## Phase 5: User Story 3 — Each Symphony Has Its Own Isolated Cache (Priority: P2)

**Goal**: Two symphonies produce two separate cache directories. Each performer dispatch receives the correct symphony's volume (read-only), not the other's.

**Independent Test**: Configure two symphonies with distinct `env_bootstrap_performer_id` entries. Start coordinare. Verify two separate directories under `{env_cache_root}/`. Dispatch performers for each symphony and confirm each receives the correct volume.

- [X] T020 [US3] Implement `get_env_volume_for_symphony(symphony_name, env_cache_states, is_bootstrap) -> VolumeMount | None` pure function in `src/coordinare/services/env_cache.py`: returns `VolumeMount(host_path=str(cache_dir), container_path="/devenv", mode="rw" if is_bootstrap else "ro")` when cache dir exists; returns `None` if symphony has no env cache or dir does not exist yet. *Superseded by T033 — container_path updated from root `/devenv` to subdirectory layout and signature extended.*
- [X] T021 [US3] Extend `HTTPPerformerService._dispatch_card_locked()` in `src/coordinare/services/http_performer_service.py` to accept `extra_volumes: list[VolumeMount] | None` parameter and append them to a shallow copy of `self._config` (with updated `volumes` list) before calling `start_ephemeral()` — applies only to containerized performers; subprocess performers are unchanged
- [X] T022 [US3] Pass `get_env_volume_for_symphony(...)` result as `extra_volumes` when calling `dispatch_card()` in the per-symphony performer dispatch path in `src/coordinare/__main__.py`
- [X] T023 [US3] Add config validation in `src/coordinare/config.py`: if `env_bootstrap_performer_id` is set on a symphony, verify the referenced performer `id` exists in `orchestra.performers`; raise `ConfigError` if not found

**Checkpoint**: User Stories 1 + 2 + 3 fully functional — isolation confirmed, correct volumes injected per symphony.

---

## Phase 6: User Story 4 — Operator Controls Env Caching via Config (Priority: P3)

**Goal**: Operator can set a custom `env_cache_root`, enable/disable per symphony, and see bootstrap sessions in the dashboard.

**Acceptance Test**: Set `env_cache_root: /tmp/test-caches` in config. Restart coordinare. Verify cache dir is created at `/tmp/test-caches/{symphony_name}/`. Open dashboard during an active bootstrap and confirm session appears with role `env_bootstrap`.

- [X] T024 [US4] Verify `env_cache_root` path expansion at load time handles both `~/...` and absolute paths in `src/coordinare/config.py` (covered by T005; this task is the explicit verification step)
- [X] T025 [US4] Confirm `env_bootstrap` role label is threaded through to the active-sessions payload in `src/coordinare/__main__.py` — the session must carry role `env_bootstrap` so the existing dashboard session-list rendering shows it without HTML/JS changes; add the label assignment if missing

**Checkpoint**: All four original user stories functional.

---

## Phase 7: Tests & Polish (Phase A–D)

Tests covering the initial four user stories and foundational data layer.

- [X] T026 [P] Write unit tests for `sanitise_symphony_name` edge cases (spaces, Unicode, slashes, collision) in `tests/unit/test_sanitise_symphony_name.py`
- [X] T027 [P] Write unit tests for `EnvCacheService` covering: `initialise()` seeds state, `check_and_trigger()` no-SHA path, `check_and_trigger()` SHA-changed path, `check_and_trigger()` SHA-unchanged (no dispatch), `on_bootstrap_complete()` clears flag and fires queued bootstrap, fetch-failure preserves SHA in `tests/unit/test_env_cache_service.py`
- [X] T028 [P] Write unit tests for `EnvCacheState` and `BootstrapJobPayload` pydantic models (field defaults, required fields, round-trip) in `tests/unit/test_env_cache_models.py`
- [X] T029 Write integration test: mock `GitHubClient.get_file_blob_sha()` to return a new SHA, mock `asyncio.create_subprocess_exec` for docker, dispatch a regular performer for the symphony, assert the `-v` volume flag appears in docker args in `tests/integration/test_env_cache_dispatch.py`
- [X] T030 [P] Run `ruff check` on all new and modified files (`src/coordinare/services/env_cache.py`, `src/coordinare/models/env_cache.py`, `src/coordinare/services/github.py`, `src/coordinare/config.py`, `src/coordinare/graph/state.py`, `src/coordinare/__main__.py`, `src/coordinare/services/http_performer_service.py`)
- [X] T031 [P] Run full test suite with `.venv/bin/pytest tests/` and confirm all tests pass

**Checkpoint**: Phases A–D complete and green — initial four user stories shipped.

---

## Phase 8: User Story 5 — Performer Activates Symphony Environment When Job Starts (Priority: P1)

**Goal**: Every job dispatched to a containerized performer for a symphony with a ready env cache receives `metadata.env_cache_path` pointing to the correct in-container subdirectory. The container path changes from a root mount to a per-symphony subdirectory so ephemeral and persistent modes share a single activation contract.

**Independent Test**: Configure a symphony with a ready env cache and dispatch a regular performer. Inspect the job init payload received by the performer and confirm `metadata.env_cache_path` equals `{container_devenv_root}/{sanitised_name}`. Confirm key is absent when no env cache is configured.

- [X] T032 [US5] Add `container_devenv_root: str` field (default `"/devenv"`) to `PerformerEndpointConfig` in `src/coordinare/models/performer_endpoint.py`; add validation that subprocess performers MUST NOT set a non-default value (mirrors existing subprocess guard pattern)
- [X] T033 [US5] Update `get_env_volume_for_symphony()` in `src/coordinare/services/env_cache.py` to accept `container_devenv_root: str` and use `{container_devenv_root}/{sanitised_name}` as `container_path` instead of `/devenv`; return the resolved in-container path alongside the `VolumeMount` (as a named tuple or `tuple[VolumeMount, str]`)
- [X] T034 [US5] Extend the performer dispatch path in `src/coordinare/graph/nodes/dispatch_performer.py` to inject `env_cache_path` into `JobInitPayload.metadata` when an env volume is present; key is `"env_cache_path"`, value is the resolved container path returned by T033; key MUST be absent when no cache is configured or cache dir is not yet ready
- [X] T035 [US5] Update `EnvCacheService._do_dispatch()` in `src/coordinare/services/env_cache.py` to pass `container_devenv_root` from the bootstrap performer's `PerformerEndpointConfig` when constructing the bootstrap volume mount, so the bootstrap container uses the same subdirectory path as regular performers

**Checkpoint**: Activation contract complete — performers receive `env_cache_path` in job payload pointing to the correct subdirectory.

---

## Phase 9: User Story 6 + 7 — Persistent Performer Multi-Symphony Mounts and State Reset (Priority: P2)

**Goal**: Persistent performers get all ready symphony env caches mounted at startup as read-only subdirectories. Coordinare calls `POST /reset` between jobs to clear performer state.

**Independent Test (US6)**: Configure a persistent performer and two symphonies with ready env caches. Observe both cache dirs mounted under `{container_devenv_root}/` at container start. Dispatch job for symphony A — confirm `env_cache_path` is the symphony A subdirectory.

**Independent Test (US7)**: Run two sequential jobs on a persistent performer. Confirm `POST /reset` is called after job 1 completes and before job 2 is dispatched. Confirm 404 response is non-fatal.

- [X] T036 [US6] Add `_collect_env_volumes_for_persistent_performer(performer_id, env_cache_states, container_devenv_root) -> list[VolumeMount]` function to `src/coordinare/services/env_cache.py`; iterates all ready `EnvCacheState` entries (`cache_dir_ready=True`) and builds one ro `VolumeMount` per symphony under the subdirectory layout `{container_devenv_root}/{sanitised_name}`
- [X] T037 [US6] In `src/coordinare/daemon.py`, before calling `start_ephemeral()` for a persistent performer at container-start time, call `_collect_env_volumes_for_persistent_performer()` from `services/env_cache.py` and pass the result as `extra_volumes` to the dispatch call — following the same pattern established in T021 (`extra_volumes` assembled in the daemon/dispatch layer, not inside `start_ephemeral()`). `start_ephemeral()` in `src/coordinare/services/performer_lifecycle.py` MUST NOT be modified to import or call env-cache code directly.
- [X] T038 [US6] Log a `env_cache.persistent_mount_skipped` warning in `src/coordinare/daemon.py` when a new symphony's env cache becomes `cache_dir_ready` after a persistent performer container is already running (detect by checking `PerformerEndpointState.availability != "unknown"`)
- [X] T039 [US7] Add `async def call_reset(self) -> bool` method to `HTTPPerformerService` in `src/coordinare/services/http_performer_service.py`; POSTs to `{endpoint}/reset`; returns `True` on 2xx, logs warning and returns `False` on 404 or other non-2xx, never raises
- [X] T040 [US7] Call `await svc.call_reset()` in `CoordinareDaemon` after a persistent performer's job completes and before the next dispatch; insert into the existing post-job completion callback path in `src/coordinare/daemon.py`

**Checkpoint**: Persistent performer mode fully functional — multi-symphony mounts and between-job reset both work.

---

## Phase 10: Tests & Polish (Phase E–I)

Tests covering the follow-up user stories (US5–US7) and the activation contract.

- [X] T041 [P] Write unit tests for `get_env_volume_for_symphony()` subdirectory path construction with custom `container_devenv_root` in `tests/unit/test_env_cache_service.py`
- [X] T042 [P] Write unit tests for `_collect_env_volumes_for_persistent_performer()`: multiple ready symphonies produce correct mount list; non-ready symphonies are excluded in `tests/unit/test_env_cache_service.py`
- [X] T043 [P] Write unit tests for `HTTPPerformerService.call_reset()`: 2xx returns `True`; 404 logs warning and returns `False`; other non-2xx logs warning and returns `False` in `tests/unit/test_http_performer_service.py`
- [X] T044 [P] Write unit test asserting `metadata.env_cache_path` is present in dispatched payload when env cache is ready, and absent when not configured in `tests/unit/test_env_cache_service.py`
- [X] T045 Run `ruff check` on all modified files (`src/coordinare/models/performer_endpoint.py`, `src/coordinare/services/env_cache.py`, `src/coordinare/services/http_performer_service.py`, `src/coordinare/graph/nodes/dispatch_performer.py`, `src/coordinare/daemon.py`)
- [X] T046 Run `.venv/bin/pytest tests/` and confirm all tests pass
- [X] T047 [P] Write contract test verifying `env_cache_path` round-trips through `JobInitPayload` serialization → JSON → performer job-init model deserialization; assert field is present and equals expected value when env cache is ready, and absent when not configured in `tests/contract/test_env_cache_payload.py`

---

## Phase 11: User Story 8 — Operator Configures Watched Files Per Symphony (Priority: P2)

**Goal**: Operators can watch multiple spec files per symphony (not just `README.md`). Any change to any watched file triggers a bootstrap. The watched file list is editable from the dashboard Symphonies detail page without a restart.

**Independent Test**: Configure `env_spec_files: [README.md, pyproject.toml]` for a symphony. Edit `pyproject.toml` on GitHub. Wait one poll cycle. Observe a bootstrap dispatch. Edit the list in the dashboard to add `requirements.txt`. Save. Confirm the new file is watched on the next cycle.

- [X] T048 [US8] Rename `SymphonyConfig.env_spec_file: str` → `env_spec_files: list[str]` (default `["README.md"]`) in `src/coordinare/config.py`; update `_validate_env_spec_file` validator to iterate the list and reject entries with a leading `/`; update the `env_bootstrap_performer_id` cross-validator to reference the new field name
- [X] T049 [US8] Update `BootstrapJobPayload` in `src/coordinare/models/env_cache.py`: replace `env_spec_file: str` + `env_spec_content: str` with `env_spec_files: list[str]` + `env_spec_contents: dict[str, str]` (file path → content)
- [X] T050 [US8] Update `EnvCacheService.initialise()` in `src/coordinare/services/env_cache.py` to iterate `symphony.env_spec_files`, call `get_file_blob_sha()` for each, sort the results into a deterministic `dict[str, str]`, serialize to JSON, and store a SHA-256 prefix (12 chars) as the combined `readme_sha` in `EnvCacheState`
- [X] T051 [US8] Update `EnvCacheService.check_and_trigger()` in `src/coordinare/services/env_cache.py` to fetch and combine SHAs for all files in `symphony_config.env_spec_files` using the same deterministic hash used in T050; compare against `cache_state.readme_sha` to decide whether bootstrap is needed
- [X] T052 [US8] Update `EnvCacheService._do_dispatch()` in `src/coordinare/services/env_cache.py` to call `get_file_content()` for each file in `symphony_config.env_spec_files`, collect results into `env_spec_contents: dict[str, str]`, and populate the updated `BootstrapJobPayload` fields from T049
- [X] T053 [US8] Add `env_spec_files` to the `GET /api/symphonies/{name}` response in `src/coordinare/dashboard.py` (return the list from the matched `SymphonyConfig`)
- [X] T054 [US8] Accept and validate `env_spec_files: list[str]` in `POST /api/symphonies` and `PUT /api/symphonies/{name}` in `src/coordinare/dashboard.py`; pass through to `SymphonyConfig` construction; fall back to `["README.md"]` when the key is absent from the PUT body
- [X] T055 [US8] Add a tag-style "Env spec files" section to `loadSymphonyDetail` JS in `src/coordinare/dashboard.py`: render existing files as removable tags, include an add-file input, wire into the Save button payload as `env_spec_files` array
- [X] T056 [P][US8] Write unit tests for combined-SHA logic (`initialise()` and `check_and_trigger()` with multiple files) in `tests/unit/test_060_env_cache.py`; cover: all files unchanged (no dispatch), one file changed (dispatch), first-run with no prior SHA (dispatch), SHA fetch failure for one file (warns, no dispatch)
- [X] T057 [P][US8] Write unit tests for dashboard API `env_spec_files` round-trip in `tests/unit/test_dashboard_symphonies.py` or nearest existing dashboard test file: GET returns list, PUT saves list, missing key in PUT uses default
- [X] T058 [P][US8] Run `ruff check` on all modified files then run `.venv/bin/pytest tests/` and confirm all tests pass and coverage remains ≥ 90%

**Checkpoint**: Phase 11 complete — multi-file watching and dashboard editing both functional; tests green, lint clean.

---

## Phase 12: E2E Integration Tests — Full Lifecycle Coverage

- [X] T059 Create `tests/integration/test_env_cache_lifecycle.py` with 6 test functions covering 10 lifecycle scenarios: full happy-path lifecycle (init → no-change → sha-change → complete → ro/rw volume), pending-SHA queueing, bootstrap failure retry, no-volume-before-ready, no-volume-for-unconfigured, and multi-file combined SHA detection; uses `tmp_path` for real filesystem ops and `AsyncMock` for GitHub + dispatch
- [X] T060 [P] Run `pytest tests/integration/test_env_cache_lifecycle.py -v` and confirm all 6 tests pass

**Checkpoint**: Phase 12 complete — E2E lifecycle validated end-to-end; all 6 tests green.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **BLOCKS all user story phases**
- **Phase 3 (US1)**: Depends on Phase 2
- **Phase 4 (US2)**: Depends on Phase 3 (extends `check_and_trigger()` built in US1)
- **Phase 5 (US3)**: Depends on Phase 2; can run in parallel with Phase 3/4 on separate files
- **Phase 6 (US4)**: Depends on Phases 3–5
- **Phase 7 (Tests A–D)**: Depends on Phase 6
- **Phase 8 (US5)**: Depends on Phase 7 (modifies `get_env_volume_for_symphony()` built in US3)
- **Phase 9 (US6+US7)**: Depends on Phase 8 (T036/T037 use the subdirectory layout from T033)
- **Phase 10 (Tests E–I)**: Depends on Phase 9

### User Story Dependencies

- **US1 (P1)**: Unblocked after Phase 2
- **US2 (P1)**: Depends on US1 (`check_and_trigger()` is extended, not replaced)
- **US3 (P2)**: Depends on Phase 2 for models; volume injection (T020–T022) can start after T008
- **US4 (P3)**: Depends on US1–US3
- **US5 (P1)**: Depends on US3 (modifies `get_env_volume_for_symphony()`) and Phase 7 (tests green)
- **US6 (P2)**: Depends on US5 (uses subdirectory layout from T033)
- **US7 (P2)**: Depends on US5 (persistent performer flow is in place)

### Parallel Opportunities

Within Phase 2: T004, T005, T006 can all run in parallel (different files).

Within Phase 7: T026, T027, T028 can all run in parallel (different test files); T030 can run in parallel with T029.

Within Phase 8: T032, T033, T034 can run in parallel (different files); T035 depends on T033.

Within Phase 9: T036–T037 (US6 volume mounts) can run in parallel with T039–T040 (US7 reset call).

Within Phase 10: T041, T042, T043, T044 can all run in parallel (different test functions, same file or different).

---

## Parallel Example: Phase 2 Foundational

```bash
# These three tasks touch different files and can start simultaneously:
Task: "T004 — Create src/coordinare/models/env_cache.py"
Task: "T005 — Add env_cache_root to GlobalConfig in src/coordinare/config.py"
Task: "T007 — Add env_cache field to CoordinareState in src/coordinare/graph/state.py"

# T002 and T003 (github.py) can run together as a pair:
Task: "T002 — Add GET_FILE_BLOB_SHA_QUERY constant"
Task: "T003 — Add get_file_blob_sha() method"
```

---

## Implementation Strategy

### MVP First (User Stories 1 + 2 only)

1. Complete Phase 2: Foundational
2. Complete Phase 3: US1 — initial bootstrap on first start
3. Complete Phase 4: US2 — SHA change detection + refresh
4. **STOP and VALIDATE**: Bootstrap triggers on README change; single dir created; `bootstrap_in_flight` guard holds
5. Continue to Phase 5 (US3) for multi-symphony isolation

### Incremental Delivery

1. Phase 2 → Foundation ready
2. Phase 3 (US1) → First-start provisioning works ✓ (MVP)
3. Phase 4 (US2) → Auto-refresh on change works ✓
4. Phase 5 (US3) → Per-symphony isolation + volume injection ✓
5. Phase 6 (US4) → Config control + dashboard label ✓
6. Phase 7 → Tests green, lint clean ✓ **(shipped)**
7. Phase 8 (US5) → Subdirectory layout + `env_cache_path` in job payload; activation contract complete ✓
8. Phase 9 (US6+US7) → Persistent performer multi-symphony mounts + state reset ✓
9. Phase 10 → Tests green, lint clean ✓ **(shipped)**

---

## Notes

- `[P]` tasks touch different files with no incomplete task dependencies — safe to run in parallel
- `[Story]` label maps each task to its user story for traceability
- Subprocess performers do not receive the env volume mount — injection applies to containerized (ephemeral/persistent Docker) performers only
- `CoordinareState.env_cache` resets on restart; `initialise()` re-fetches SHAs to re-seed comparison baselines
- `sanitise_symphony_name` always appends a 6-char SHA1 suffix (not just on collision) — every slug is globally unique; same input always produces the same output
- **Mount layout (FR-004/FR-005)**: container path is `{container_devenv_root}/{sanitised_name}/` not `{container_devenv_root}/` root — bootstrap performer and regular performers both use the same subdirectory path, enabling persistent performers to host multiple symphonies without path collision
- `POST /reset` is fire-and-forget from coordinare's perspective: 404 = not implemented (log warning, continue); any other non-2xx = log warning, continue; never blocks the next dispatch
- `metadata.env_cache_path` is **absent** (key not present) when env cache is not configured or cache dir is not yet ready — callers must check for key existence, not a None value
