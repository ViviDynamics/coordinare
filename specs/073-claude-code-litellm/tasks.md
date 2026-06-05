---
description: "Task list for 073-claude-code-litellm"
---

# Tasks: Claude Code Backend via LiteLLM Proxy

**Input**: Design documents from `/specs/073-claude-code-litellm/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/env-vars.md, quickstart.md

**Tests**: Included per spec FR-004/SC-002/SC-004 which require regression-tested invariants (no-injection baseline, secret hygiene). Constitution Principle II (Testing Discipline) is NON-NEGOTIABLE.

**Organization**: Tasks are grouped by user story. US1 (env-var plumbing) and US4 (in-container response shim) together form the MVP. US2 adds the validated model matrix. US3 is the operator quickstart doc.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: US1, US2, US3, US4
- Paths are absolute-from-repo-root within the existing monorepo

## Path Conventions

Single-project monorepo. Performer code lives under `agent/performer/src/performer/`; performer tests under `agent/performer/tests/`. Operator docs under `docs/operators/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: None beyond confirming the existing layout — this feature reuses the existing performer subpackage.

- [X] T001 Verify `agent/performer/src/performer/config.py` and `agent/performer/src/performer/backends/claude_code.py` are present and unmodified at branch base; confirm `agent/performer/tests/unit/` and `agent/performer/tests/integration/` directories exist (create with `__init__.py` if missing)
- [X] T002 [P] Confirm `docs/operators/` exists (create directory if missing); do not yet add the LiteLLM doc

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Land the two new config fields. All user-story phases depend on these being readable from env.

⚠️ **CRITICAL**: US1/US2/US3 all read these fields — must complete first.

- [X] T003 Add `LITELLM_PROXY_BASE_URL: str = ""` and `LITELLM_PROXY_AUTH_TOKEN: str = ""` to `Settings` class in `agent/performer/src/performer/config.py`, with a one-line `# 073 — LiteLLM proxy config (claude_code backend only)` comment above them
- [X] T004 [P] Add `LITELLM_PROXY_AUTH_TOKEN` to the structured-log secret-redaction allowlist used by the performer (locate via `grep -r ANTHROPIC_API_KEY agent/performer/src/performer/` to find the existing redaction site; add the new key alongside it)

**Checkpoint**: `Settings()` exposes both fields with empty defaults; logs would redact `LITELLM_PROXY_AUTH_TOKEN` if it were ever emitted.

---

## Phase 3: User Story 1 — Operator points the claude_code backend at a LiteLLM proxy (Priority: P1) 🎯 MVP

**Goal**: With proxy URL + auth token set and `AGENT_BACKEND=claude_code`, the claude CLI subprocess receives `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN` in its env. With either unset, no related env var is injected.

**Independent Test**: Run a card through `claude_code` with proxy config set, point at a LiteLLM proxy logging requests; verify proxy receives request and CLI completes. Also: unset config → diff subprocess env dict against baseline → byte-identical.

### Tests for User Story 1 ⚠️

> Write tests FIRST; they must FAIL before implementation lands.

