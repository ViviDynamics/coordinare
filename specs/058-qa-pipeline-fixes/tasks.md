# Tasks: QA Pipeline Fixes (058)

## Fix 1 — monitoring_pr concurrency exclusion

- [X] Audit `check_board.py` — find all slot-count comparisons against `max_concurrent_cards`
- [X] Audit `daemon.py` — find capacity guard in multi-session orchestration loop
- [X] Define `PASSIVE_PHASES = {"monitoring_pr"}` constant (shared or per-file)
- [X] Update slot calculation in `check_board.py` to exclude passive-phase sessions
- [X] Update capacity guard in `daemon.py` to exclude passive-phase sessions
- [X] Verify phase transition back from `monitoring_pr` re-counts immediately on next cycle

## Fix 2 — Backend inheritance through effective_config

- [X] Reproduce the bug in a unit test: `default.backend: codex`, no explicit backend on any role, symphony effective_config path → assert dispatched backend is `codex`
- [X] Identify the exact call site in `effective_config()` that loses `model_fields_set`
- [X] Implement fix (reconstruct `model_fields_set` post-instantiation, or compare-to-default approach in `resolved_role()`)
- [X] Confirm fix: role with explicit `backend` still overrides correctly
- [X] Confirm fix: legacy (non-symphony) config path is unaffected

## Fix 3 — Symphony edit UI (dashboard)

- [X] `GET /api/symphonies/{name}`: return `enabled`, `overrides`, `personas` fields
- [X] `POST /api/symphonies`: create endpoint — validate, persist, hot-reload
- [X] `PUT /api/symphonies/{name}`: accept and persist `enabled` field
- [X] Frontend `loadSymphonyDetail`: fetch + pre-populate edit form
- [X] Frontend `renderSymphoniesPage`: list view + Add Symphony inline form
- [X] Frontend `submitAddSymphony`: POST new symphony, redirect to detail

## Tests

- [X] `tests/unit/test_058_concurrency.py` — monitoring_pr sessions excluded from slot count
- [X] `tests/unit/test_058_backend_inheritance.py` — backend inherited correctly through effective_config
- [X] Run full test suite: `.venv/bin/pytest`
- [X] Lint: `.venv/bin/ruff check src/coordinare/`

## Spec updates

- [X] Add User Story 3 (symphony edit UI) to spec.md
- [X] Add FR-008 to FR-011 to spec.md
- [X] Add SC-007 to SC-009 to spec.md
- [X] Create plan.md
- [X] Create tasks.md

## Task 5 — Single-image performer with runtime backend selection

- [X] Create `agent/performer/entrypoint.sh`: case-switch on `$BACKEND`, non-fatal CLI install, `exec python -m performer --serve --port 8088`
- [X] Update `agent/performer/Dockerfile.full`: drop stub binary RUN loop; copy `entrypoint.sh`; set as `ENTRYPOINT`
- [X] Add `env: dict[str, str]` field to `PerformerEndpointConfig` in `performer_endpoint.py`; add `env` to subprocess-mode forbidden-fields validator
- [X] Wire `config.env` into `docker run -e KEY=VAL` args in `start_ephemeral()` in `performer_lifecycle.py`
- [X] Update `config.yaml` `codex-ephemeral` endpoint with `env: {BACKEND: codex}`

## Task 6 — Dashboard bug fixes

- [X] Fix symphony detail page not rendering when navigating from list view: scope SSE partial-update selector to `table[data-detail]` so the list table doesn't short-circuit `loadSymphonyDetail`
- [X] Add `data-detail` attribute to status table rendered by `loadSymphonyDetail` so the SSE optimization can safely identify it
- [X] Fix disconnect banner overlaying navbar: remove `position:fixed` so banner flows above navbar in document order instead of overlapping it

## Task 7 — Docker container leak fix and dashboard container visibility

- [X] Add `cleanup_orphaned_containers()` to `performer_lifecycle.py`: `docker ps --filter label=coordinare.performer.id`, stop each found container, return count
- [X] Export `cleanup_orphaned_containers` in `performer_lifecycle.__all__`
- [X] Call `cleanup_orphaned_containers()` in `__main__._bootstrap_services()` before `_build_http_performer_services()` when any non-subprocess endpoints exist
- [X] Add `container_id` to `_dispatch_card_locked()` success return dict in `http_performer_service.py`
- [X] Propagate `container_id` into `active_session_summaries` in `dashboard.py` (multi-session path)
- [X] Propagate `container_id` into `active_session_summaries` in `dashboard.py` (single-session fallback path)
- [X] Show truncated container ID (12 chars) next to card title in Performers page JS detail pane

## Task 8 — Codex auth.json mode fix

- [X] Change `"auth_mode": "api_key"` → `"auth_mode": "apikey"` in `agent/performer/src/performer/backends/codex.py`
- [X] Rebuild performer Docker images (`base` + `full`) with the fix
- [X] Verify: codex jobs no longer return 401 Unauthorized (confirmed — error changed to Quota exceeded, proving authentication succeeded)

## Task 9 — Performer stream data for Docker containers

