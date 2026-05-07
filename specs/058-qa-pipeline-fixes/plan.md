# Implementation Plan: QA Pipeline Fixes (058)

**Branch**: `058-qa-pipeline-fixes`
**Spec**: [spec.md](spec.md)

## Overview

Three QA-confirmed fixes discovered during live coordinare testing:

1. **Concurrency counting bug** — `monitoring_pr` sessions (passively awaiting human review) incorrectly count toward `max_concurrent_cards`, preventing coordinare from picking up new TODO cards.
2. **Backend inheritance bug** — `effective_config()` round-trips through `model_dump()` + re-instantiation, destroying `model_fields_set` and causing roles without an explicit `backend` field to silently fall back to the hardcoded default (`opencode`) instead of inheriting from `performers.default`.
3. **Symphony edit UI gap** — The Symphonies dashboard page was read-only; no web forms existed to edit symphony configuration (even though all backend API endpoints were implemented in spec 057).

## Implementation Strategy

All three fixes are independent and can be implemented in any order. No new dependencies required.

---

## Task 1: Fix monitoring_pr concurrency counting (FR-001 to FR-005)

**Files**: `src/coordinare/graph/check_board.py`, `src/coordinare/daemon.py`

**Approach**: Define the set of "passive" phases that do not consume a concurrency slot. Replace raw session-count checks with a filtered count that excludes `monitoring_pr` sessions.

**Code sites**:
- `check_board.py` — slot calculation used when deciding whether to pick up a new card
- `daemon.py` — capacity guard in the multi-session orchestration loop

**Change**: Define `PASSIVE_PHASES = {"monitoring_pr"}`. Anywhere `len(active_sessions)` is compared to `max_concurrent_cards`, filter out sessions whose phase is in `PASSIVE_PHASES`.

---

## Task 2: Fix backend inheritance through effective_config (FR-006, FR-007)

**File**: `src/coordinare/config.py`

**Root cause**: `effective_config()` calls `global_config.model_dump()`, merges overrides, then re-instantiates `ProjectConfiguration(**merged)`. This sets `model_fields_set` to all fields in the merged dict, including fields that were defaults — making `resolved_role()` think every field was explicitly set. When it later calls `role_cfg.model_fields_set` to detect explicit overrides, it always sees every field as explicit, so inheritance from `default` never fires.

**Fix**: After re-instantiation, reconstruct `model_fields_set` to only include fields that were actually set by the symphony's `overrides` dict (plus any fields explicitly set in the global config's own `model_fields_set`). Alternatively, compare the merged dict against defaults before re-instantiating so only genuinely-overridden fields are marked as set.

**Simpler alternative**: In `resolved_role()`, instead of using `model_fields_set` to detect explicit overrides, compare the field value against the field's default value — if equal to default, treat as unset for inheritance purposes.

---

## Task 3: Symphony edit UI — backend API completeness (FR-008 to FR-011)

**File**: `src/coordinare/dashboard.py`

**Completed**:
- `GET /api/symphonies/{name}` — now returns `enabled`, `overrides`, `personas`
- `POST /api/symphonies` — creates new symphonies, persists, hot-reloads
- `PUT /api/symphonies/{name}` — now accepts and persists `enabled`
- Frontend `loadSymphonyDetail`, `renderSymphoniesPage`, `submitAddSymphony` — complete edit forms

---

## Task 4: Tests

**New test files**: `tests/unit/test_058_concurrency.py`, `tests/unit/test_058_backend_inheritance.py`

**Concurrency tests**:
- `monitoring_pr` session does not count toward slot limit
- Non-passive phases (dispatching, merging, blocked) do count
- Mixed passive + active: limit correctly enforced on active count
- Regression: never exceeds `max_concurrent_cards` active sessions

**Backend inheritance tests**:
- Role without explicit `backend` inherits from `default.backend`
- Role with explicit `backend` overrides `default.backend`
- Same behavior in legacy config vs. symphony effective_config path