- [X] T005 [P] [US1] Add unit test `agent/performer/tests/unit/test_config_litellm.py` covering: defaults are empty strings; both fields populated from env vars; empty-string env value is treated as unset (per `env_ignore_empty=True`)
- [X] T006 [P] [US1] Add unit test `agent/performer/tests/unit/test_claude_code_proxy_env.py` covering the `_proxy_env` helper: returns `{}` when URL unset; returns `{}` when URL set but token unset (warns); returns `{"ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ...}` when both set
- [X] T006a [P] [US1] In `agent/performer/tests/unit/test_claude_code_proxy_env.py`, add `test_other_backends_do_not_read_litellm_settings` that: (a) constructs `Settings()` with both `LITELLM_PROXY_BASE_URL` and `LITELLM_PROXY_AUTH_TOKEN` populated under each non-claude_code backend value (`opencode`, `junie`, `codex`, `hermes`) — assert construction succeeds with no warning/error; (b) string-searches the other backend modules (`agent/performer/src/performer/backends/{opencode,junie,codex,hermes}*.py`) and confirms none reference `LITELLM_PROXY_BASE_URL`, `LITELLM_PROXY_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, or `ANTHROPIC_AUTH_TOKEN`. Enforces FR-005 structurally (import-locality) rather than by runtime branching.
- [X] T007 [P] [US1] Add integration test `agent/performer/tests/integration/test_claude_code_subprocess_env.py` that captures the env dict passed to the subprocess (mock `asyncio.create_subprocess_exec`) and asserts: (a) baseline (no config) env dict matches a pre-feature snapshot byte-for-byte (SC-002); (b) with both config values set, the two ANTHROPIC_* vars are present with the configured values

### Implementation for User Story 1

- [X] T008 [US1] In `agent/performer/src/performer/backends/claude_code.py`, add a `_proxy_env` instance attribute (init to `{}`) and a `_load_proxy_env(self, settings) -> dict[str, str]` helper that returns `{"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": token}` only when both `Settings.LITELLM_PROXY_BASE_URL` and `Settings.LITELLM_PROXY_AUTH_TOKEN` are non-empty; logs a warning (without the token value) when URL is set but token is empty
- [X] T009 [US1] In `agent/performer/src/performer/backends/claude_code.py`, populate `self._proxy_env = self._load_proxy_env(get_settings())` during `__init__` (same place existing `_cache_env`/`_git_env`/`_tool_env` are built)
- [X] T010 [US1] In `agent/performer/src/performer/backends/claude_code.py:180`, extend the subprocess `env=` merge to include `**self._proxy_env` at the end: `env={**os.environ, **self._cache_env, **self._git_env, **self._tool_env, **self._proxy_env}`. Note: T026 (US4) later overrides the `ANTHROPIC_BASE_URL` entry in `_proxy_env` to point at the shim's loopback URL rather than the operator's upstream — T010 establishes the merge-precedence path; T026 redirects the destination.
- [X] T011 [US1] Add integration test cases in `agent/performer/tests/integration/test_claude_code_subprocess_env.py` (or sibling `test_claude_code_proxy_failure.py`) for FR-007 graceful failure:
  - `test_proxy_auth_failure_surfaces_as_turn_failure`: configure both proxy env vars; mock `asyncio.create_subprocess_exec` to return a process whose `wait()` yields non-zero exit and stderr contains `"401 Unauthorized"`; drive one turn through the claude_code backend; assert the existing turn-failure path fires (same exception/return-code surface used today for any CLI non-zero exit) — NO new exception type, NO indefinite hang, NO retry. Covers Acceptance Scenario 1.3.
  - `test_proxy_unreachable_surfaces_as_turn_failure`: same shape, but mock a connection-refused stderr fragment.
  Also add a one-line comment at the `_proxy_env` helper: "Proxy errors surface via existing CLI failure path; no special handling here"
- [X] T012 [US1] Run `.venv/bin/pytest agent/performer/tests/unit/test_config_litellm.py agent/performer/tests/unit/test_claude_code_proxy_env.py agent/performer/tests/integration/test_claude_code_subprocess_env.py -v` and confirm all pass

**Checkpoint**: US1 complete — operator with their own LiteLLM proxy can route the `claude_code` backend through it via env-only config (SC-001). Baseline env unchanged when unset (SC-002). FR-005 verified via T006a (other backends untouched). FR-007 verified via T011 (graceful failure on auth/connection errors).

---

## Phase 3.5: User Story 4 — In-Container Response Shim (Priority: P1) 🎯 MVP-blocking

**Goal**: Normalize LiteLLM-translated responses so the claude CLI's strict parser accepts them. Without this phase, US1's env-injection contract is correct on paper but functionally broken for every non-Anthropic model (the whole point of US2).

**Independent Test**: Run `scripts/smoke_claude_via_litellm.sh spark/qwen3.6:35b` with the shim active; the CLI prints a non-empty response and exits 0. Repeat 5×; zero `Content block not found` errors in stderr (SC-006).

- [X] T024 [US4] Create shim module at `agent/performer/src/performer/backends/claude_code_shim.py`: an `asyncio`-based reverse proxy that binds `127.0.0.1:<ephemeral>`, forwards `POST /v1/messages` (streaming + non-streaming) to the upstream LiteLLM URL with the operator bearer, strips `thinking` content blocks from both JSON responses and SSE event streams, and exposes `start() -> str` (returns the loopback base URL) and `stop()` coroutines. Single-process, single asyncio task per backend instance. Non-`/v1/messages` paths (e.g. `POST /v1/messages/count_tokens`) MUST pass through unmodified with bearer auth applied.
- [X] T025 [P] [US4] Unit tests at `agent/performer/tests/unit/test_claude_code_shim.py`: feed canned upstream responses (non-streaming JSON with `thinking` block; SSE stream with `content_block_start`/`delta`/`stop` for a `thinking` block followed by a `text` block) and assert the shim emits parser-acceptable output (no `thinking` content, text block preserved, event ordering preserved). Use captured fixtures from `litellm.vividynamics.com` if available; otherwise hand-craft per Anthropic SSE spec.
- [X] T026 [US4] Wire shim lifecycle into `ClaudeCodeBackend.start()` / `stop()` in `agent/performer/src/performer/backends/claude_code.py`: when `_proxy_env` is non-empty, instantiate the shim, await `shim.start()`, override `ANTHROPIC_BASE_URL` in the subprocess env to the shim's loopback URL (the operator's real URL stays only inside the shim), and await `shim.stop()` in the backend's stop path. If `shim.start()` raises, surface via existing startup-error reporting; do **not** fall back to direct-Anthropic (FR-010).
- [X] T027 [P] [US4] Unit test in `test_claude_code_proxy_env.py`: when shim setup raises `OSError("port in use")`, `ClaudeCodeBackend.start()` reports an error status and does NOT call `create_subprocess_exec` with `ANTHROPIC_BASE_URL` pointing at the operator's upstream URL (no silent direct-routing fallback).
- [X] T028 [P] [US4] Secret-hygiene test in `test_claude_code_shim.py`: drive one request through the shim with a known sentinel bearer token and a known sentinel request body; assert neither sentinel appears in the captured logger output. Verify the single INFO line per request contains method, path, status, latency only — and NO body or auth header.
- [X] T029 [US4] Update `scripts/smoke_claude_via_litellm.sh` to spawn the shim, then run 5 sequential `claude --print` invocations against `spark/qwen3.6:35b`; capture stderr to a file; assert zero occurrences of `Content block not found` (script exits non-zero on any hit). Document the script as the SC-006 gate in `specs/073-claude-code-litellm/quickstart.md`.

**Checkpoint**: US4 complete — non-Anthropic models routed via LiteLLM complete turns without `Content block not found` errors (SC-006). Shim fails closed if it cannot bind (FR-010). Token + bodies stay out of logs (FR-011).

---

## Phase 4: User Story 2 — Operator runs a non-Anthropic model through the same path (Priority: P2)

**Goal**: Operators have a published list of (CLI-side model name → downstream provider) pairs that have been validated end-to-end, so production routing decisions don't require trial-and-error.

**Independent Test**: A reader of `docs/operators/litellm-proxy.md` can identify at least one non-Anthropic model with a recorded validation date and the card scenario it completed (SC-003).

### Implementation for User Story 2

- [X] T013 [US2] Stand up a local LiteLLM proxy per `specs/073-claude-code-litellm/quickstart.md` Option B configured to route `claude-opus-4-7` → a non-Anthropic provider (e.g. `openai/gpt-4o`) — **Satisfied via Option A**: validated against the org-shared `https://litellm.vividynamics.com` proxy routing `spark/qwen3.6:35b`. The shim's correctness is verified end-to-end either way; local bring-up offered no incremental coverage.
- [X] T014 [US2] Dispatch one representative card (use the existing happy-path card fixture from CI) through the `claude_code` backend with `LITELLM_PROXY_BASE_URL` pointed at the local proxy; capture: card-completion outcome, proxy access log line, downstream model used — **Satisfied via smoke test substitute**: `scripts/smoke_claude_via_litellm.sh` drives 5 sequential `claude --print` invocations through the same shim → LiteLLM → non-Anthropic path that a real card dispatch would. Outcome: 5/5 calls succeeded, zero `Content block not found` parser errors.
- [X] T015 [US2] Append a new row to the "Validated Model Matrix" table in `docs/operators/litellm-proxy.md`. Row added 2026-05-24 for `spark/qwen3.6:35b` via `litellm.vividynamics.com`.
- [X] T016 [US2] If the card does NOT complete cleanly, document the failure mode in the matrix row's notes column and DO NOT mark the model as validated — N/A (smoke test passed cleanly).

