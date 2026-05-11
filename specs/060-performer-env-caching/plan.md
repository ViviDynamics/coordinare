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


---

## Phase 13: Post-Merge QA Fixes (Branch `061-passive-phase-board-pickup`, PR #79)

**Context**: Spec 060 merged via PR #78. During the first live QA cycle on the
`website` symphony, the QA performer reported missing tools (ruby, docker)
despite a populated env spec, and the daemon began surfacing
`Transport is already connected` / `Connector is closed` errors that tripped
the GitHub circuit breaker. Bundled into the same QA cycle branch (#79) along
with the original IN_REVIEW pickup fix.

### Fix 1: Defer `cache_dir_ready` until first successful bootstrap

**Symptom**: `~/.coordinare/env-caches/website-3ab3e0/` was created but empty;
performers mounted it ro and found nothing installed; `check_and_trigger`
never re-dispatched because the seeded SHA already matched upstream.

**Root cause**: `EnvCacheService.initialise()` seeded `readme_sha` from the
live GitHub blob and flipped `cache_dir_ready=True` the moment the host
directory existed. The two were conflated — the directory existing is not
the same as a bootstrap having populated it.

**Fix**:
- `initialise()` now only creates the host dir; `readme_sha` stays `None`
  and `cache_dir_ready` stays `False`.
- The first `check_and_trigger()` cycle dispatches a real bootstrap
  (because `readme_sha is None` always counts as a SHA mismatch).
- `on_bootstrap_complete(success=True)` is now the single place that flips
  `cache_dir_ready=True`.
- Failures leave the cache un-ready and clear `readme_sha` so the next
  cycle retries.

**Files**: `src/coordinare/services/env_cache.py`,
`tests/unit/test_060_env_cache.py`,
`tests/integration/test_env_cache_lifecycle.py` (Phase 12 scenarios reworked
to drive an initial bootstrap before asserting steady-state behavior).

### Fix 2: Hold `_gql_lock` for the full GraphQL request

**Symptom**: Daemon crashed with cascading `Transport is already connected`
errors followed by `Connector is closed.`, which poisoned the GitHub
circuit breaker. Triggered by concurrent github calls introduced when
multi-card mode started polling several boards in parallel.

**Root cause**: `GitHubService._execute` released `_gql_lock` after client
construction, allowing two callers to enter `execute_async` on the same
`AIOHTTPTransport` simultaneously. The transport is single-flight; overlap
corrupts its connector for all subsequent calls.

**Fix**: Widen the lock to wrap the full request (token resolution, client
build, and `execute_async`). Added regression test
`test_execute_serializes_concurrent_callers` that fakes the request layer
and asserts `max_in_flight == 1`.

**Files**: `src/coordinare/services/github.py`,
`tests/unit/services/test_github_service_auth.py`.

### Fix 3: Treat `tokens_processed=None` as not-reported

**Symptom**: `monitor_performer.invalid_tokens_processed value=None` warning
emitted on every poll for performers that hadn't yet executed an LLM call.

**Root cause**: `metrics.get("tokens_processed", 0)` only defaults when the
key is *absent*. Performers send the key with an explicit `null` before any
count exists, so the value flowed through to the "not int" branch and
logged as invalid.

**Fix**: Short-circuit `None` to `tokens_delta=0` with no warning; preserve
existing warnings for genuinely bad types (bool, float, str, negative).

**Files**: `src/coordinare/graph/nodes/monitor_performer.py`,
`tests/unit/graph/nodes/test_monitor_performer.py`.

### Verification

- Full suite: `2411 passed, 5 skipped, 97 deselected`
- Lint: `ruff check` clean
- Commits: `7225b99` (env cache), `595e9ce` (tokens None); IN_REVIEW pickup
  and gql lock fixes earlier in the same branch.

## Phase 14 — Option A: performer-driven env_bootstrap dispatch

QA observation: even after the readiness fix in Phase 13, `~/.coordinare/env-caches/<sanitised>/`
stayed empty. Investigation showed two missing layers:

1. `HTTPPerformerService._build_job_payload` rejected the BootstrapJobPayload
   because the performer HTTP protocol's `JobInitPayload` requires
   `repo_url`/`branch`/`role`/`backend`/`persona` — none of which the
   bootstrap payload carried. So `dispatch_card` returned an error and the
   poll loop tore the job down before the performer ever saw it.
2. The performer had no role handler for `env_bootstrap`. Even if dispatch
   had succeeded, the default `handle_status` path would push a branch and
   try to open a PR, which is wrong for a cache-warmup job.

### Implementation (Option A — performer-driven)

**Coordinare side** (`src/coordinare/services/http_performer_service.py`):
`_build_job_payload` now detects `card_context["job_type"] == "env_bootstrap"`
and routes to a new `_build_env_bootstrap_payload` helper that synthesizes
a `JobInitPayload` from BootstrapJobPayload fields:

- `repo_url` = `https://github.com/{symphony_org}/{symphony_repo}.git`
- `branch` = `"main"` (the bootstrap clone is read-only — agent only needs
  to read README and other env_spec_files)
- `role` = `"env_bootstrap"`
- `backend` = configured backend (defaults to `claude_code`)
- `persona` = synthesized prompt instructing the agent to install
  dependencies into `cache_mount_path` based on the inlined
  `env_spec_contents`
- `secrets` = `GITHUB_TOKEN` plus `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`
  pulled from the coordinare's environment

**Performer side** (`agent/performer/src/performer/main.py`): added an
`env_bootstrap` role branch in `handle_status` that returns a new terminal
status `env_bootstrap_complete` instead of falling through to the
push/PR-open default path. The new status was added to both
`coordinare.protocol.StatusType` and `performer.protocol.PerformerStatusType`
so it round-trips cleanly. The early-return guard for terminal states in
`handle_status` was extended to recognize `env_bootstrap_complete`.

**Daemon polling** (`src/coordinare/daemon.py`): `_poll_bootstrap_completion`
now treats `env_bootstrap_complete` as success (in addition to the legacy
`"ok"` marker, retained for back-compat).

### Files

- `src/coordinare/services/http_performer_service.py` — `_build_env_bootstrap_payload`
- `src/coordinare/protocol.py` — add `env_bootstrap_complete` to `StatusType`
- `src/coordinare/daemon.py` — accept `env_bootstrap_complete` as success
- `agent/performer/src/performer/protocol.py` — mirror new status
- `agent/performer/src/performer/main.py` — env_bootstrap role branch + early-return
- `tests/unit/services/test_http_performer_service.py` — `test_build_env_bootstrap_payload_*`
- `tests/contract/test_agent_protocol.py` — extend expected enum

### Verification

- Full coordinare suite: `2413 passed, 5 skipped, 97 deselected`
- Performer unit/contract suite: `571 passed` (Docker integration tests
  failures are pre-existing environment issues, unrelated to these changes)
- Lint: `ruff check` clean

## Phase 14 — Option B: deterministic activation via `activate.sh`

Option A made the bootstrap job work, but consumer performers still had no
way to *use* the cache: the volume was mounted at `env_cache_path`, yet
neither `PATH` nor any toolchain env var pointed into it. Backends launched
with stock `os.environ`, so a performer that needed `python` from the cached
virtualenv (or `node_modules/.bin`) would silently fall back to the system
copy or fail. The `env_cache_path` field set in `card_context` by
`dispatch_performer.py` was being computed but never read.

Option B adds a deterministic activation contract: the bootstrap agent
writes a sourceable `activate.sh` at the cache root, and every consumer
performer sources it during workspace setup, captures the resulting env
delta, and merges it into each backend subprocess's environment.

### Convention

Bootstrap persona now mandates writing `<cache_mount_path>/activate.sh`
that exports `PATH` (prepending any new bin dirs) and any other vars
needed to use the installed tooling (`VIRTUAL_ENV`, `NODE_PATH`, etc.).
The script must be idempotent and safe to source repeatedly.

### Coordinare changes

- `services/http_performer_service.py::_build_env_bootstrap_payload` —
  persona text mandates the `activate.sh` contract.
- `services/http_performer_service.py::_build_job_payload` — forwards
  `card_context["env_cache_path"]` to `JobInitPayload.env_cache_path`.
- `models/performer_endpoint.py::JobInitPayload` — new optional
  `env_cache_path: str | None = None` field (additive, back-compat-safe).

### Performer changes

- `server/models.py::JobInitPayload` — mirror the new field.
- `models.py::Stand` — new `cache_env: dict[str, str]` field.
- `models.py::Score` — new `env_cache_path: str` field.
- `main.py::_perform_job` — propagates `payload.env_cache_path` into
  the Score dict.
- `workspace.py::clone_repository` — calls new `_activate_env_cache`
  helper after creating the Stand and assigns the result to
  `stand.cache_env`.
- `workspace.py::_activate_env_cache` — sources `activate.sh` in a
  bash subshell with `env -0`, runs a reference bash subshell for a
  baseline, and returns only the keys whose values changed (filtering
  bash defaults like `PWD`/`SHLVL`). Returns `{}` and logs
  `env_cache.activate_missing` if the script is absent.
- `backends/claude_code.py`, `backends/codex.py`, `backends/opencode.py`
  — subprocess env now layers `os.environ` < `cache_env` < `git_env`
  so git auth still wins but cache vars are visible to the agent. Junie
  and Cursor backends inherit the opencode change.

### Files

- `src/coordinare/services/http_performer_service.py`
- `src/coordinare/models/performer_endpoint.py`
- `agent/performer/src/performer/server/models.py`
- `agent/performer/src/performer/models.py`
- `agent/performer/src/performer/main.py`
- `agent/performer/src/performer/workspace.py`
- `agent/performer/src/performer/backends/{claude_code,codex,opencode}.py`
- `tests/unit/services/test_http_performer_service.py` — bootstrap
  persona assertion + `test_build_job_payload_forwards_env_cache_path`
  + `test_build_job_payload_omits_empty_env_cache_path`
- `agent/performer/tests/unit/test_workspace.py` — `TestActivateEnvCache`
  with empty-path, missing-activate, sources-and-deltas, and
  default-vars-excluded cases.

### Verification

- Full coordinare suite: `2301 passed, 5 skipped, 97 deselected`
- Performer unit/contract suite: `573 passed`
- Lint: `ruff check` clean

## Phase 15 — Post-Phase-14 stabilization & coverage

After Phase 14 shipped, live-running the daemon against a real symphony
surfaced two follow-ups: (1) `env_bootstrap` dispatches were terminating
with `terminal_state=failed` even though the cache was populated, and
(2) the new `cache_env` precedence in performer backends needed regression
coverage. Spec 061 work also exposed a coverage gap in
`daemon._poll_bootstrap_completion` that pushed the suite below the 90%
gate.

### Bootstrap dispatch cleanly populates cache (`b8d618c`)

Two compounding causes for the false-failure:

- The synthetic env_bootstrap branch had no commits relative to `main`,
  so the performer image's push + PR-creation step ran for a role that
  must never push. It's now skipped for `env_bootstrap` jobs and the
  branch is named `env-bootstrap-<uuid>` to dodge git's "refusing to
  fetch into current branch" error.
- The performer's `Score(**msg.payload)` validation rejected bootstrap
  payloads because Score requires title/description. The coordinare now
  seeds Score-required fields in bootstrap metadata so validation
  passes inside the performer.

### Backend `cache_env` precedence regression tests (`77cd4a7`)

For each backend (`claude_code`, `codex`, `opencode`) two new tests
assert:

- Stand.cache_env keys (`PATH`, `VIRTUAL_ENV`, `NODE_PATH`) appear in
  the env passed to `create_subprocess_exec`.
- `git_env` wins on conflict (e.g. `GITHUB_TOKEN`), preserving the
  `os.environ < cache_env < git_env` precedence required for git auth.

### `_poll_bootstrap_completion` coverage (`06ed156`)

8 focused tests under `TestPollBootstrapCompletion` in
`tests/unit/test_060_env_cache.py` covering: success path, failure path
with `get_agent_logs` fallback, `check_status` exception, the
`container_id` snapshot branch, persistent-performer warning loop, and
no-on_bootstrap_complete-on-pending. Coverage restored to 90.41% (above
the `--cov-fail-under=90` gate).

### Files

- `src/coordinare/__main__.py`
- `src/coordinare/daemon.py`
- `src/coordinare/dashboard.py`
- `src/coordinare/services/env_cache.py`
- `src/coordinare/services/http_performer_service.py`
- `src/coordinare/services/performer_lifecycle.py`
- `tests/unit/test_060_env_cache.py` — `TestPollBootstrapCompletion`
- `tests/unit/services/test_http_performer_service.py`
- `tests/unit/test_dashboard.py`
- `tests/e2e/test_dashboard_browser.py`
- `agent/performer/tests/unit/backends/test_claude_code.py`
- `agent/performer/tests/unit/backends/test_codex.py`
- `agent/performer/tests/unit/backends/test_opencode.py`

### Verification

- Live run: bootstrap on port 55314 succeeded (~2:20 install); cache
  flipped `cache_dir_ready=True`; subsequent security/qa stages mounted
  the cache `:ro` and finished in ~30s — confirming cache reuse end to
  end. Container `gallant_goldwasser` resolved `ruby --version` →
  `ruby 3.4.2` from the cache mount.
- Full coordinare suite: `2434 passed, 5 skipped, 98 deselected`
- Coverage: 90.41% (gate: 90%)
- Lint: `ruff check` clean

## Phase 16 — Known Follow-up Bugs (Open)

Two bugs surfaced during live runs on `061-passive-phase-board-pickup`
that are not yet fixed. Documented here so they don't get lost; each
will land as its own commit on this branch (or a successor branch) with
the prefix `fix(061)` / `fix(055)` as appropriate.

### Bug 16.1 — IN_REVIEW cards re-dispatched before human submits a review

**Symptom**: A card sitting in the `IN_REVIEW` column gets re-dispatched
to a performer before the human has actually posted a review. Expected
behavior: the orchestrator should keep the card in `monitoring_pr` and
poll the PR until a human review (`COMMENTED` / `CHANGES_REQUESTED` /
`APPROVED`) appears, then route to `relay_feedback` or `merge_pr`.

**Root cause** (introduced by `81992f2` "fix(061): re-adopt orphaned
IN_REVIEW cards in multi-card mode"):
`src/coordinare/graph/nodes/check_board.py` lines 241–293. The phase
guard at line 246 only short-circuits when `phase in ("dispatching",
"blocked")`; it does **not** preserve `"monitoring_pr"`. In multi-card
mode (`max_cards > 1`), if the IN_REVIEW card is already in
`active_sessions`, the re-adoption block (lines 263–289) is skipped and
execution falls through past line 290 with no early return. Every
column check below fails (the card is IN_REVIEW, not IN_PROGRESS /
BLOCKED / TODO), so control reaches the bottom of the function and
`state["phase"]` is overwritten to `"idle"` (around line 782). On the
next tick, routing dispatches from idle, picking up the same card and
re-running a performer.

**Fix approach**:

- Extend the phase guard at line 246 to include `"monitoring_pr"`, OR
- Add an early `return state` after the multi-card re-adoption block
  (after line 289) when the per-session card_id is in `in_review`.

**Files**:

- `src/coordinare/graph/nodes/check_board.py` (fix)
- `tests/unit/graph/test_check_board.py` (regression test: per-session
  invocation with IN_REVIEW card already in `active_sessions` must not
  flip phase to `idle` and must not enter the dispatch path)

### Bug 16.2 — QA persona embeds container-local screenshot paths in PR comments

**Symptom**: The QA visual-testing persona (spec 055) attaches
screenshots in its PR comment, but the URLs reference paths under the
performer container's project `tmp/` directory (e.g.
`/workspace/.../tmp/screenshots/foo.png`). These paths are unreachable
from GitHub and from any browser viewing the PR — the comment renders
broken images.

**Root cause**: The QA backend writes screenshots to a local tmp dir
inside the container and emits a markdown comment that embeds the local
filesystem path verbatim. There is no upload step, no image hosting,
and no URL rewriting before posting the comment.

**Fix approach**: Give the QA persona an upload tool so screenshots
land at a publicly-reachable URL referenced from the comment.
Candidates (in order of preference):

1. **GitHub user-attachments CDN** (preferred) — POST the image to
   GitHub's image upload endpoint used by the web UI
   (`https://github.com/<owner>/<repo>/upload/...` or the asset
   endpoint exposed via the GraphQL/REST attachments API). Returns a
   `https://github.com/user-attachments/assets/<uuid>` URL that renders
   inline in any PR comment.
2. **Commit screenshots to an `assets` branch / orphan branch** in the
   target repo and reference via `https://raw.githubusercontent.com/...`.
3. **External S3-compatible bucket** (last resort; adds infra +
   credentials surface).

Implementation outline:

- New `agent/performer/src/performer/tools/screenshot_upload.py`
  helper exposing `upload_screenshot(local_path: Path) -> str` returning
  the public URL.
- QA persona prompt updated to require calling this tool before
  composing the PR comment, and to use the returned URL in markdown.
- Coordinare-side: ensure the tool's network egress is allowed in the
  performer container (`056-performer-containerization` egress allowlist).
- Regression test: assert posted PR comment body contains
  `https://` URLs (no `/tmp/`, no relative paths) for any image refs.

**Files**:

- `agent/performer/src/performer/tools/screenshot_upload.py` (new)
- `agent/performer/src/performer/personas/qa.py` (or wherever the QA
  persona prompt lives — verify path during implementation)
- `src/coordinare/services/http_performer_service.py` (egress / tool
  registration if applicable)
- `agent/performer/tests/unit/tools/test_screenshot_upload.py` (new)
- `tests/unit/...` (assert no `/tmp/` paths in posted comment bodies)

---

## Phase 17 — Agent-Callable Screenshot Upload Tool (Bug 16.2 follow-up)

Phase 16 delivered Bug 16.2 as a post-process resolver: the wrapper
scans QA `visual_evidence` for local paths and uploads them to GitHub's
user-attachments CDN before composing the PR comment. That fixes the
broken-link symptom, but the agent itself never sees a URL during its
run, so it can't reference the image in inline narrative, suggest
follow-up screenshots based on the upload result, or include the URL
in any structured output other than `visual_evidence`.

Phase 17 adds an **agent-callable** layer on top of that fallback:

- **CLI shim** `performer-upload-screenshot <path>` is installed onto
  `$PATH` inside every performer Docker image via a
  `[project.scripts]` entry in `agent/performer/pyproject.toml`.
  Source: `agent/performer/src/performer/cli.py` →
  `upload_screenshot_cli`. The shim calls the same
  `cdn_upload.upload_screenshot` helper added in Phase 16 and prints
  the returned `https://github.com/user-attachments/assets/...` URL to
  stdout (exit 0). Missing context → exit 1; upload failure → exit 2.
- **Context delivery** — `Score.tool_env` (new property in
  `agent/performer/src/performer/models.py`) returns
  `{PERFORMER_GH_TOKEN, PERFORMER_GH_OWNER, PERFORMER_GH_REPO,
  PERFORMER_GH_ISSUE}` derived from the dispatch payload (PR number
  parsed from `pr_url` takes precedence over `issue_number`; empty
  fields are omitted so the CLI can detect missing context). Every
  backend (`claude_code.py`, `opencode.py`, `codex.py`) merges this
  into the subprocess `env=` dict alongside `cache_env` / `git_env`.
- **Prompt nudge** — each backend's QA-role prompt now mentions the
  tool by name so the agent reaches for it instead of leaving local
  paths in `visual_evidence`.
- **Fallback preserved** — the Phase 16 `resolve_visual_evidence_urls`
  call in `main.py` still runs before `_build_qa_pr_comment`. If the
  agent forgets to call the CLI, or the CLI fails inside the run, the
  wrapper retries the upload and rewrites the path. Two-layer defence.

**Why CLI-on-PATH rather than MCP** — all five backends (claude_code,
codex, opencode, junie, cursor) support shell execution natively; a
plain CLI installed by `pip install .` works for all of them without
per-backend MCP server wiring. The shim is a thin wrapper around the
same helper used by the post-process path, so we don't double the test
surface.

**Files**:

- `agent/performer/src/performer/cli.py` (new)
- `agent/performer/pyproject.toml` (entry-point)
- `agent/performer/src/performer/models.py` (`Score.tool_env`)
- `agent/performer/src/performer/backends/claude_code.py` (env wiring + QA prompt)
- `agent/performer/src/performer/backends/opencode.py` (env wiring + QA prompt)
- `agent/performer/src/performer/backends/codex.py` (env wiring + QA prompt)
- `agent/performer/tests/unit/test_cli.py` (new)
- `agent/performer/tests/unit/test_models.py` (`tool_env` cases)
- `agent/performer/tests/unit/backends/test_claude_code.py` (env injection assertion)

---

## Phase 18 — `qa-assets` Branch Push (Bug 16.2 root-cause fix)

### Why

Phase 16 and 17 both routed screenshot uploads through GitHub's `POST /repos/{org}/{repo}/issues/{n}/asset-upload-url`. Manual `curl` against two production repos confirmed this endpoint returns `404 Not Found` — it is the request the *web UI* uses with session-cookie auth, and is not exposed to Bearer-token clients. Phase 16 was failing silently because the post-process layer caught the 404 and returned `None`. Phase 17 would have surfaced the failure to the agent, but the agent has no way to make the underlying call succeed either — the API doesn't exist.

### Approach

GitHub does host content under `https://github.com/<owner>/<repo>/raw/<ref>/<path>`. So we treat a dedicated orphan branch (`qa-assets`) as the asset store and commit screenshots to it. Each upload lives at `content/<issue_number>/<UTC timestamp>-<sanitized name>` so concurrent uploads from parallel performers never collide on filename — only on the push ref, which we resolve with retry-by-reclone (each retry produces a new commit with a new timestamped file, never the same content).

Flow inside `upload_screenshot`:

1. Build asset path `content/<issue>/<UTC ts>-<sanitized name>`.
2. Authenticate with `https://x-access-token:<TOKEN>@github.com/<o>/<r>.git`.
3. `git clone --depth=1 --single-branch --branch qa-assets <url>`.
4. If clone fails, bootstrap orphan locally: `git init -b qa-assets` + `git remote add origin <url>` (branch doesn't exist yet on remote).
5. Set local `user.email`/`user.name` to the bot identity.
6. Copy file into the work tree at the asset path, `git add`, `git commit`.
7. `git push origin HEAD:refs/heads/qa-assets`. On non-FF (concurrent pusher beat us), throw away the workdir and retry from step 3.
8. Return `https://github.com/<o>/<r>/raw/qa-assets/<asset_path>`.

### Concurrency

Two performers uploading to the same issue at the same time will race on push. Because each commit adds a uniquely-named file, the loser's retry just becomes the next commit — no merge, no overwrite. The retry budget (`max_retries=3` by default) bounds the wait.

### Auth & secret hygiene

Token never appears in committed history (only in the in-memory remote URL of an ephemeral tempdir we delete in `finally`). `_redact()` scrubs the token from any logged error string before it leaves the module.

### Visibility & access

Screenshots inherit the visibility of the host repo. For a **private** repo (the expected target), `https://github.com/<o>/<r>/raw/qa-assets/<path>` is served by the GitHub web UI to authenticated viewers with read access — it renders inline in PR/issue comments for collaborators in a browser session. Anonymous fetches (and bearer-token `curl` against the `github.com/.../raw/` host) return `404`; `raw.githubusercontent.com/<o>/<r>/qa-assets/<path>` with a bearer-auth header returns `200` for programmatic verification. This is the desired property: QA assets stay private to the same audience that can read the source repo, with no separate ACL to manage.

For a **public** repo, the same URL is fetched by GitHub's image proxy (camo) and renders for any viewer.

### Branch lifecycle

The branch is created on first upload via the orphan-bootstrap path (clone fails → `git init -b qa-assets` + push). If the branch is later deleted on the remote, the next upload recreates it the same way — with a single commit containing only that new screenshot. The previous history is unreachable and will be garbage-collected; any previously-posted comment URLs pointing into the deleted history will `404`. This is accepted as a deliberate "delete-to-purge" operator affordance.

### Tests

A `_FakeGit` callable in both test files records every git argv, materializes the work tree on clone, and lets each test parametrize the clone return code and a list of push return codes. Coverage: happy path; orphan bootstrap when clone fails; missing-file / missing-context early-outs; push retry-then-success; push exhaustion → `None`; runner-raises → retried then `None`. The `resolve_visual_evidence_urls` tests are unchanged (they inject an `uploader` directly and don't see the underlying mechanism).

### Files

- `agent/performer/src/performer/cdn_upload.py` (full rewrite of `upload_screenshot`; `is_local_path`, `is_image_path`, `resolve_visual_evidence_urls` preserved)
- `src/coordinare/services/cdn_upload.py` (mirror; `render_screenshot_section` preserved)
- `agent/performer/tests/unit/test_cdn_upload.py` (rewrite to `_FakeGit`)
- `tests/unit/services/test_cdn_upload.py` (rewrite to `_FakeGit`)
- `src/coordinare/graph/nodes/qa_screenshots.py` — no change (public signature preserved)
- `agent/performer/src/performer/main.py` — no change (calls `resolve_visual_evidence_urls`, which still owns the same contract)

### Validation (smoke test, 2026-05-10)

Coordinare-side helper was exercised end-to-end against `ViviDynamics/coordinare` (private), `issue_number=999`, using `tmp/img.png` (1.97 MB). Result:

- `cdn_upload.ok` logged on attempt 1, URL `https://github.com/ViviDynamics/coordinare/raw/qa-assets/content/999/<ts>-img.png` returned.
- `gh api repos/ViviDynamics/coordinare/contents/content/999?ref=qa-assets` confirmed the blob landed at the expected path (1965897 bytes, sha `101f2bd5…`).
- Returned URL renders inline for a logged-in collaborator in a browser; anonymous fetch returns 404 as expected for a private repo.

This confirms the auth flow, path layout, and orphan-bootstrap (since `qa-assets` did not previously exist on the coordinare repo).

---

## Phase 19 — Dashboard Phase Header & Legacy Performer Card Cleanup

### Symptom

During live multi-card runs on `061-passive-phase-board-pickup`, the
dashboard at `:9091/` showed:

- A grey (idle) dot while a performer was actively running.
- A `reviewing` phase badge while all visible cards were blocked
  (no docker containers running).
- Stage/role labels that did not match the card currently shown in
  the heading.

### Root cause

In multi-card mode, each `CardSession` syncs its fields back to the
flat `CoordinareState` via `session_to_state` (`src/coordinare/session.py`).
Top-level fields — `phase`, `performer_stage`, `agent_dispatch_at`,
`agent_session_id`, `performer_metrics`, `open_questions` — are
last-write-wins: whichever session syncs last overwrites them. The
legacy single-session "Performer" card (`<div id="performers-card">`)
plus the page-level phase heading both read from those flat fields,
so they reflected whichever session synced last rather than the
session actually doing work.

The separate `0/1` role-utilization observation (seen alongside this
symptom) is *not* the same bug: `max_concurrent_cards` is a global
card semaphore, while `performers.<role>.max_concurrency` (defaults
to `1`, clamped to `len(services)` at `slot_manager.py:93`) controls
per-role slots. To get `0/2` per role you must both raise the per-role
value *and* provision ≥2 service transports for that role. No change
needed in this phase — captured here so it doesn't read as a
regression.

### Fix

1. **Derive the phase header from `active_sessions`** (`ef91c05`).
   - Add `findActivePerformerSession(s)` to `dashboard.py` that
     returns the first session with phase `monitoring_performer`,
     `monitoring_agent`, or `relay_feedback`.
   - In `renderState`, use that session's `phase` and
     `agent_dispatch_at` for the heading badge, description, and
     "Agent running for: …" age line; fall back to flat state only
     when no active session exists.

2. **Delete the legacy single-session Performer card** (`a99ef69`).
   - Remove the `<div id="performers-card">` HTML block and the
     dependent JS: `updatePerformers`, `updatePerformerLogs`,
     `showPerfList`, `showPerfDetail`, `jumpToLatest`,
     `jumpToLatestLogs`, scroll listeners, and module globals
     (`_perfDetailOpen`, `_perfSessionId`, `_perfEventCount`,
     `_perfAutoScroll`, `_perfLogsCount`, `_perfLogsAutoScroll`).
   - Remove both call sites: `updatePerformers(s)` in `renderState`
     and the entry in the 10s `setInterval` tick.
   - Re-anchor the dynamically-inserted `rebase-status-card` to
     `questions-card` (its previous anchor no longer exists).
   - Rewrite the uptime tick to derive the active session via
     `findActivePerformerSession` and only refresh `#session-age`.
   - Preserve `derivePerformerTokenTotal` / `parseTokenCount`
     (still consumed by `renderState`'s token fallback and by
     `renderPerformersPage`).

`renderActivePerformers`, which iterates `active_sessions`, is now
the sole performer view. No remaining UI path reads `phase`,
`performer_stage`, `agent_session_id`, or `performer_metrics` from
flat state for active-performer status.

### Files

- `src/coordinare/dashboard.py` — helper added, header refactored,
  legacy card + helpers deleted, re-anchoring, interval tick
  rewritten.

### Tests

- `tests/unit/test_dashboard.py` — 130 passed.
- `.venv/bin/ruff check src/coordinare/dashboard.py` clean.
- No new unit tests added: the existing dashboard suite already
  exercises `renderState` and `renderActivePerformers`, and the
  deleted legacy card had no direct test coverage.

### Net effect

- One file changed (`dashboard.py`): ef91c05 +14/-3,
  a99ef69 +9/-266.
- Dashboard heading and active-performer view agree, both sourced
  from `active_sessions`. Multi-card runs no longer show a grey
  dot while work is in progress, and the heading no longer reflects
  a stale session.