---

## Task 5: Single-image performer with runtime backend selection

**Problem**: `Dockerfile.full` previously installed 10-byte stub shell scripts for each backend CLI (`codex`, `claude`, `cursor`, `junie`, `opencode`). These satisfied `shutil.which()` checks but would immediately exit when invoked, causing all real backend calls to fail silently.

**Approach**: Replace stub binaries and per-backend Dockerfile variants with a single image + runtime entrypoint that installs the real CLI on container start.

**Files**:
- `agent/performer/entrypoint.sh` (new) — installs the real backend CLI via `npm install -g …@latest` or `curl | sh` based on `$BACKEND`; non-fatal on failure (container still starts without the CLI); ends with `exec python -m performer --serve --port 8088`
- `agent/performer/Dockerfile.full` — drops stub RUN loop; copies `entrypoint.sh` and sets it as `ENTRYPOINT`
- `src/coordinare/models/performer_endpoint.py` — adds `env: dict[str, str]` field to `PerformerEndpointConfig`; forbidden in `subprocess` mode
- `src/coordinare/services/performer_lifecycle.py` — wires `config.env` into `docker run -e KEY=VAL` args in `start_ephemeral()`
- `config.yaml` — `codex-ephemeral` endpoint now carries `env: {BACKEND: codex}`

**Runtime flow**:
1. Coordinare calls `start_ephemeral(config)` → `docker run -e BACKEND=codex … coordinare-performer:full`
2. Container starts; `entrypoint.sh` sees `BACKEND=codex` → runs `npm install -g @openai/codex@latest`
3. If install fails, warning is emitted and performer starts anyway (capability probe will simply not advertise that backend)
4. `exec python -m performer --serve --port 8088` replaces the shell as PID 1

**Why single image**: Five per-backend Dockerfile variants would require five CI build jobs, five GHCR tags, and a complex tag-latest loop — all eliminated by moving CLI installation to runtime.

---

## Task 6: Dashboard bug fixes

**File**: `src/coordinare/dashboard.py`

**Bug 1 — Symphony detail page not rendering on navigation from list view**

**Root cause**: `renderSymphoniesPage()` had an SSE partial-update optimization that checked `el.querySelector('table')`. When navigating from the list view to a detail URL, `el` still contained the list view's `<table>`. The optimization found the list table, tried to update the wrong rows, and returned early without ever calling `loadSymphonyDetail()`.

**Fix**: Scope the selector to `table[data-detail]` and add `data-detail` to the status table rendered by `loadSymphonyDetail`. The optimization now only fires when the detail view is already rendered — navigating from the list always falls through to `loadSymphonyDetail`.

**Bug 2 — Disconnect banner overlays navbar, blocking navigation**

**Root cause**: `#disconnected-banner` used `position: fixed; top: 0; z-index: 999`, which placed it over the navbar (`position: sticky; z-index: 100`) regardless of DOM order.

**Fix**: Removed `position: fixed; top: 0; left: 0; right: 0; z-index: 999` from the banner's CSS. The banner is already rendered before `<nav id="navbar">` in the DOM, so it now flows above the navbar naturally when shown.

---

## Task 7: Docker container leak fix and dashboard container visibility

**Files**: `src/coordinare/services/performer_lifecycle.py`, `src/coordinare/services/http_performer_service.py`, `src/coordinare/__main__.py`, `src/coordinare/dashboard.py`

**Problem 1 — Container leak after coordinare crash**: Ephemeral performer containers are started with `--rm` but that only removes them when the process exits cleanly. When coordinare crashes, `aclose()` is never called, `_active_jobs` is lost, and containers keep running. On the next startup, new containers are launched alongside the orphaned ones.