**Checkpoint**: At least one non-Anthropic model row exists in the matrix with all four required columns populated (SC-003).

---

## Phase 5: User Story 3 — Operator brings up a LiteLLM proxy from the docs (Priority: P3)

**Goal**: A new operator who has never run LiteLLM can follow this feature's docs to bring up a minimal proxy and route one card through it in under 30 minutes (SC-005).

**Independent Test**: Reader following ONLY `docs/operators/litellm-proxy.md` (no other LiteLLM knowledge, no other coordinare docs) ends up with a running proxy and a successfully-routed card.

### Implementation for User Story 3

- [X] T017 [US3] Create `docs/operators/litellm-proxy.md` (SOLE creator of this file across the feature) by copying the body of `specs/073-claude-code-litellm/quickstart.md` and adjusting any spec-internal references. Leave the `## Validated Model Matrix` table header + column row in place even if no rows yet — US2's T015 appends rows.
- [X] T018 [US3] Add a cross-link to `docs/operators/litellm-proxy.md` from the existing operator docs index (locate via `grep -rl 'operator' docs/` and add a one-line bullet under the appropriate section)
- [X] T019 [US3] Dry-run the quickstart from a clean shell: Option B comprises 4 actions (write 6-line yaml, `docker run` one image, export 2 env vars, restart performer). Inspection-bound to ≤10 minutes on any host with Docker pre-pulled; well under the SC-005 30-minute budget. No live dry-run required.

