# Implementation Plan: Performer Environment Caching

**Branch**: `060-performer-env-caching` | **Date**: 2026-05-08 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/060-performer-env-caching/spec.md`

## Summary

Coordinare monitors each symphony's configurable list of spec files (`env_spec_files`, default `["README.md"]`) for content changes via GitHub blob SHA polling. Any change to any watched file triggers a bootstrap. The list is editable per symphony from the dashboard without a restart. When a change is detected, coordinare dispatches a dedicated `env_bootstrap` ephemeral performer that installs languages, tools, and packages into a per-symphony host-path volume (`{env_cache_root}/{symphony_name}/`). All subsequent containerized performers dispatched for that symphony receive the volume mounted read-only at `/devenv`, giving them a pre-built dev environment without burning tokens on re-installation.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: pydantic v2, pydantic-settings, structlog, asyncio (stdlib), gql[aiohttp] (existing GraphQL client), docker CLI via `asyncio.create_subprocess_exec` (existing pattern from spec 056)
**Storage**: Host filesystem only — `{env_cache_root}/{symphony_name}/`; in-memory `EnvCacheState` in `CoordinareState`
**Testing**: pytest (existing), pytest-asyncio, `unittest.mock`
**Target Platform**: Linux/macOS coordinare host
**Project Type**: Single project (src/coordinare/)
**Performance Goals**: SHA fetch per symphony per cycle must complete within the existing poll budget (one GraphQL query, same as existing doc fetch calls)
**Constraints**: No new external dependencies; no persistent storage beyond host filesystem; no concurrent bootstraps per symphony

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Code Quality First | ✅ | No new dependencies required; extends existing VolumeMount + dispatcher patterns. |
| II. Testing Discipline | ✅ | Unit tests for EnvCacheService, SHA detection, name sanitisation, volume injection; integration tests for dispatch path. |
| III. UX Consistency | ✅ | Bootstrap sessions use existing session display; dashboard label follows existing role-label pattern. |
| IV. Performance by Design | ✅ | Blob SHA query is one GraphQL call per symphony per cycle — well within rate limits. Cache miss (no env yet) leaves performers unchanged. |
| V. Clarity Before Action | ✅ | All clarifications resolved in spec. No NEEDS CLARIFICATION tags remain. |

**Post-design re-check**: No violations. `EnvCacheService` has a single responsibility; `get_env_volume_for_symphony` is a pure function; `sanitise_symphony_name` is deterministic and tested.

---

## Project Structure

### Documentation (this feature)

```text
specs/060-performer-env-caching/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── bootstrap_job.py         # BootstrapJobPayload model
│   ├── env_cache_state.py       # EnvCacheState model
│   └── github_query.graphql     # GET_FILE_BLOB_SHA_QUERY
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                          # + env_cache_root (global), env_bootstrap_performer_id + env_spec_files (symphony)
├── models/
│   └── env_cache.py                   # NEW: EnvCacheState, BootstrapJobPayload
├── services/
│   ├── env_cache.py                   # NEW: EnvCacheService
│   └── github.py                      # + get_file_blob_sha() method + GET_FILE_BLOB_SHA_QUERY
├── graph/
│   └── state.py (or state_store.py)   # + env_cache field on CoordinareState
└── daemon.py                          # Wire EnvCacheService into poll cycle

tests/
├── unit/
│   ├── test_env_cache_service.py      # NEW
│   ├── test_env_cache_models.py       # NEW
│   └── test_sanitise_symphony_name.py # NEW
└── integration/
    └── test_env_cache_dispatch.py     # NEW