**Fix**: Added `cleanup_orphaned_containers()` to `performer_lifecycle.py`. It runs `docker ps --filter label=coordinare.performer.id --format {{.ID}}` to find all containers started by any coordinare instance, then stops each one. Called from `__main__._bootstrap_services()` before `_build_http_performer_services()` so orphans are cleared before new performers are registered. The `coordinare.performer.id` label was already applied by `start_ephemeral()`.

**Problem 2 — Dashboard doesn't show container ID**: Operators had no way to correlate an active performer session with its Docker container for debugging.

**Fix**: `_dispatch_card_locked()` in `http_performer_service.py` now includes `container_id` in the success return dict. `dashboard.py` (Python) extracts it from `sess["agent_dispatch"]` into `active_session_summaries` (both multi-session and single-session fallback paths). The Performers page JS shows the first 12 chars of the container ID next to each card title in the detail pane.

---

---

## Task 8: Codex auth.json mode fix

**File**: `agent/performer/src/performer/backends/codex.py`

**Problem**: Codex jobs returned `401 Unauthorized: Missing bearer or basic authentication in header` against `api.openai.com`. The performer writes `~/.codex/auth.json` to force API key mode, but used `"auth_mode": "api_key"` (with underscore). The codex `AuthMode` Rust enum serialises as `"apikey"` (no underscore); the unrecognised string caused codex to fall through to unauthenticated mode.

**Fix**: Changed the `auth_mode` value from `"api_key"` to `"apikey"` — a one-character change. Confirmed end-to-end: the 401 error was replaced by a `Quota exceeded` response, proving codex is now sending authenticated requests to OpenAI.

---

## Task 9: Performer stream data for Docker containers

**Files**: `agent/performer/src/performer/server/models.py`, `agent/performer/src/performer/server/job_runner.py`, `agent/performer/src/performer/main.py`, `src/coordinare/models/performer_endpoint.py`, `src/coordinare/services/http_performer_service.py`

**Problem**: When a performer runs inside a Docker container, the dashboard Performers page showed "No live events yet" and "No stderr logs yet" permanently. Root cause: `JobStatus` had no `events` or `metrics` fields; `_perform_job()` consumed intermediate `handle_status()` results internally without surfacing them via HTTP; `HTTPPerformerService` had no `get_agent_logs()` method.

**Fix (events/metrics)**:
- Added `events: list[dict]` and `metrics: dict | None` to `JobStatus` on both performer and coordinare sides
- Added `_live_events` / `_live_metrics` buffers to `JobRunner`, populated via `push_progress()`
- Added `_progress_cb_var` contextvar set in `_run()` so `_perform_job()` can push events after each `handle_status()` poll without a direct runner reference
- `check_status()` now includes `events`/`metrics` in the non-terminal return dict so `monitor_performer.py` accumulates them into `performer_events` / `performer_metrics` state

**Fix (logs)**:
- Added `get_agent_logs() -> list[str]` to `HTTPPerformerService` (sync, reads from `_log_buffer`)
- A background task (`_poll_container_logs`) polls `docker logs --tail=200 <container_id>` every 5 s during job execution, refreshing `_log_buffer`
- Task is started in `dispatch_card` immediately after the ephemeral job is registered

---

## Task 10: Dashboard panel layout and classification fixes

**File**: `src/coordinare/dashboard.py`

**Fix 1 — Active Work / Awaiting Human Review panel width**: Both panels were `class="card full"` (spanning all columns). Changed to `class="card"` so each occupies one column.

**Fix 2 — Misclassified symphony-fallback cards**: The symphony-state fallback (used between poll cycles when `active_sessions` is empty) was routing cards with a `pr_url` into "Awaiting Human Review". A PR URL only means a PR exists — it does not mean the card is in `monitoring_pr` phase. Cards actively being worked on by performers already have a PR URL on their branch. Fixed by removing the `pr_url`-based branch; the fallback now always routes `IN_PROGRESS` cards to "Active Work". "Awaiting Human Review" only populates from explicit `monitoring_pr` sessions in `active_sessions`.