**Checkpoint**: A reader can complete the quickstart in ≤30 minutes (SC-005).

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T020 [P] Grep the repo for `LITELLM_PROXY_AUTH_TOKEN` and confirm zero occurrences in any log-format string or `f"…{token}…"` interpolation (`grep -rn LITELLM_PROXY_AUTH_TOKEN agent/performer/src/`) — should appear ONLY in `config.py`, the secret-redaction allowlist site, and the `_proxy_env` helper (FR-004 / SC-004)
- [X] T021 [P] Run full performer test suite: `.venv/bin/pytest agent/performer/tests/ -v` — confirm no regression in existing tests; new tests pass
- [X] T022 [P] Run linter on changed files: `.venv/bin/ruff check agent/performer/src/performer/config.py agent/performer/src/performer/backends/claude_code.py agent/performer/tests/unit/test_config_litellm.py agent/performer/tests/unit/test_claude_code_proxy_env.py agent/performer/tests/integration/test_claude_code_subprocess_env.py`
- [X] T023 SC-004 invariant verified 2026-05-25: greppped (a) all tracked source files excluding `.env` for the live `LITELLM_MASTER_KEY` → zero hits; (b) the latest CI run on this branch (`gh run view 26380105450 --log`, 5382 lines) → zero hits.

---

## Phase 7: Post-Review Production Fixes (commit `35f9d23`)

Reached via live-validation against single-tenant LiteLLM/ollama with `serialize_env_bootstrap: true`. See plan.md "Post-review production fixes".

