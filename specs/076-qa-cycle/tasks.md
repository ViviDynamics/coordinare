# Tasks — QA Cycle 076 (Dispatcher Dedup & Lifecycle Correctness)

**Spec:** [spec.md](./spec.md) | **Plan:** [plan.md](./plan.md) | **Research:** [research.md](./research.md) | **Data model:** [data-model.md](./data-model.md) | **Contracts:** [contracts/](./contracts/) | **Quickstart:** [quickstart.md](./quickstart.md)

5 user stories (3× P1, 2× P2). 27 functional requirements. 13 success criteria. All 5 clarifications from /speckit.clarify are baked into the design.

Per spec/AGENTS conventions:
- Run pytest via `.venv/bin/pytest`
- Run lint via `.venv/bin/ruff check`
- Every test file is paired with a documented FR or SC

---

## Phase 1 — Setup

- [X] T001 Add `dispatcher_dedup:` config block (with the 8 tunables from data-model §9) as a new Pydantic model `DispatcherDedupConfig` in `src/coordinare/config.py`; wire it onto the top-level `CoordinareConfiguration` with sensible defaults; preserve `extra="forbid"` semantics
- [X] T002 [P] Add `dispatcher_dedup:` block to all 5 active configs (`config.yaml`, `config.claude.yaml`, `config.hermes.yaml`, `config.junie.yaml`, `config.opencode.yaml`) with the same default values as T001, commented in place under the existing `persona_scope.ci_gate` block on the `website` symphony
- [X] T003 [P] Add `dispatcher_dedup:` block to all 6 example configs (`config.example.yaml`, `config.example.claude_code.yaml`, `config.example.codex.yaml`, `config.example.hermes.yaml`, `config.example.junie.yaml`, `config.example.opencode.yaml`)
- [X] T004 Bump `CURRENT_SCHEMA_VERSION` from 6 to 7 in `src/coordinare/state_store.py`; update the version-band comment block (lines 18–31) to document v7 fields (idle_timeout_retries, pr_artefacts_recorded_at, multi_pr_divergence, wedge_count_window, reconciliation_decisions_last_startup)
- [X] T005 Add a module-level `_DAEMON_STARTED_AT: datetime | None = None` plus `def get_daemon_started_at() -> str` accessor in `src/coordinare/daemon.py`; populate at daemon init with an ISO8601 UTC timestamp; expose via the existing health endpoint as a new `daemon_started_at` field

---

## Phase 2 — Foundational (BLOCKING — must complete before any user story)

### Snapshot schema + session round-trip

- [X] T010 Extend `PersistedSession` in `src/coordinare/state_store.py` with the 5 new fields from data-model §8 (idle_timeout_retries, pr_artefacts_recorded_at, multi_pr_divergence, wedge_count_window, reconciliation_decisions_last_startup); annotate Pydantic types matching data-model §4–§7
- [X] T011 Add the 5 new entity classes from data-model §3–§7 (`ReconciliationDecision` StrEnum, `WedgeResolution` StrEnum, `IdleTimeoutRetryRecord`, `MultiPRDivergence`, `ReconciliationReport`) to a new module `src/coordinare/services/dispatcher_dedup_models.py`; mark all BaseModels `extra="forbid"`
- [X] T012 Extend `_SESSION_FIELDS` tuple in `src/coordinare/session.py` with the 5 new field names from T010; add type hints to the `CardSession` TypedDict; ensure `create_session_from_card` initialises them to safe empty defaults
- [X] T013 [P] Write `tests/unit/test_session_076_round_trips.py` covering each of the 5 new fields: assert `session_to_state` then `state_to_session` returns byte-identical values for `idle_timeout_retries={"PVTI_X:implementing": IdleTimeoutRetryRecord(...)}`, `pr_artefacts_recorded_at=datetime.now(UTC)`, `multi_pr_divergence=MultiPRDivergence(...)`, `wedge_count_window={"PVTI_X": [datetime, datetime]}`, `reconciliation_decisions_last_startup={"PVTI_X": "adopted"}`
- [X] T014 [P] Write `tests/contract/test_state_persistence_v7.py`: load a v6 snapshot fixture, verify it parses cleanly with new fields at defaults; round-trip through v7 write and re-read; assert `schema_version == 7`

### Docker label plumbing (used by US1, US3, US5)