---

## Task 11: Performer dispatch loop — 4 root causes

**Files**: `agent/performer/src/performer/main.py`, `src/coordinare/services/http_performer_service.py`

**Problem**: Ephemeral Docker performer containers accepted jobs but cards looped indefinitely — every dispatch cycle found the card still `IN_PROGRESS` and re-dispatched.

**Root causes and fixes**:

1. **No executor wired** — `create_app_from_env()` was called without an `executor=` argument, so the default stub returned `success=False, summary="no executor wired"` for every job. Fixed by passing `_perform_job` as `executor` in `main.py`.

2. **Protocol shape mismatch** — `check_status()` returned a raw `JobStatus` dict, but `monitor_performer.py` expected a `PerformerResponse`-shaped dict with a `status` key. Fixed by translating `JobStatus` into the expected dict shape and deserialising the JSON-serialised `PerformerResponse` stored in `JobResult.summary`.

3. **Shared instance state** — `_container_id`, `_endpoint`, and `_client` were instance-level attributes on `HTTPPerformerService`, so concurrent cards overwrote each other's state. Fixed by introducing `_EphemeralJob` dataclass and tracking per-job state in `_active_jobs: dict[str, _EphemeralJob]`.

4. **Missing `OPENAI_API_KEY` in container** — The key wasn't forwarded in `_build_job_payload`, so the codex backend's three-source secret resolver couldn't find it. Fixed by adding `OPENAI_API_KEY` to the secrets dict.

---

## Task 12: Infrastructure fixes — SSE, GQL transport, SubprocessTransport

**Files**: `src/coordinare/dashboard.py`, `src/coordinare/graph/github_transport.py`, `src/coordinare/services/subprocess_performer_service.py`

**Fix 1 — SSE EventSource relative URL** (`dashboard.py`): The SSE client was constructed with a relative URL (`/events`). On sub-routes like `/admin/*` this resolved to `/admin/events`, which doesn't exist. Fixed by using an absolute URL (`window.location.origin + '/events'`) so SSE always connects to the correct endpoint regardless of the current page path.

**Fix 2 — Concurrent GQL transport collision** (`github_transport.py`): The `AIOHTTPTransport` + `Client` pair was shared across concurrent coroutines without locking. Concurrent GitHub GraphQL calls collided on the shared session, causing intermittent transport errors. Fixed by wrapping transport access with `asyncio.Lock`.

**Fix 3 — SubprocessTransport built with empty executable** (`subprocess_performer_service.py`): When `performer_endpoints` were configured, `SubprocessTransport.__init__` was called without resolving the performer binary path, producing an empty string. Fixed by guarding construction with an existence check so subprocess-mode performers are skipped when containerised performers are the primary backend.

---

## Task 13: Global Config page

**File**: `src/coordinare/dashboard.py`

**Problem**: The `/admin` route rendered a placeholder stub ("Admin Config — coming soon").

**Fix**: Implemented a full Global Config page that reads and displays the live `ProjectConfiguration` as a structured, read-only JSON viewer. Sections: symphony list, performer endpoints, notification targets, and raw YAML download link.

---

## Task 14: Assessor and error-detail fixes

**Files**: `src/coordinare/graph/assess_card.py`, `src/coordinare/graph/dispatch_performer.py`, `src/coordinare/services/http_performer_service.py`

**Fix 1 — Assessor non-JSON fallback** (`assess_card.py`): The assessor LLM sometimes returned plain text instead of a JSON decision blob. The fallback path treated any non-empty response as a `PROCEED` signal, causing cards to be dispatched without a valid assessment. Fixed by treating any non-JSON response as an error — the card is blocked with an explanatory comment instead of silently proceeding.

**Fix 2 — Error detail in blocked card comments** (`dispatch_performer.py`): Blocked card GitHub comments showed only a generic "coordinare encountered an error" message with no actionable detail. Fixed by including the raw error message and phase name in the comment body so operators can diagnose without reading logs.