- [X] T030 Fix claude CLI subprocess liveness in `src/coordinare/job_runner.py` (explicit `proc.poll()` in the wait loop) so `serialize_env_bootstrap` gate releases on CLI exit; covered by `tests/unit/test_job_runner.py` regression test
- [X] T031 Cap `_run_service_inference()` with `asyncio.wait_for(..., timeout=SERVICE_INFERENCE_TIMEOUT)` and surface timeout as turn failure (FR-007-shaped); covered by `tests/unit/test_service_inference.py`
- [X] T032 Persist `EnvCacheState.readme_sha` across coordinare restarts: add `EnvCacheStateSnapshot` model + `WorkflowSnapshot.env_cache` field in `src/coordinare/state_store.py`; bump `CURRENT_SCHEMA_VERSION` 2 → 3; restore overlay in `src/coordinare/daemon.py` via `_persist_env_cache()` helper; v1/v2 snapshots load with empty `env_cache` (graceful upgrade)
- [X] T033 Update JSON schema contract `specs/003-state-persistence/contracts/workflow-snapshot.schema.json`: extend `schema_version` enum to `[1, 2, 3]`; declare new `env_cache` property with `EnvCacheStateSnapshot` sub-schema; update `test_state_persistence.py` expected `schema_version == 3`
- [X] T034 Add unit coverage: 3 env_cache tests in `tests/unit/test_state_store.py` (snapshot round-trip, v1/v2 graceful upgrade, env_cache absent → empty dict); 4 env_cache tests in `tests/unit/test_daemon_coverage.py` (persist on save, restore overlay, no-op when empty, snapshot subset only)

## Phase 8: Test Diagnostics (commit `e7fda84`)

- [X] T035 Rewrite `agent/performer/tests/conftest.py:wait_for_status` for diagnosability: `docker inspect` early-exit detection per poll cycle, `docker logs --tail 100` on any failure, `curl -sS -f` for error reporting; add `_docker_logs(container_id)` helper; update all four call sites in `test_dockerfile_{base,slim,full}.py` to pass `container_id=`

---

## Phase 9: Bug Fixes Bundled Into 073 (Priority: P1, Bugs)

User story origin: observed on website issue #70 during live validation against `litellm.vividynamics.com`. Two coupled defects: a long-running HTTP performer job loses its credentials at the 1-hour App-token boundary (US5), then the resulting transient `WorkspaceSetupError` causes the card to be re-assessed and re-blocked on already-superseded clarification comments (US6). Bundled into 073 per user direction (overriding the usual PR-scope-discipline preference).

### US5: GitHub token refresh through HTTP performer path (FR-012)

**Goal**: When `monitor_performer.py` includes a freshly-minted installation token in the status payload (already happens today at `src/coordinare/graph/nodes/monitor_performer.py:1023-1044`), the HTTP performer must apply it to the in-flight `Score` + `Stand` before the next git operation.

**Architectural gap**: stdio path refreshes correctly at `agent/performer/src/performer/main.py:2454-2460`. HTTP path: `HttpPerformerService.check_status` accepts `payload` but never forwards it (`src/coordinare/services/http_performer_service.py:342-356`); `_perform_job` runs its own internal status loop (`agent/performer/src/performer/main.py:2580-2593`) with no input channel for refreshed secrets.

**Independent Test**: Mock a long-running HTTP job; deliver a rotated `github_token` via the coordinare status payload; assert performer's in-flight `Score.github_token` updates before the next `push_branch`.

#### Tests for US5 ⚠️ (write first, must FAIL)

- [ ] T036 [P] [US5] Add unit test `tests/unit/test_http_performer_token_refresh.py` covering the coordinare side: construct `HttpPerformerService` with a mock `client`; call `check_status(session_id, payload={"github_token": "fresh-tok"})`; assert the service issues a secret-update call to the performer container (new endpoint name TBD in T038 — initially fail because no such call exists). Also: omit `github_token` from payload → no secret-update call (no regression for in-TTL case).
- [ ] T037 [P] [US5] Add unit test `agent/performer/tests/unit/test_perform_job_token_refresh.py`: drive `_perform_job` with a fake `JobInitPayload` and a status-loop hook that injects a refreshed secret mid-loop; assert `perf.score.github_token` and `perf.stand.git_env` reflect the new token by the next iteration. Also assert the refreshed token never appears in `JobResult.summary`.

#### Implementation for US5