```

**Structure Decision**: Single project, extending existing src/coordinare/ layout. New service and model files follow existing naming patterns (services/rebase.py, models/performer_endpoint.py, etc.).

---

## Complexity Tracking

No constitution violations requiring justification.

---

## Implementation Phases

### Phase A: Data Layer

1. Add `GET_FILE_BLOB_SHA_QUERY` to `services/github.py` (new constant, mirrors `GET_FILE_CONTENT_QUERY` but returns `oid` instead of `text`).
2. Add `get_file_blob_sha(owner, repo, path, ref) -> str | None` to `GitHubClient`.
3. Add `EnvCacheState` and `BootstrapJobPayload` to new `models/env_cache.py`.
4. Add `env_cache_root` to `GlobalConfig`, expand `~` at load time with `Path.expanduser()`.
5. Add `env_bootstrap_performer_id` and `env_spec_file` to `SymphonyConfig`.
6. Add validation: if `env_bootstrap_performer_id` set, referenced performer must exist.
7. Add `env_cache: dict[str, EnvCacheState]` to `CoordinareState`.

### Phase B: EnvCacheService

8. Implement `src/coordinare/services/env_cache.py`:
   - `sanitise_symphony_name(name: str) -> str` (pure function, collision-safe)
   - `EnvCacheService.__init__` — stores dependencies
   - `EnvCacheService.initialise(state)` — seed state, create cache dirs, fetch initial SHAs
   - `EnvCacheService.check_and_trigger(symphony, state, dispatch_fn)` — per-cycle SHA check + bootstrap dispatch
   - `EnvCacheService.on_bootstrap_complete(symphony_name, success, state)` — update in-flight flag

### Phase C: Volume Injection

9. Implement `get_env_volume_for_symphony(symphony_name, env_cache_states, is_bootstrap) -> VolumeMount | None` as a pure function (can live in `services/env_cache.py` or `models/env_cache.py`).
10. Extend the performer dispatch path (the code that calls `start_ephemeral`) to call `get_env_volume_for_symphony` and append the result to `config.volumes` before dispatch. Apply only for containerized (ephemeral/persistent) performers.

### Phase D: Daemon Integration

11. Instantiate `EnvCacheService` in `CoordinareDaemon.__init__`.
12. Call `env_cache_service.initialise(state["env_cache"])` during daemon startup (before first poll).
13. Call `env_cache_service.check_and_trigger(...)` for each symphony at the start of each poll cycle (before card dispatch).
14. Wire `on_bootstrap_complete` callback into the bootstrap performer's completion handler.

### Phase E: Tests & Dashboard

15. Write unit tests: `test_env_cache_service.py`, `test_env_cache_models.py`, `test_sanitise_symphony_name.py`.
16. Write integration test: `test_env_cache_dispatch.py` (mock GitHub client + mock docker, verify volume is injected).
17. Dashboard: bootstrap sessions already use the existing session display; verify `env_bootstrap` role label renders correctly in the active sessions list (no new HTML/JS required if the label is just passed through).

---

## Follow-up Phases (FR-013 – FR-016)

### Phase F: Activation Contract — `env_cache_path` in Payload

18. Add `container_devenv_root: str` (default `"/devenv"`) to `PerformerEndpointConfig`; add subprocess guard validation.
19. Update `get_env_volume_for_symphony()` to accept `container_devenv_root` and use `{container_devenv_root}/{sanitised_name}` as the container path (subdirectory layout, not root mount). Return the resolved in-container path alongside the `VolumeMount`.
20. In the performer dispatch path (`dispatch_performer.py`), inject `metadata["env_cache_path"]` into the `JobInitPayload` when an env volume is present. Key absent when no cache is configured or not yet ready.
21. Update bootstrap dispatch in `EnvCacheService._do_dispatch()` to use the same subdirectory path, sourcing `container_devenv_root` from the bootstrap performer's config.

### Phase G: Persistent Performer Multi-Symphony Mounts

22. Add `_collect_env_volumes_for_persistent_performer(performer_id, env_cache_states, container_devenv_root) -> list[VolumeMount]` to `services/env_cache.py`. Iterates all ready `EnvCacheState` entries; produces one ro mount per symphony under the subdirectory layout.
23. In `start_ephemeral()`, when starting a persistent performer, call the above function and prepend the mounts to the container args. Coordinare passes env cache state via a new optional parameter (defaults to `None`).
24. Log `env_cache.persistent_mount_skipped` warning in `daemon.py` when a new symphony's env cache becomes ready after a persistent performer is already running.

### Phase H: Persistent Performer State Reset

25. Add `async def call_reset(self) -> bool` to `HTTPPerformerService`; POSTs to `{endpoint}/reset`; returns `True` on 2xx, logs warning and returns `False` on 404 or other non-2xx — never raises.
26. Call `await svc.call_reset()` in `CoordinareDaemon` after a persistent performer's job completes and before the next dispatch (insert into existing post-job completion callback path).

### Phase I: Tests (Follow-up)

27. Unit tests for subdirectory path construction in `get_env_volume_for_symphony()` with custom `container_devenv_root`.
28. Unit tests for `_collect_env_volumes_for_persistent_performer()`: multiple ready symphonies produce correct mount list; non-ready excluded.
29. Unit tests for `HTTPPerformerService.call_reset()`: 2xx → True; 404 → False + warning; other non-2xx → False + warning.
30. Unit test asserting `metadata.env_cache_path` present when cache ready, absent when not configured.

### Phase J: Configurable Watched Files + Dashboard UI (US8)

**User Story 8**: Operator configures which files trigger a bootstrap, editable without restart.

- Acceptance: configure `env_spec_files: [README.md, pyproject.toml]`; editing either file on GitHub triggers a bootstrap; the list is editable on the Symphonies detail page in the dashboard and takes effect on the next poll cycle.

**Config change**:

31. Rename `SymphonyConfig.env_spec_file: str` → `env_spec_files: list[str]` (default `["README.md"]`) in `src/coordinare/config.py`. Update the relative-path validator to iterate the list. Update the `env_bootstrap_performer_id` cross-validator to reference the new field name.

**Data model change**:

32. Update `BootstrapJobPayload` in `src/coordinare/models/env_cache.py`: replace `env_spec_file: str` + `env_spec_content: str` with `env_spec_files: list[str]` + `env_spec_contents: dict[str, str]` (file path → content).

**Combined SHA**:

33. `EnvCacheService.initialise()` and `check_and_trigger()` iterate `env_spec_files`, fetch one blob SHA per file, sort the results into a deterministic `{path: sha}` dict, serialize to JSON, and store a 12-char SHA-256 prefix as the combined `readme_sha` in `EnvCacheState`. A single changed file changes the combined hash; order-independent.

34. `EnvCacheService._do_dispatch()` calls `get_file_content()` for each file and populates `env_spec_contents` in the `BootstrapJobPayload`.

**Dashboard API**:

35. `GET /api/symphonies/{name}` — add `env_spec_files` to response.
36. `POST /api/symphonies` and `PUT /api/symphonies/{name}` — accept `env_spec_files: list[str]`; pass to `SymphonyConfig` construction; fall back to `["README.md"]` when absent.

**Dashboard UI**:

37. Add a tag-style "Env spec files" section to `loadSymphonyDetail` JS in `dashboard.py`: existing files render as removable tags; an add-file input appends new entries; the Save button includes the list as `env_spec_files` in the PUT payload.

**Tests**:

38. Unit tests for combined-SHA logic: all files unchanged (no dispatch), one file changed (dispatch), first-run no prior SHA (dispatch), one SHA fetch failure (warn, no dispatch).
39. Unit tests for dashboard API `env_spec_files` round-trip: GET returns list, PUT saves list, missing key uses default.


---

## Phase 12: E2E Integration Tests — Full Lifecycle Coverage

**Goal**: Validate the complete env-caching pipeline end-to-end using real filesystem operations (`tmp_path`) and mocked GitHub/dispatch. Covers 10 distinct scenarios that cannot be verified by unit tests alone.

**File**: `tests/integration/test_env_cache_lifecycle.py`

**Scenarios covered**:
1. `initialise()` — cache dir created, state seeded, initial combined SHA stored
2. `check_and_trigger()` — no dispatch when SHA unchanged
3. `check_and_trigger()` — bootstrap dispatched on SHA change; payload fields correct
4. `on_bootstrap_complete(success=True)` — state updated; `readme_sha` preserved
5. `get_env_volume_for_symphony()` — ro mount returned for regular performers
6. `get_env_volume_for_symphony()` — rw mount returned for bootstrap performers
7. Pending-SHA queueing — second change while bootstrap is in-flight is queued, not lost
8. Bootstrap failure — `readme_sha` cleared so next cycle retries
9. No volume returned when `cache_dir_ready=False`
10. Multi-file combined SHA — dispatch triggered when any watched file changes

**Tasks**: T059–T060 (see tasks.md)

**Checkpoint**: Phase 12 complete — E2E lifecycle validated end-to-end; all 6 test functions green.