**Fix 3 — `check_health()` stale `_active_jobs`** (`http_performer_service.py`): `check_health()` on an ephemeral performer returned `unknown` even after a job completed, because it checked `_active_jobs` which was populated only during dispatch and never cleared on terminal job state. Fixed by clearing the stale entry on terminal status so subsequent health checks return `idle`.

---

## Task 15: Backend parameter wiring and CodexBackend fixes

**Files**: `agent/performer/src/performer/backends/codex.py`, `agent/performer/tests/`

**Fix — Missing CodexBackend params**: `CodexBackend.start()` accepted `effort`, `temperature`, and `max_tokens` in its signature but never forwarded them to the codex CLI invocation. Fixed by wiring all three into the command-line arguments passed to `codex`.

**Tests — Backend parameter wiring**: Added a suite of wiring tests (`tests/backends/test_*_wiring.py`) covering each backend (codex, opencode, cursor, junie, claude) to assert that all `JobInitPayload` fields are correctly forwarded to the CLI invocation. Tests moved from coordinare unit suite to the performer package where the backend code lives.

---

## Task 16: API key injection for all backends

**Files**: `src/coordinare/services/http_performer_service.py`, `agent/performer/src/performer/backends/*.py`, `config.example.yaml`

**Problem**: Only `OPENAI_API_KEY` was forwarded to performer containers; other backends (`opencode`, `junie`, `cursor`) needed `ANTHROPIC_API_KEY`, and all needed their keys available in the container's secret resolver.

**Fixes**:
- `ANTHROPIC_API_KEY` added to the secrets forwarded in `_build_job_payload` alongside `OPENAI_API_KEY`
- Opencode, Junie, and Cursor backends updated to inject `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` from the resolved secrets dict into the subprocess environment
- `config.example.yaml` updated with volume-mount examples for Cursor and Junie (which require host-side credential files), with inline comments explaining the auth flow

---

## Task 17: CalVer versioning and GHCR CI pipeline

**Files**: `version`, `registry`, `bin/validate-version`, `bin/update-version`, `.github/workflows/pr-ci.yml`, `.github/workflows/main-branch-build.yml`

**Problem**: Performer Docker images had no versioning or automated publishing; there was no way to reference a specific image in production.

**Solution**: CalVer scheme (`YYYY.MM.DD[.N]`) with automated GHCR publishing.

- `version` file — current CalVer string (e.g. `2026.05.05`); single source of truth
- `registry` file — GHCR image prefix (`ghcr.io/vividynamics/coordinare-performer`)
- `bin/validate-version` — checks format, valid date, not already tagged, not older than latest tag; run in CI
- `bin/update-version` — increments the build suffix (`YYYY.MM.DD.N+1`) or rolls to a new day
- `pr-ci.yml` update — after SC-007 size-gap check passes, builds and pushes `-base` and `-full` snapshot images to GHCR tagged with the branch name
- `main-branch-build.yml` (new) — full release pipeline: `validate-version` → lint/test/coverage → create GitHub release + git tag → build+push versioned base and full images → retag both as `latest`
- `sync-version-to-prs` — gracefully handles push rejections (branch protection, concurrent pushes) without failing the workflow

---

## Task 18: Dashboard UX improvements

**File**: `src/coordinare/dashboard.py`

**Fix 1 — Open Questions panel width**: `questions-card` was `class="card full"` (spanning all columns). Changed to `class="card"` so it occupies one column alongside other panels.

**Fix 2 — Open Questions issue link**: Each open question now renders a "View issue ↗" link pointing to `s.issue_url` (the active card's GitHub issue URL). Previously questions were plain text with no path to the source. If no issue URL is available the question renders as plain text as before.

---

## Completion Criteria

All items in `tasks.md` marked complete, all new tests pass, `ruff check` clean.