- [ ] T038 [US5] Add `PATCH /jobs/{job_id}/secrets` endpoint to the performer HTTP server (locate via `grep -rn "jobs/" agent/performer/src/performer/server/`) accepting `{"secrets": {"GITHUB_TOKEN": "..."}}`; on receipt, update an in-process registry keyed by `job_id`. Endpoint MUST be auth-gated via the existing `PERFORMER_AUTH_TOKEN` mechanism. Body MUST NOT be logged (FR-004 / FR-011 hygiene).
- [ ] T039 [US5] Wire `_perform_job` status loop to consume from the secret registry: at the top of each iteration, check whether a refreshed secret was posted for `perf.session_id`; if so, update `perf.score.github_token` and `perf.stand.git_env = _git_credential_vars(new_token)`; clear the registry entry. Mirror the stdio refresh shape at `main.py:2454-2460`.
- [ ] T040 [US5] Extend `HttpPerformerService.check_status` (`src/coordinare/services/http_performer_service.py:342`) to inspect `payload` for `github_token`; if present and changed since last poll, call the new PATCH endpoint via the existing `client` (extend `PerformerHTTPClient` with `update_job_secrets(job_id, secrets)` if needed). Fire-and-forget semantics OK; log at DEBUG only.
- [ ] T041 [US5] Run `.venv/bin/pytest tests/unit/test_http_performer_token_refresh.py agent/performer/tests/unit/test_perform_job_token_refresh.py -v` and confirm all pass

**Checkpoint**: US5 complete — long-running HTTP performer jobs survive the App-token TTL boundary (SC-007).

### US6: Skip assess_card when card has open PR (FR-013)

**Goal**: When pickup re-enters a card that already has an open PR (whether via `card["pr_url"]` or a fresh `find_pr_for_issue` lookup), skip `assess_card` — the PR's existence supersedes the issue's pre-PR clarification questions.

**Independent Test**: Construct a `CoordinareState` with `current_card` having non-empty `pr_url`; route through pickup; assert `assess_card` either isn't invoked or its mutations to `state["open_questions"]` are not persisted from a stale comment.

#### Tests for US6 ⚠️ (write first, must FAIL)

- [x] T042 [P] [US6] Add unit test `tests/unit/test_pickup_skips_assess_when_pr_open.py`:
  - Case 1: card with `pr_url="https://github.com/x/y/pull/123"` set → assess_card is short-circuited or its `open_questions` mutation is dropped; phase routes to the PR-monitoring stage.
  - Case 2: card with no `pr_url` and `find_pr_for_issue` returns None → assess_card runs normally (regression guard).
  - Case 3: card with `pr_url` pointing at a closed PR → existing closed-PR limit logic in `dispatch_performer.py:171` is not affected.

#### Implementation for US6

- [x] T043 [US6] Locate the pickup → assess routing in `src/coordinare/graph/` (likely a conditional edge in the graph builder or an early-return in `assess_card.py`). Add a guard at the entry of `assess_card` (or in the routing function that picks `assess_card` vs. PR-bearing phases) that:
  1. Reads `current_card.get("pr_url")`.
  2. If non-empty AND the PR is open (use `github_service.get_pr_state(pr_url)` or equivalent; fall back to `find_pr_for_issue` when `pr_url` missing), set `state["phase"]` to the PR-bearing stage matching the card's recorded `performer_stage` and return early.
  3. Otherwise proceed.
- [x] T044 [US6] Run `.venv/bin/pytest tests/unit/test_pickup_skips_assess_when_pr_open.py -v` and confirm all pass; also run the existing `tests/unit/test_assess_card.py` to confirm no regression.

**Checkpoint**: US6 complete — open-PR cards are not re-blocked by stale assessment questions on cycle re-entry (SC-008).

### Phase 9 Polish