- [X] Add `events: list[dict]` and `metrics: dict | None` to `JobStatus` in performer-side `server/models.py`
- [X] Add `events` and `metrics` fields to coordinare-side `models/performer_endpoint.py` `JobStatus`
- [X] Add `_live_events`, `_live_metrics`, `push_progress()`, and `_progress_cb_var` contextvar to `job_runner.py`
- [X] Update `JobRunner._run()` to set `_progress_cb_var` before calling executor
- [X] Update `JobRunner.get()` to include live events/metrics in returned `JobStatus`
- [X] Update `_perform_job()` in `main.py` to call `push_progress()` after each `handle_status()` poll
- [X] Update `check_status()` in `http_performer_service.py` to include `events`/`metrics` for non-terminal jobs
- [X] Add `get_agent_logs() -> list[str]` (sync) to `HTTPPerformerService` backed by `_log_buffer`
- [X] Add `_poll_container_logs()` background task; start it in `dispatch_card` after job registration
- [X] Fix `test_dashboard_quickstart.py` pre-existing failure: `card-section` → `active-work-card`
- [X] Rebuild performer Docker images with the fix

## Task 10 — Dashboard panel layout and classification fixes

- [X] Change `active-work-card` and `awaiting-review-card` from `class="card full"` to `class="card"` (one column each)
- [X] Fix symphony-fallback card misclassification: remove `pr_url`-based `monitoring_pr` inference; always route `IN_PROGRESS` fallback cards to Active Work

## Task 11 — Performer dispatch loop (4 root causes)

- [X] Wire `_perform_job` as `executor` in `agent/performer/src/performer/main.py` — fixes "no executor wired" silent failure
- [X] Fix `check_status()` protocol shape: translate `JobStatus` → `PerformerResponse`-style dict with `status` key
- [X] Fix shared instance state: introduce `_EphemeralJob` dataclass + `_active_jobs: dict[str, _EphemeralJob]`
- [X] Pass `OPENAI_API_KEY` in `_build_job_payload` secrets so codex backend can resolve it inside the container

## Task 12 — Infrastructure fixes (SSE, GQL transport, SubprocessTransport)

- [X] Fix SSE EventSource relative URL: use `window.location.origin + '/events'` to avoid `/admin/events` resolution
- [X] Fix concurrent GQL transport collision: wrap `AIOHTTPTransport` + `Client` access with `asyncio.Lock`
- [X] Fix SubprocessTransport built with empty executable when `performer_endpoints` configured

## Task 13 — Global Config page

- [X] Implement `/admin` Global Config page: structured read-only view of live `ProjectConfiguration`

## Task 14 — Assessor and error-detail fixes

- [X] Fix assessor non-JSON fallback: treat non-JSON LLM response as error → block card with comment
- [X] Improve error detail in blocked card GitHub comments: include raw error message and phase name
- [X] Fix `check_health()` stale `_active_jobs`: clear terminal job entries so `idle` is returned correctly

## Task 15 — Backend parameter wiring and CodexBackend fixes

- [X] Fix `CodexBackend.start()`: wire `effort`, `temperature`, `max_tokens` into CLI invocation
- [X] Add backend parameter wiring tests for all backends (codex, opencode, junie, claude)
- [X] Move backend wiring/conformance tests from coordinare unit suite to performer package

## Task 16 — API key injection for all backends

- [X] Forward `ANTHROPIC_API_KEY` to ephemeral performer containers in `_build_job_payload`
- [X] Inject `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` into subprocess env for opencode, junie backends
- [X] Document Junie volume-mount auth in `config.example.yaml`

## Task 17 — CalVer versioning and GHCR CI pipeline

- [X] Add `version` file (CalVer `YYYY.MM.DD[.N]`) and `registry` file (GHCR prefix)
- [X] Add `bin/validate-version`: format check, valid date, not already tagged, not older than latest tag
- [X] Add `bin/update-version`: day-roll and build-increment helpers
- [X] Update `pr-ci.yml`: push snapshot `-base` and `-full` images to GHCR after SC-007 check
- [X] Add `main-branch-build.yml`: validate-version → lint/test → GitHub release + tag → build+push versioned images → retag as `latest`
- [X] Gracefully handle push rejection in `sync-version-to-prs` step
- [X] Populate GitHub release notes body from merge commit message

## Task 18 — Dashboard UX improvements

- [X] Change `questions-card` from `class="card full"` to `class="card"` (one column)
- [X] Render "View issue ↗" link per open question using `s.issue_url`

## CI / coverage housekeeping

- [X] Fix `test_github_retry.py`: correct arg order for `RateLimitedGitHubError(retry_after, message)`
- [X] Fix `test_performer_endpoint.py`: use valid `SecretSourceConfig` field to reach subprocess validator
- [X] Add 9 coverage tests to `test_doc_dedup.py` — cover `_token_overlap`, `_extract_sections`, OSError paths, ambiguous overlap, `_rename_heading`, canonical priority fallthrough
- [X] Fix lint in `test_advocate.py`: sort inline imports (I001), rename unused `github` → `_github` (RUF059)
- [X] Fix lint in `test_ci_detection.py`: drop unused `result =` assignment (F841)
- [X] Confirm combined statement+branch coverage ≥ 90% (`fail_under=90`)