- [X] T015 Modify `start_ephemeral` in `src/coordinare/services/performer_lifecycle.py:88` to accept `extra_labels: dict[str, str] | None = None` keyword arg; validate keys match `^coordinare\.[a-z0-9._-]+$` and values are non-empty strings ≤256 chars; raise `LifecycleError` on invalid input; emit each as additional `--label key=value` args after the existing `coordinare.performer.id` label
- [X] T016 Modify `dispatch_card` in `src/coordinare/services/http_performer_service.py:229` to: (a) allocate a `session_id = str(uuid.uuid4())` BEFORE `start_ephemeral`; (b) build the 5-label dict (coordinare.session_id, coordinare.card_id, coordinare.performer_stage, coordinare.daemon_started_at, coordinare.spec_version="076") from card_context + state; (c) pass via `extra_labels`; (d) key `self._active_jobs` on the pre-allocated `session_id` (not the job-runner's job_id, which becomes a sub-field on `_EphemeralJob`)
- [X] T017 Add `job_id: str | None = None` field to `_EphemeralJob` dataclass at `src/coordinare/services/http_performer_service.py:84`; populate after the job-runner accepts the dispatch
- [X] T018 [P] Write `tests/contract/test_docker_label_schema.py`: mock `start_ephemeral`, dispatch a card, assert all 6 labels are emitted with the right shapes; assert invalid key/value inputs raise `LifecycleError`

### New module skeletons (used by US1, US3, US5)

- [X] T019 [P] Create `src/coordinare/services/dispatch_guard.py` with type-annotated stubs for `check_inflight`, `canonical_branch_name`, `compute_title_slug`, `drain_or_reap`, and the module-level `_dispatch_locks: dict[tuple[str, str], asyncio.Lock] = {}` registry from data-model §6 and research R-06
- [X] T020 [P] Create `src/coordinare/services/reconciliation.py` with type-annotated stubs for `run_startup_reconciliation`, `handle_potentially_stale_session`, `detect_wedged_state`; import the entities from `dispatcher_dedup_models`
- [X] T021 [P] Create `src/coordinare/services/retry_counter.py` with type-annotated stubs for `record_idle_timeout`, `attempts_in_window`, `reset_if_window_expired`, `should_block`; operates on `IdleTimeoutRetryRecord`
- [X] T022 [P] Create `src/coordinare/services/docker_executor.py` — thin wrapper around `docker ps` / `docker stop` / `docker port` / `docker inspect` subprocess calls, returning typed results; centralises subprocess and timeout handling so the rest of the codebase doesn't reach for `subprocess` directly

**Checkpoint:** After Phase 2, schema is v7-ready, all new modules exist as empty/stubbed types, and the Docker-label channel from dispatch through container labels is plumbed. NO behaviour change yet — just plumbing.

---

## Phase 3 — User Story 1 (P1): A Card Has At Most One Live Performer Container

**Goal:** restart-safe dispatch with no duplicate containers. Implements FR-001 to FR-014.

**Independent Test:** with one card `IN_PROGRESS` and a live performer container, restart coordinare — within 60 s either the original container is in `_active_jobs` (adopted) or it is stopped and one fresh container is launched. Never two.

### Canonical branch foundation (used by both US1 reconciliation matching and US4 dispatcher)

- [X] T030 [US1] Implement `compute_title_slug` in `src/coordinare/services/dispatch_guard.py` per the algorithm in `contracts/canonical-branch.md` (lowercase → non-alphanumerics to `-` → strip → 60-char `-`-boundary truncation)
- [X] T031 [P] [US1] Write `tests/contract/test_canonical_branch_contract.py` exercising all 8 mandatory test vectors from `contracts/canonical-branch.md`; assert a `SLUG_ALGORITHM_VERSION = 1` constant exists on `dispatch_guard.py`

### In-flight guard + per-card mutex

- [X] T032 [US1] Implement `check_inflight(state, card_id, performer_stage) -> InFlightGuardResult` in `src/coordinare/services/dispatch_guard.py` per `contracts/in-flight-guard.md`; calls `service.has_live_session(session_id)` when `agent_dispatch.session_id` is set
- [X] T033 [US1] Implement the per-card-stage mutex acquire/release via `async with _dispatch_locks[(card_id, performer_stage)]` helper context manager in `dispatch_guard.py`; emit `dispatch_performer.mutex_waited` log event when contention is observed (>0 ms wait)
- [X] T034 [US1] Insert the guard at the top of `dispatch_performer` in `src/coordinare/graph/nodes/dispatch_performer.py` — immediately after `_apply_pending_override`, before any other side-effect. Acquire the mutex; call `check_inflight`; on `advice="refuse"` emit `dispatch_performer.in_flight_guard_tripped` and return state unmodified
- [X] T035 [P] [US1] Write `tests/unit/services/test_dispatch_guard.py` for `check_inflight` happy + refuse paths
- [X] T036 [P] [US1] Write `tests/unit/services/test_dispatch_guard_mutex.py` — use `asyncio.gather` to launch 2 concurrent `dispatch_performer`-style coroutines on the same `(card_id, stage)`; assert the second one observes the first's state mutation (mutex serialised them); assert `dispatch_performer.mutex_waited` fires for the second
- [X] T037 [P] [US1] Write `tests/contract/test_inflight_guard_contract.py` asserting the 3 log events from `contracts/in-flight-guard.md` carry their required fields

### Docker executor + container enumeration

- [X] T040 [US1] Implement `docker_executor.list_containers_by_label(label_filters: dict[str, str], timeout: float = 5.0) -> list[ContainerInfo]` in `src/coordinare/services/docker_executor.py`; uses `docker ps --filter label=k=v` and parses JSON output; raises `DockerUnreachableError` on connection failure
- [X] T041 [US1] Implement `docker_executor.stop_container(container_id, timeout: float = 5.0)` using `docker stop --time=<n>`; falls back to `docker kill` on stop-timeout
- [X] T042 [US1] Implement `docker_executor.port_of(container_id) -> int` using `docker port <cid> 8088`; cached per container_id in a process-local dict to avoid repeated subprocess
- [X] T043 [P] [US1] Write `tests/unit/services/test_docker_executor.py` with mocked subprocess; cover happy path, timeout, Docker-unreachable, and malformed JSON output

### Startup reconciliation pass

- [X] T050 [US1] Implement `run_startup_reconciliation(state, docker_executor, *, budget_seconds=30.0) -> ReconciliationReport` in `src/coordinare/services/reconciliation.py` per `contracts/reconciliation-pass.md`. Algorithm:
  - List all containers with label `coordinare.spec_version=076`
  - For each card in snapshot with `phase ∈ {dispatching, monitoring_performer, monitoring_agent}` and non-empty `agent_dispatch.session_id`: classify container match → `adopt` / `reap_and_replace` / `fresh_dispatch`
  - For containers with no matching session: `orphan_swept`
  - For each `skipped_persistent` (service is mode=persistent): skip
  - Total wall-clock bounded by `budget_seconds`
  - On `DockerUnreachableError`: set `docker_unreachable=True` and return early
- [X] T051 [US1] Implement the adopt branch helper `_adopt(container_info, session_id, service) -> None` — constructs `_EphemeralJob(container_id=…, endpoint=…, client=PerformerHTTPClient(...))` from the live container and inserts into `service._active_jobs[session_id]`
- [X] T052 [US1] Implement the reap branch helper `_reap_and_replace(container_info, state, card_id, session_id) -> None` — `docker_executor.stop_container` the existing, clear `state.active_sessions[card_id].agent_dispatch` so next graph tick fresh-dispatches; emit `daemon.reap_failed` on stop failure but proceed
- [X] T053 [US1] Implement the orphan sweep helper `_sweep_orphan(container_info) -> None` — stop the container, emit `daemon.orphan_swept`
- [X] T054 [US1] Implement `_probe_job_runner_health(endpoint, timeout=15.0) -> bool` — calls `GET /healthz` on the container's port with the configured auth token; used by adopt vs reap classification
- [X] T055 [US1] Implement `handle_potentially_stale_session(state, card_id, docker_executor=None) -> ReconciliationDecision` in `src/coordinare/services/reconciliation.py` — single-card variant invoked by `check_board._is_stale`
- [X] T056 [US1] Wire `daemon.py` to call `run_startup_reconciliation` after snapshot load and before the first poll cycle; on `docker_unreachable=True`, emit `daemon.reconciliation_pass_aborted_docker_unreachable` and refuse to dispatch any ephemeral performer for the lifetime of this process (set a module flag)
- [X] T057 [US1] Replace the `check_board._is_stale` redispatch block at `src/coordinare/graph/nodes/check_board.py:484-498` with a call to `handle_potentially_stale_session`; emit `check_board.stale_session_reconciled` with the chosen decision branch in place of the existing `check_board.stale_session_redispatch` event
- [X] T058 [P] [US1] Write `tests/unit/services/test_reconciliation.py` covering `_enumerate_containers` / `_classify_container` / `_adopt` / `_reap_and_replace` / `_sweep_orphan` / `_probe_job_runner_health` independently with mocked docker_executor + service. Include one test per `ReconciliationDecision` enum value, including an explicit FR-011 test asserting `SKIPPED_PERSISTENT` is returned when the resolved service has `mode=persistent` AND no Docker calls fire for that branch
- [X] T059 [P] [US1] Write `tests/contract/test_reconciliation_log_events.py` asserting every event in the `contracts/reconciliation-pass.md` log-event table emits the required fields under its triggering branch
- [X] T060 [P] [US1] Write `tests/unit/graph/nodes/test_check_board_reconciliation.py` exercising the new `_is_stale` → `handle_potentially_stale_session` path
- [X] T061 [P] [US1] Write `tests/unit/graph/nodes/test_dispatch_performer_inflight_guard.py` exercising the guard integration in `dispatch_performer`

### Notification dedup (FR-010)

- [X] T062 [US1] Implement `should_emit_card_dispatched(state, card_id) -> bool` in `src/coordinare/graph/nodes/notify.py` per `contracts/notification-dedup.md` — reads `state.reconciliation_decisions_last_startup[card_id]`
- [X] T063 [US1] Wire the notification path: before emitting any `card_dispatched` event, call the helper; on suppress, emit `notify.card_dispatched_suppressed` (debug-level OK); on emit, emit `notify.card_dispatched_emitted` with the reason
- [X] T064 [US1] Clear `state.reconciliation_decisions_last_startup` at the end of the first poll cycle after startup (so it doesn't suppress mid-run notifications)
- [X] T065 [P] [US1] Write `tests/unit/graph/nodes/test_notify_dedup_076.py` exercising every row of the decision matrix from `contracts/notification-dedup.md`

### Integration regression for US1

- [X] T066 [US1] Write `tests/integration/test_restart_no_duplicate_dispatch.py` (FR-014 regression): simulate today's incident — persisted snapshot with card `IN_PROGRESS` + session id, no entry in `_active_jobs`, a still-running container labelled with that session id; run one reconciliation pass; assert either adopt-no-redispatch OR stop-and-replace single container; assert no other branch fires

**Checkpoint:** After Phase 3 — US1 is delivered. The MVP can ship here. A restart with a live container no longer produces a duplicate. SC-001 verifiable.

---

## Phase 4 — User Story 2 (P1): A Successful Performer Turn Is Never Forgotten

**Goal:** every DONE with new PR artefacts updates session state before the next dispatch. Implements FR-015 to FR-017.

**Independent Test:** with a card in `IN_PROGRESS` and no prior PR, run an implementer turn that opens a new PR; within one poll cycle `state.active_card.pr_url`, `pr_node_id`, `head_sha` reflect the new PR; survives daemon restart.

### Success-result schema (FR-017)

- [X] T070 [P] [US2] Define `PerformerSuccessResult` Pydantic model in a new module `src/coordinare/services/performer_result_schema.py` with required fields: `outcome` (Literal["done","partial_progress","blocked","idle_timeout"]), `pushed_branch: str | None`, `pr_url: str | None`, `pr_node_id: str | None`, `pr_number: int | None`, `head_sha: str | None`, `comment: str | None`, `next_focus: str | None`; `extra="forbid"`
- [X] T071 [US2] Modify the performer-status parser in `src/coordinare/graph/nodes/monitor_performer.py` (the place that today parses the JSON sentinel) to validate against `PerformerSuccessResult`; on validation failure, treat as a malformed result and route to `partial_progress` (with a `monitor_performer.malformed_success_result` log event) rather than silently accepting
- [X] T072 [P] [US2] Write `tests/unit/services/test_performer_result_schema.py` exercising valid cases (each outcome) and invalid cases (missing required fields for DONE, extra fields, wrong types)

### PR-artefact write-through (FR-015, FR-016)

- [X] T073 [US2] Implement `_record_pr_artefacts(state, success_result)` helper in `src/coordinare/graph/nodes/monitor_performer.py`: if `success_result.pr_url` or other artefact fields are populated, overwrite the corresponding fields on `state.active_card`, `state.active_sessions[card_id]`, set `state.active_sessions[card_id].pr_artefacts_recorded_at = datetime.now(UTC)`
- [X] T074 [US2] Wire the snapshot write barrier: after `_record_pr_artefacts` modifies state, the existing `_persist_active_sessions` / snapshot writer in `daemon.py` MUST flush within the same poll cycle (synchronously); add a structured `monitor_performer.pr_artefacts_recorded` log event with the field deltas
- [X] T075 [US2] Branch-contract verification (FR-023 referenced; partial impl here): after `_record_pr_artefacts`, compare `success_result.pushed_branch` with the canonical branch the dispatcher sent in `JobInitPayload`; if they disagree → reject the DONE outcome, emit `monitor_performer.branch_contract_violated`, transition card to BLOCKED with structured reason
- [X] T076 [P] [US2] Write `tests/unit/graph/nodes/test_monitor_performer_pr_artefacts.py` for happy path (DONE with new PR fields → state updated), partial fields (only pr_url provided), no PR fields (DONE on a card with no new artefacts → no-op), and branch-contract violation
- [X] T077 [US2] Write `tests/integration/test_pr_artefacts_round_trip.py` (FR-015 regression): simulate today's incident — implementer DONE opens new PR; assert active_card.pr_url updates within the poll cycle, snapshot reflects the new PR, daemon restart preserves the new PR — never reverts to the prior stale PR

**Checkpoint:** After Phase 4 — US2 is delivered. SC-008 verifiable; no more silent loss of successful turns.

---

## Phase 5 — User Story 3 (P1): Lifecycle Advancement on Terminal Outcomes

**Goal:** every terminal performer outcome drives an explicit recorded transition. Implements FR-018 to FR-021 plus FR-007 (drain) plus FR-020 (wedge invariant).

**Independent Test:** dispatch an implementer for a card with a clear `lifecycle_sequence` ending in `reviewing`; on DONE within one poll cycle `performer_stage=reviewing` and a reviewer dispatch is queued; on IDLE_TIMEOUT, the retry counter increments and a recorded transition happens.

### DONE advances stage (FR-018)

- [X] T080 [US3] Modify the DONE-handling branch in `src/coordinare/graph/nodes/monitor_performer.py` to: (a) verify `_record_pr_artefacts` ran first (T073); (b) compute next stage from `state.lifecycle_sequence`; (c) if next stage exists → set `state.performer_stage=next`, `state.phase="dispatching"`, clear `agent_dispatch`; (d) if `lifecycle_sequence` exhausted → move card to terminal board column (existing path), clear `active_card`. Emit `monitor_performer.lifecycle_advanced` event with `from_stage`, `to_stage`, `card_id`, `head_sha`
- [X] T081 [P] [US3] Write `tests/unit/graph/nodes/test_monitor_performer_lifecycle_advance.py` covering: DONE → next stage queued, DONE on last stage → board terminal column move + active_card cleared, DONE without valid PR artefacts → routed back to malformed-result path (T071), DONE with `pushed_branch` mismatch → BLOCKED (T075)

### Retry counter for IDLE_TIMEOUT (FR-019)

- [X] T082 [US3] Implement `retry_counter.record_idle_timeout(state, card_id, performer_stage, *, budget=2, window_hours=24) -> Literal["retry", "block"]` in `src/coordinare/services/retry_counter.py`: reads / mutates the `IdleTimeoutRetryRecord` on `state.active_sessions[card_id].idle_timeout_retries[<key>]`; resets the window if `window_start_at` older than 24 h; returns `retry` if `attempt_count < budget` after increment, else `block`
- [X] T083 [US3] Wire IDLE_TIMEOUT handling in `monitor_performer.py`: call `record_idle_timeout`; on `retry` → drain prior container via `drain_or_reap`, set `phase=dispatching, agent_dispatch={}`, emit `monitor_performer.idle_timeout` with `attempt`/`budget`; on `block` → move to BLOCKED, emit `card_blocked` notification with `reason="idle_timeout_exhausted"`, emit `monitor_performer.idle_timeout_exhausted`
- [X] T084 [P] [US3] Implement `drain_or_reap(session_id, *, drain_budget=5.0, reap_budget=5.0) -> tuple[Literal["drained","reaped"], float]` in `src/coordinare/services/dispatch_guard.py` per `contracts/in-flight-guard.md` "Relay-handoff containment" section; total budget hard-capped at 10 s
- [X] T085 [US3] Wire `drain_or_reap` into the `partial_progress` relay branch at `src/coordinare/graph/nodes/monitor_performer.py:2161-2186` so prior container is drained before the next dispatch (FR-007)
- [X] T086 [P] [US3] Write `tests/unit/services/test_retry_counter.py` covering: first idle-timeout creates record, second increments, third triggers `block`, window-expired resets to 1, persistence round-trip
- [X] T087 [P] [US3] Write `tests/unit/graph/nodes/test_monitor_performer_idle_timeout.py` covering retry path, exhausted path, persistence across simulated daemon restart (T086 already tests in-process; this one tests the integration with session restore)
- [X] T088 [P] [US3] Write `tests/unit/services/test_dispatch_guard_drain.py` for `drain_or_reap`: drain succeeds within budget → "drained", drain times out → "reaped", docker stop fails → escalates to docker kill, total elapsed always ≤10 s

### Wedge invariant (FR-020)

- [X] T090 [US3] Implement `detect_wedged_state(state) -> WedgeResolution | None` in `src/coordinare/services/reconciliation.py`: returns `None` if state is not wedged; otherwise applies the default RELEASED resolution (clarification Q1) — sets `state.active_card = None`, appends `datetime.now(UTC)` to `state.active_sessions[card_id].wedge_count_window[card_id]`, trims entries older than 24 h, returns `WedgeResolution.RELEASED`
- [X] T091 [US3] Implement the BLOCKED promotion path: if the count of entries in `wedge_count_window[card_id]` within the trailing `dispatcher_dedup.wedge_block_window_hours` (default 24) reaches `dispatcher_dedup.wedge_block_threshold` (default 3), promote next wedge to `BLOCKED` instead of `RELEASED`; emit `card_blocked` notification with `reason="wedge_block_threshold_exceeded"`
- [X] T092 [US3] Wire `detect_wedged_state` into `daemon.py`'s per-cycle driver as a `finally:` block at the end of `_run_symphony_cycle` (or equivalent) so the invariant runs even if the cycle body raised; emit `daemon.wedged_state_detected` + `daemon.wedge_resolution` events
- [X] T093 [P] [US3] Write `tests/unit/test_daemon_wedge_invariant.py` covering: no-wedge (returns None), wedge → RELEASED default, wedge → BLOCKED after 3-in-24h, invariant runs in `finally:` even when cycle body raises
- [X] T094 [US3] Write `tests/integration/test_wedged_state_recovery.py` (FR-020 regression): construct today's incident state (active_card pinned + empty sessions + phase=idle); run one poll cycle; assert release-the-pin happened within ≤30 s; assert card eligible for re-pickup; assert `daemon.wedge_resolution` event with `resolution=released`

### Forbidden-state assertion (FR-018, FR-020)

- [X] T095 [P] [US3] Add an assertion in `src/coordinare/graph/state.py` (or wherever `CoordinareState` is materialised at cycle end) that the forbidden combination `active_card != None AND active_sessions[active_card.id] missing AND performer_stage is None AND phase in {None, "idle"}` cannot survive a cycle end without `detect_wedged_state` having recorded a resolution; raise `WedgedStateUnresolvedError` in dev mode, log a critical event in production

**Checkpoint:** After Phase 5 — US3 is delivered. SC-009, SC-010, SC-013 verifiable. With Phases 3+4+5, the MVP is feature-complete for all P1 stories.

---

## Phase 6 — User Story 4 (P2): One Card, One PR (No Silent Branch Forking)

**Goal:** dispatcher injects canonical branch + multi-PR detection blocks divergence. Implements FR-022 to FR-024.

**Independent Test:** with a card already having one open PR on branch B1, dispatch a new performer turn; performer's workspace is initialised on B1 and resulting commits land on B1 — never a new branch.

### Canonical branch resolution

- [X] T100 [US4] Implement `canonical_branch_name(card) -> CanonicalBranchName` in `src/coordinare/services/dispatch_guard.py` per `contracts/canonical-branch.md`; uses `compute_title_slug` (T030)
- [X] T101 [US4] Implement `resolve_branch_for_dispatch(card, github_service, owner, repo) -> tuple[str, BranchResolution]` in `src/coordinare/services/dispatch_guard.py`: queries GitHub for open PRs on the canonical prefix; returns the canonical name + resolution code (`reused_existing` / `fresh_canonical` / `legacy_divergence`)
- [X] T102 [US4] Modify `dispatch_performer` in `src/coordinare/graph/nodes/dispatch_performer.py` to call `resolve_branch_for_dispatch` before invoking the service; on `legacy_divergence` → refuse dispatch (emit `dispatch_performer.legacy_branch_divergence_refused`); inject the resolved branch into the workspace/initialisation pipeline
- [X] T103 [US4] Add `canonical_branch: str` to the `JobInitPayload` schema (likely in `src/coordinare/models/performer_endpoint.py`); populate from T102 in `dispatch_card`
- [X] T104 [US4] Update the persona/prompt-template wiring in `src/coordinare/services/persona_service.py` (or wherever prompts are assembled) so the implementer's prompt explicitly references `{canonical_branch}` and contains the "do NOT create a new branch" instruction from `contracts/canonical-branch.md`
- [X] T105 [P] [US4] Write `tests/unit/services/test_canonical_branch_naming.py` for `canonical_branch_name` edge cases (empty title, all-symbol title, exactly-60 char, 61-char)

### Multi-PR detection (FR-024)

- [X] T110 [US4] Implement `github.list_prs_by_branch_prefix(owner, repo, prefix, *, state="OPEN", limit=20) -> list[PRInfo]` in `src/coordinare/services/github.py` using GraphQL per research R-04
- [X] T111 [US4] Implement `detect_multi_pr_divergence(state, card_id, github_service, trigger: Literal["dispatch", "restart", "webhook"]) -> MultiPRDivergence | None` in `src/coordinare/services/dispatch_guard.py`; returns the divergence record if >1 open PR found; populates `state.active_sessions[card_id].multi_pr_divergence`
- [X] T112 [US4] Wire the check at the 3 trigger points (clarification Q4): (a) at the top of `dispatch_performer` immediately after the in-flight guard passes — `dispatch` trigger; (b) in the startup reconciliation pass for every in-flight card — `restart` trigger; (c) in any existing webhook handler for `pull_request.opened` / `pull_request.edited` events (location TBD via code search) — `webhook` trigger
- [X] T113 [US4] Wire the response: on divergence detected at `dispatch` trigger → refuse the dispatch, emit `dispatch_performer.multi_pr_divergence_refused`; at `restart` or `webhook` trigger → transition card to BLOCKED, emit `card_blocked` notification listing both PR numbers
- [X] T114 [P] [US4] Write `tests/unit/services/test_multi_pr_divergence.py` for: zero PRs → None, one PR → None, two PRs → MultiPRDivergence record populated, GitHub-error → returns None and logs a warning
- [X] T115 [US4] Write `tests/integration/test_multi_pr_divergence_blocks.py` (FR-024 regression): simulate the card #101 scenario — two open PRs with `coordinare/<cid>/*` prefix; assert dispatch refused, card moved to BLOCKED, structured event surfaces both PR numbers

**Checkpoint:** After Phase 6 — US4 delivered. SC-011 verifiable; no more silent branch forking.

---

## Phase 7 — User Story 5 (P2): Board ↔ Local State Stay In Sync

**Goal:** per-cycle reconciliation between project-board Status and `state.active_card.status`. Implements FR-025 to FR-027.

**Independent Test:** with a card `IN_PROGRESS` on the board and `state.active_card.id == card_id`, manually move the card to TODO on the board; within one poll cycle coordinare either re-acquires (moves back to IN_PROGRESS) or releases the pin (`active_card=None`).

### Per-cycle board reconciliation

- [X] T120 [US5] Implement `reconcile_board_state(state, board_snapshot) -> BoardReconciliation` in `src/coordinare/services/reconciliation.py`: compares `state.active_card.status` to `board_snapshot[active_card.id].Status`; returns one of `agreed | reacquired | pin_released | blocked`
- [X] T121 [US5] Wire the call into `daemon.py`'s per-cycle driver — at the start of each cycle, after fetching the board snapshot, before any dispatch decision; emit `daemon.board_state_reconciled` with local_status, board_status, action; default action on disagreement: release the pin (consistent with FR-020 / clarification Q1)
- [X] T122 [P] [US5] Write `tests/unit/services/test_board_state_reconciliation.py` covering: agreed (no-op), board=TODO local=IN_PROGRESS (release pin), board=DONE local=IN_PROGRESS (release pin), board missing card entirely (release pin + warning)

### Dashboard divergence surfacing (FR-027)

- [X] T125 [P] [US5] In `src/coordinare/dashboard.py`, render the board status AND `active_card.status` side-by-side in the existing symphony view; when they disagree, apply the existing `warning` CSS class (no new design tokens per Constitution III)
- [X] T126 [P] [US5] Surface `MultiPRDivergence` records in the symphony detail view: if `state.active_sessions[card_id].multi_pr_divergence` is non-None, render a banner with both PR numbers as links; reuse the existing banner component
- [X] T127 [P] [US5] Surface wedge resolutions in the symphony detail view: render `state.active_sessions[card_id].wedge_count_window` count over the trailing 24 h with a colour hint (≥2 = warning)
- [X] T128 [P] [US5] Write `tests/unit/test_dashboard_076_surfaces.py` asserting all three new surfaces render their data correctly given synthetic state fixtures

**Checkpoint:** After Phase 7 — US5 delivered. SC-012 verifiable; all 5 user stories complete.

---

## Phase 8 — Polish & Cross-Cutting

### Performance benchmarks (SC-002, hot-path budgets)

- [X] T140 [P] Write `tests/perf/test_reconciliation_latency.py`: 100-trial benchmark of `run_startup_reconciliation` with 5 mocked in-flight cards + 3 mocked orphans, assert p95 ≤ 30 s wall-clock and zero-card path ≤ 500 ms; fail CI if p95 regresses >10 %
- [X] T141 [P] Write `tests/perf/test_inflight_guard_microbench.py`: 10 000-iteration microbenchmark of `check_inflight`, assert p95 ≤ 5 ms uncontested, p99 ≤ 10 ms
- [X] T142 Add the perf tests to CI workflow under `.github/workflows/` so they run on every PR; ensure they have their own pytest marker so the main suite isn't slowed when they run
- [X] T143 [P] Write `tests/perf/test_dispatch_success_regression.py` (SC-006 regression bound): captures the dispatch-success rate over a 50-trial simulated workload BOTH against (a) `git checkout` of pre-076 main and (b) HEAD. Asserts the delta is ≥ -1 percentage point (i.e., new code does not regress by more than 1 pp). Skipped by default; CI runs it explicitly via the perf marker. Documents the baseline in the test docstring with a captured-at timestamp

### Cross-cutting cleanup

- [X] T150 Run `.venv/bin/ruff check src/coordinare/services/dispatch_guard.py src/coordinare/services/reconciliation.py src/coordinare/services/retry_counter.py src/coordinare/services/docker_executor.py src/coordinare/services/dispatcher_dedup_models.py src/coordinare/services/performer_result_schema.py src/coordinare/graph/nodes/ src/coordinare/daemon.py src/coordinare/session.py src/coordinare/state_store.py src/coordinare/config.py tests/` — fix any lint findings
- [X] T151 Run the full suite `.venv/bin/pytest -q` — expect all existing tests still pass; document any pre-076 tests that needed updates (e.g., snapshot fixture bumps to v7) in the PR description
- [X] T152 Document the new log-event vocabulary in `AGENTS.md`'s "Structured Events" section (or create the section if absent) — at minimum list every event from contracts/ with a one-line description
- [X] T153 Update `AGENTS.md` footer with the spec 076 line entry following the established convention (matches 074, 075 precedents)
- [X] T154 Update [project_roadmap.md](file://~/.claude/projects/-Users-local-Workspaces-ViviDynamics-coordinare/memory/project_roadmap.md) memory to mark 076 as the latest landed spec
- [X] T155 Manual operator quickstart walkthrough on a scratch repo per `quickstart.md` Steps 1–6; record observed behaviour vs expected in a quickstart-validation comment on the PR (mirrors 075 Phase 8 precedent)

---

## Phase 9 — Review hardening + live-test fixes (post-implementation)

Fixes landed on the same `076-qa-cycle` branch during PR #94 review
rounds (R1–R6) and the 2026-05-29 live test. Each is a discrete commit;
all carry regression tests. Recorded here so the task ledger reflects
the shipped code, not just the original 99-task plan.

### Review-round fixes (PR #94 code review)

- [X] T160 [R1] FR-003 adoption port resolution — `reconciliation._adopt` resolved the container's real host port via `docker_executor.port_of` instead of binding a `127.0.0.1:0` placeholder; falls through to reap-and-replace when the port can't be resolved. Regression: `test_fr014_adopted_client_points_at_real_host_port`. Commit `1a56619`.
- [X] T161 [R1] Session-id collision logging — `run_startup_reconciliation` logs `daemon.reconciliation_session_id_collision` and keeps the most-recently-started container when two carry the same `coordinare.session_id`. Commit `1a56619`.
- [X] T162 [R2] FR-020 per-card wedge windows — `wedge_count_window` corrected from a flat `list[datetime]` to the spec-final `dict[str, list[datetime]]` (PersistedSession, CardSession, `_SESSION_FIELDS`, `initial_state`, `detect_wedged_state`). Regression: `test_different_cards_have_isolated_wedge_windows`. Commit `c6b893e`.
- [X] T163 [R2] FR-010 notify dedup race — `reconciliation_decisions_last_startup` is now popped on consume in `notify.py` so a genuine mid-cycle dispatch for an ADOPTED card isn't silently suppressed; removed the redundant end-of-cycle-1 clear in `daemon.py`. Commit `c6b893e`.
- [X] T164 [R2] FD leak on docker timeout — `docker_executor._run_docker` awaits `proc.wait()` (bounded, `contextlib.suppress(TimeoutError)`) after `proc.kill()` so child stdout/stderr FDs are reaped on repeated timeouts. Commit `c6b893e`.
- [X] T165 [R2] Schema-rollback error message — `StateLoadError` for an unreadable future-version snapshot now names the remediation (restore a compatible snapshot or upgrade). Commit `c6b893e`.
- [X] T166 [R3] Legacy flat-list `wedge_count_window` load — `@field_validator(mode="before")` on `PersistedSession` coerces a legacy flat-list shape to `{}` so a pre-fix dev-build snapshot loads instead of raising ValidationError at startup. Regression: `test_v7_legacy_flat_wedge_window_coerced_to_empty_dict`. Commit `ae01614`.
- [X] T167 [R-live] Config validator allowlist — `config_validation.py` now permits the top-level `dispatcher_dedup` block; without it the daemon raised `config_validation_error` at boot. Commit `f88f57d`.
- [X] T168 [R4] FR-020 wedge-threshold off-by-one — promotion changed from `>= threshold` to `> threshold` so the spec's "after N wedges, the NEXT wedge promotes" holds (wedges 1–N release; wedge N+1 blocks). Also: removed stale "Stubs" comment, dead `replace` import, standardised `daemon.reap_failed` field set, and routed `drain_or_reap` budgets from `dispatcher_dedup` config at both call sites. Commit `a01f63d`.
- [X] T169 [R5/R6] Lazy-import + docstring + doc-drift cleanup — hoisted redundant in-function `datetime` imports to module level in `monitor_performer` + `dispatch_guard`; corrected `_record_pr_artefacts` docstring (return-value vs side-effect write paths); fixed test name; aligned data-model/plan docs to `reconciliation.detect_wedged_state` and the `>`-threshold wording. Commits `dba1a5d`, `3b40462`.

### Live-test fix (2026-05-29, card #101)

- [X] T170 [LIVE-CRITICAL] FR-019 idle-timeout detector realigned to the real performer error string. The claude_code backend emits `"claude CLI idle for {N}s with no terminal event"` (and `stop_reason="idle_timeout"`), but the 076 detector matched the substring `"idle timeout"` — which never appears — so every real idle-timeout fell through to the generic error→blocked path and the FR-019 retry counter never fired. Broadened detection to cover `status=="idle_timeout"`, `stop_reason=="idle_timeout"`, and an error reason mentioning "idle" + ("no terminal" | "idle for" | "idle timeout"). Regressions: `test_monitor_performer_real_claude_idle_error_string_routes_to_retry`, `test_monitor_performer_stop_reason_idle_timeout_routes_to_retry` (both use the exact backend strings). Commit `457022c`.

### Live-test fix #2 (2026-05-29, card #149)

- [X] T171 [LIVE] Empty-output churn cap. Card #149 (the re-queued duplicate of #101) ran the architect ~17 min on qwen3.6:35b and returned `"Backend produced an empty architecture plan"` — twice across #101/#149 on the identical card, i.e. a *deterministic* model-capability failure. Pre-T171 this looped forever: `error → blocked → handle_blocked (no questions) → requeue to TODO → re-dispatch → empty → …`, re-running a 17-min stage and pinging Slack every cycle. This is a US3 lifecycle-correctness gap (a terminal outcome that does not drive a *bounded* transition). Fix: detect empty-output terminal errors in `monitor_performer` (reason matches "empty architecture plan" / "produced an empty" / "empty implementation" / "empty output") and route through a new `retry_counter.record_empty_output` — a low-budget (default **1**, vs idle-timeout's 2) rolling-window counter namespaced `:empty_output` so it's independent of the idle-timeout counter (no schema change). On budget exhaustion: `phase=blocked` **with** an operator-facing `open_questions` entry, which makes `handle_blocked` keep the card blocked instead of requeuing — converting infinite churn into fail-fast-then-escalate. New config: `dispatcher_dedup.empty_output_retries` (default 1) + `empty_output_window_hours` (24), added to all 5 active + 7 example configs. Tests: `test_retry_counter.py` (independence from idle counter, budget-0 immediate block), `test_monitor_performer_empty_output.py` (uses the exact backend string, asserts retry→block→operator-question, no idle-counter bleed). Commit: `7917ab2`.

### Live-test fix #3 (2026-05-29, card #150 — clean architect-QA re-run)

Re-running the architect QA on a pristine duplicate (#150) on a **cold env
cache** surfaced a cascade of dispatcher/bootstrap correctness gaps that the
prior warm-cache runs (#101/#149) had masked. All five carry production-accurate
regression tests.

- [X] T172 [LIVE] env_bootstrap empty-label deadlock. The 076 Docker-label code (`http_performer_service.dispatch_card`) unconditionally set `coordinare.card_id` / `coordinare.performer_stage`, but the symphony-scoped env_bootstrap performer has no card context → empty-string label values → `_validate_extra_label` rejected the launch (`ContainerStartError`). The bootstrap container never started, yet `serialize_env_bootstrap` believed one was in-flight and **held every card dispatch indefinitely** — a hard deadlock on any cold start (empty env-cache). Fix: only emit the card-scoped labels when populated (reconciliation adopts strictly by `coordinare.session_id`, so omitting them on card-less containers is safe). Regression: `test_dispatch_card_omits_empty_card_labels_for_bootstrap` (mirrors `start_ephemeral`'s real validation). Commit `eb6ddeb`.
- [X] T173 [LIVE] Held-dispatch `card_dispatched` Slack leak. A held dispatch (env-cache-not-ready / env_bootstrap_in_flight / in-flight guard) returns from `dispatch_performer` with `phase="dispatching"` and **no container started**; `notify` mapped that phase to `card_dispatched` and re-emitted every poll cycle. The per-channel 600 s dedup window swallowed the repeats but leaked one duplicate "dispatched to <stage>" post each time the window lapsed (observed: `delivered` events exactly 600 s apart for a card that never launched a performer). Fix: a genuine dispatch always advances to `monitoring_performer` before `notify`, so suppress `card_dispatched` when `phase=="dispatching"`. Distinct from the `dispatched_notified_stages` gate (that covers the `monitoring_performer` re-emit). Test: `test_notify_suppresses_card_dispatched_when_dispatch_held` (+ repointed the `phase="dispatching"` card_dispatched triggers across notify/integration suites to `monitoring_performer`). Commit `98694fd`.
- [X] T174 [LIVE] env_bootstrap wall-clock budget + reap. The bootstrap poll loop ran a hardcoded `720×10s` (~2 h) ceiling with no early reap, so a slow/hung bootstrap gated the symphony for up to 2 h (and via T173 spammed Slack the while). Fix: `Config.bootstrap_max_seconds` (default **3600**; 0 = legacy ~2 h) derives the poll-attempt cap, and the timeout path now explicitly `docker stop`s the container (it was started `--rm` but a hung agent never exits). A failed bootstrap leaves the cache un-ready → next cycle re-dispatches (automatic retry). Idle-based reap was considered and **deferred** (the container's `docker logs` are dominated by coordinare's own `GET /jobs` access lines, so no reliable cross-backend idle signal). Regression: `test_budget_exhausted_reaps_container_and_marks_failure`. Commit `98694fd`.
- [X] T175 [LIVE-CRITICAL] env_bootstrap `service_inference` timeout made non-fatal — the dominant blocker. In the performer (`agent/performer/.../main.py`), the env_bootstrap handler ran the dev-env install (into the mounted cache) and then `service_inference` wrapped in `asyncio.wait_for(timeout=600)`. On slow qwen, inference exceeds 600 s; the external `TimeoutError` escaped `_run_service_inference`'s own "don't crash bootstrap" failsafe and returned `status="error"`. Coordinare read that as bootstrap failure, left the cache un-ready, and **re-dispatched forever** (observed: 3 attempts, 0 successes, ~30–45 min each, Slack leak every ~10 min). `service_inference` is a best-effort 063 telemetry probe — the install that determines cache readiness already succeeded. Fix: on timeout, return `status="env_bootstrap_complete"` with `inference_succeeded=False` + `inference_skipped_reason="timeout"` (matches the internal failsafe), so the cache becomes ready and degraded inference surfaces on the dashboard instead of wedging the symphony. **Requires a performer image rebuild.** Test updated: `test_inference_timeout_is_non_fatal_and_marks_complete` (was `..._returns_error_response`). Commit `2fea2cc`.
- [X] T177 [LIVE] env_bootstrap idle reap (hardening). The T174 wall-clock budget (`bootstrap_max_seconds`) bounds total runtime but can't distinguish "slow but progressing" from "hung" — a stuck bootstrap still burns the full hour. Added `bootstrap_idle_timeout_seconds` (default **600**; 0 disables): the poll loop diffs the container's *meaningful* log output each cycle (filtering coordinare's own `GET /jobs/<id>` status-poll lines via `_bootstrap_progress_lines`, leaving LLM `shim request` / `service_inference` / install output) and, if it's frozen for the idle window, reaps the container early and marks the bootstrap failed (→ re-dispatch). Factored the reap into `_reap_bootstrap_container` shared by the idle + wall-clock paths; bumped the log snapshot to `--tail 500` so meaningful lines don't scroll out of the window under poll noise. Motivated by the live #150 run where the bootstrap legitimately ground ~36 min before completing — idle reap leaves a slow run alone while catching a truly hung one within 10 min instead of 60. Tests: idle-fires-when-frozen, not-triggered-while-progressing, disabled-when-zero, plus reap-failure-non-fatal. Commit: pending.
- [X] T176 [LIVE] Resume re-adopted IN_PROGRESS cards at the earliest INCOMPLETE stage (US3 lifecycle correctness). `create_session_from_card` hardcodes `performer_stage="implementing"`; re-adopting an IN_PROGRESS card with no durable snapshot stage (snapshot lost/corrupted, or a human dragged a card into IN_PROGRESS the daemon never saw) accepted that default and **skipped an assessor/architect that never actually finished**, running the implementer with no plan. A half-finished `_resume_stage` block in `check_board` (computed, never used — dead code) had attempted this with the wrong policy. Fix: `_derive_resume_stage` probes the card branch for each stage's output artifacts (`assessment.md` → assessing done; `plan.md`+`tasks.md` → architecting done; via `get_file_blob_sha`) and resumes at the earliest stage whose artifact(s) are missing — never skipping an incomplete stage, never re-running a completed one. Best-effort and fail-safe (any GitHub error / no client / degenerate title → `None` → caller keeps prior default; runs only on the rare re-adopt-without-snapshot-stage path). `_card_docs_dir` mirrors the performer's `_doc_folder` slug exactly. Validated live: #150 (assessment.md only) re-adopted and resolved to `architecting`. Regressions: `TestDeriveResumeStage` (assessment-only→architecting, plan+tasks→implementing, partial→architecting, none→assessing, + None-github / GitHub-error / missing-owner-repo fail-safes). Commit `8e469ca`.

- [X] T178 [LIVE] Promote the milestone-based architect + implementer contract prompts to the committed defaults. The live-test persona overrides (architect: high-level 3-7 `## Milestones` plan with Goal/Scope/Done-when + `## Affected Modules`, `Forbidden: implementing`; implementer: ONE MILESTONE PER TURN procedure + partial_progress relay) were validated as well-formed and **fully delivered to the model** (confirmed in the captured Anthropic `system` block — the earlier "persona not injected" reading was a false alarm from inspecting `messages[]` instead of the top-level `system` field). Moved both verbatim from the gitignored `config.claude.yaml` override into `persona_service.DEFAULT_INSTRUCTIONS` so every backend/deployment gets the improved, smaller-context contract by default (the old default's exhaustive "every file + every step" plan overran smaller-context models). `_CI_COMMITTER_DIRECTIVE` preserved in the implementer. Note: this does NOT fix qwen3.6:35b — it received the explicit `Forbidden: implementing` contract and implemented anyway (a model instruction-following limitation, likely aggravated by the large Claude Code system prompt; tracked separately, candidate for a leaner backend/stronger architect model). Tests: existing persona suite (content-agnostic equality) green; coverage 90.04%. Commit: pending.

**Phase 9 total: 19 tasks (T160–T178), all complete.** Every fix carries
at least one regression test using production-accurate inputs. The five
T172–T176 fixes came from the 2026-05-29 clean-cache architect-QA re-run:
the empty env-cache exposed a deadlock (T172) and a bootstrap-failure thrash
(T175) that warm-cache runs never hit, plus the notification-leak (T173) and
resume-stage (T176) correctness gaps they made visible.

---

## Dependency graph

```
Phase 1 (Setup) ──────────────────┐
                                  ├─→ Phase 2 (Foundational) ─┐
                                  │                            │
                                  │                            ├─→ Phase 3 (US1) ─┐
                                  │                            │                  │
                                  │                            ├─→ Phase 4 (US2) ─┤
                                  │                            │                  ├─→ Phase 8 (Polish)
                                  │                            ├─→ Phase 5 (US3) ─┤
                                  │                            │                  │
                                  │                            ├─→ Phase 6 (US4) ─┤
                                  │                            │                  │
                                  │                            └─→ Phase 7 (US5) ─┘
                                  │
                                  (T002, T003 may run in parallel with T001 — both are config edits in different files)
```

User stories are independent within Phase 2's foundation:
- **US1 ↔ US2:** no overlap (US1 is dispatcher mechanics, US2 is success-result schema)
- **US1 ↔ US3:** US3 reuses `drain_or_reap` (US1 task T084 in Phase 3 but logically depends on dispatch_guard module — built in T019)
- **US1, US2, US3 ↔ US4, US5:** US4/US5 are P2 and gated on US1 only via Phase 2 plumbing
- **US4 ↔ US5:** independent

Suggested MVP cutline: **Phase 1 + Phase 2 + Phase 3 (US1) only.** Phases 4–5 are also P1 and should land soon, but US1 alone is shippable as the immediate fix to the duplicate-dispatch class.

---

## Parallel execution opportunities

Within each phase, the `[P]` markers identify parallelisable tasks. Highlights:

**Phase 1:** T002, T003 are pure file edits across configs — fully parallel.

**Phase 2:** T013, T014, T018 are independent test files; T019–T022 are independent new module stubs. All `[P]`.

**Phase 3:** T031, T035, T036, T037, T043, T058, T059, T060, T061, T065 are all test files in different paths — parallel-eligible. T056 (daemon wiring) blocks on T050; T057 (check_board wiring) blocks on T055.

**Phase 4:** T070 + T072 + T076 + T077 are independent test files.

**Phase 5:** T081, T086, T087, T088, T093 all `[P]`. T085 (drain into partial_progress) chains on T084.

**Phase 6:** T105, T114 `[P]`. T103 (JobInitPayload) blocks T112's `dispatch` trigger wire-up.

**Phase 7:** T122, T125, T126, T127, T128 all `[P]`.

**Phase 8:** T140, T141 `[P]`; the rest are sequential cleanups.

---

## Implementation strategy

**MVP (deliverable in one PR if scope allows):** Phase 1 + Phase 2 + Phase 3 (US1).
- Closes the seed bug (duplicate dispatch on restart).
- Restart-safe: no orphan containers, no two-implementer races.
- Independently shippable; later phases land in follow-up PRs on the same branch.

**Incremental delivery:**
1. **PR #1 (MVP):** Phases 1–3. Tag: `076-mvp`. Validates SC-001 in live testing.
2. **PR #2:** Phases 4–5. Adds US2 (no-forgotten-success) + US3 (lifecycle correctness). Validates SC-008, SC-009, SC-010.
3. **PR #3:** Phases 6–7. Adds US4 (one-PR) + US5 (board sync). Validates SC-011, SC-012, SC-013.
4. **PR #4:** Phase 8 polish, perf benchmarks, docs updates, AGENTS.md footer.

This staging matches the project's "QA-cycle branches collect fixes only" convention (per memory `feedback_pr_scope_discipline.md`) — each PR is a discrete fix bundle on the same 076 branch.

---

## Task index summary

| Phase | Task IDs | Count | Story | Parallelisable count |
|---|---|---|---|---|
| 1 — Setup | T001–T005 | 5 | — | 2 (T002, T003) |
| 2 — Foundational | T010–T022 | 13 | — | 6 (T013, T014, T018, T019, T020, T021, T022) |
| 3 — US1 | T030–T066 | 29 | US1 | 11 |
| 4 — US2 | T070–T077 | 8 | US2 | 4 |
| 5 — US3 | T080–T095 | 15 | US3 | 7 |
| 6 — US4 | T100–T115 | 12 | US4 | 4 |
| 7 — US5 | T120–T128 | 7 | US5 | 5 |
| 8 — Polish | T140–T155 | 10 | — | 4 |
| 9 — Review hardening + live fixes | T160–T178 | 19 | R1–R6 + live | — |
| **Total** | | **118** | | **43 parallelisable** |

> Phase 9 was not in the original plan — it captures the fixes that
> emerged during PR #94 review rounds and the 2026-05-29 live tests.
> Two recurring lessons: (1) **contract tests must use the producer's
> exact output** — **T170**'s FR-019 idle-timeout counter never fired
> because every 076 unit test used a synthetic "idle timeout" reason
> that matched the wrong substring while the performer emits "idle for
> Ns". (2) **cold-path coverage** — the T172–T176 cascade only appeared
> on a cold env cache (empty-label deadlock, then a bootstrap-failure
> thrash from a fatal `service_inference` timeout), because every prior
> live run reused a warm cache that skipped bootstrap entirely. T175 is
> the standout: a best-effort telemetry probe's timeout was failing the
> whole bootstrap and re-dispatching it forever.