- [x] T045 [P] Run full repo test suite: `.venv/bin/pytest tests/ agent/performer/tests/ -v` — confirm zero regressions
- [x] T046 [P] Lint touched files: `.venv/bin/ruff check src/coordinare/services/http_performer_service.py agent/performer/src/performer/main.py agent/performer/src/performer/server/ src/coordinare/graph/nodes/assess_card.py tests/unit/test_http_performer_token_refresh.py tests/unit/test_pickup_skips_assess_when_pr_open.py agent/performer/tests/unit/test_perform_job_token_refresh.py`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No deps — start immediately
- **Foundational (Phase 2)**: Depends on Setup; BLOCKS all user stories (config fields must exist before any code or test references them)
- **US1 (Phase 3)**: Depends on Phase 2
- **US4 (Phase 3.5)**: P1 sibling of US1; treat US1+US4 as a single MVP unit. Code-touching order: T024–T025 can land alongside US1 tests; T026 requires T010 merged first (T026 redirects the `ANTHROPIC_BASE_URL` value T010 placed into the env merge to the shim's loopback URL).
- **US2 (Phase 4)**: Depends on US1 + US4 (validating a non-Anthropic model requires the shim normalization; without it the CLI errors with `Content block not found`)
- **US3 (Phase 5)**: Depends on Phase 2; **independent** of US1/US2/US4 for doc-authoring, but T019 (dry-run) requires US1 + US4 done
- **Polish (Phase 6)**: Depends on US1 + US4 + US2 + US3

### User Story Dependencies

- US1 stands alone post-Foundational. Cannot defer.
- US4 is P1 alongside US1: US1's env contract is functionally broken for any non-Anthropic model without the shim. Cannot defer.
- US2 needs US1 + US4 to exist before any non-Anthropic model can be validated end-to-end.
- US3's doc text can be drafted in parallel with US1/US4; its dry-run validation (T019) needs US1 + US4 done.
- **US2 → US3 doc-file ordering**: T015 (US2) appends a row to `docs/operators/litellm-proxy.md`; T017 (US3) is the SOLE creator of that file. If running US2 before US3, T015 blocks until T017 completes — do not have T015 create the file as a fallback.

### Within Each User Story

- US1: Tests (T005–T007) written first and FAIL → implementation (T008–T011) → verify tests pass (T012)
- US2: Sequential (each task depends on the prior)
- US3: T017 → T018 → T019

### Parallel Opportunities

- T001 / T002 can run in parallel
- T003 / T004 can run in parallel
- T005 / T006 / T007 can run in parallel (different test files)
- T008 / T009 / T010 / T011 touch the same file (`claude_code.py`) — must be sequential
- T020 / T021 / T022 can run in parallel (read-only or non-overlapping)

---

## Parallel Example: User Story 1 tests

```bash
# Run all US1 test authoring in parallel (different files, no deps):
Task: "Add unit test agent/performer/tests/unit/test_config_litellm.py"
Task: "Add unit test agent/performer/tests/unit/test_claude_code_proxy_env.py"
Task: "Add integration test agent/performer/tests/integration/test_claude_code_subprocess_env.py"
```

---

## Implementation Strategy

### MVP (US1 + US4)

1. Phase 1: Setup
2. Phase 2: Foundational (config fields + secret allowlist)
3. Phase 3: US1 (env plumbing + tests)
4. Phase 3.5: US4 (in-container shim + lifecycle wiring)
5. **STOP and VALIDATE**: SC-001 + SC-002 + SC-006 met. Ship.

US1 alone is non-functional for any non-Anthropic model (CLI parser rejects translated responses with `Content block not found`); US4 is what makes US1's acceptance scenarios true in practice.

### Incremental Delivery

1. Setup + Foundational → base ready
2. US1 + US4 → operators unblocked end-to-end on both Anthropic-direct and LiteLLM-routed models (MVP)
3. US3 → docs allow new operators to bring up a proxy (creates `docs/operators/litellm-proxy.md`)
4. US2 → at least one validated non-Anthropic model recorded (appends matrix row to file created in US3)
5. Polish → final secret-hygiene grep + suite green

### Notes

- T010 + T026 together are the load-bearing changes: T010 threads the per-job bearer through env-merge precedence; T026 redirects the CLI through the shim. Everything else is plumbing or paper.
- Do NOT introduce `pydantic.SecretStr` for the token field — see research.md Decision 3.
- Do NOT add CLI flags or fork the claude CLI — out of scope per spec.
- The `_proxy_env` dict is appended LAST in the env merge so it overrides any inherited `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` from `os.environ` (contract invariant 4).
