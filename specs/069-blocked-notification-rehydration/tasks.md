---

description: "Task list for spec 069 — Block-Notification Dedup on State Rehydration"
---

# Tasks: Block-Notification Dedup on State Rehydration

**Input**: Design documents from `/specs/069-blocked-notification-rehydration/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Tests are explicitly required by the spec (User Story Independent Test sections + Affected Files list) and by Constitution Principle II. All test tasks below are mandatory.

**Organization**: Tasks are grouped by user story. The three stories share `notify.py` so cross-story file ordering is documented in Dependencies.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- File paths are absolute-from-repo-root.

## Path Conventions

Single-project layout. Source under `src/coordinare/`; tests under `tests/unit/`.

---

## Phase 1: Setup (Shared Infrastructure)

No new dependencies, modules, or scaffolding. Phase intentionally empty — confirm working tree is on branch `069-blocked-notification-rehydration` and `.venv/bin/pytest` runs green on `tests/unit/graph/nodes/test_notify.py` and `tests/unit/graph/nodes/test_handle_blocked.py` before starting Phase 2.

- [X] T001 Verify branch `069-blocked-notification-rehydration` checked out and baseline `.venv/bin/pytest tests/unit/graph/nodes/test_notify.py tests/unit/graph/nodes/test_handle_blocked.py tests/unit/test_state_store.py` passes pre-change.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Snapshot-schema change that all three user stories rely on. MUST complete before any story-specific work.

**⚠️ CRITICAL**: User Story 1 reads the rehydrated session-level `last_blocked_notified_at`; without T002–T004 the field never round-trips.

- [X] T002 Add `last_blocked_notified_at: datetime | None = None` to `PersistedSession` in `src/coordinare/state_store.py` (per data-model.md §1). Ensure pydantic v2 serialization handles datetime identically to `WorkflowSnapshot.last_blocked_notified_at`.
- [X] T003 In `src/coordinare/state_store.py` `persist()` path, copy `session.get("last_blocked_notified_at")` from each live session into the corresponding `PersistedSession` entry.
- [X] T004 In `src/coordinare/daemon.py` rehydration path (around the existing lines ~463–510 that build `session_dict` and the v1-snapshot synthesis), copy `PersistedSession.last_blocked_notified_at` into the reconstructed `active_sessions[card_id]["last_blocked_notified_at"]`. For v1 snapshots (field absent), leave as `None`.
- [X] T005 [P] Add `tests/unit/test_state_store.py::test_persisted_session_roundtrips_last_blocked_notified_at` — round-trip a snapshot with a known timestamp; assert equality after `dump → load`.
- [X] T006 [P] Add `tests/unit/test_state_store.py::test_v1_snapshot_defaults_last_blocked_notified_at_to_none` — deserialize a v1 snapshot dict lacking the field; assert `None`.
- [X] T007 [P] Add `tests/unit/test_daemon_rehydration.py::test_rehydrate_copies_session_last_blocked_notified_at` (new file, or extend an existing daemon test if one exists) — start daemon from a persisted snapshot where `PersistedSession.last_blocked_notified_at = T`; assert `daemon._state["active_sessions"][card_id]["last_blocked_notified_at"] == T`.

**Checkpoint**: Snapshot now carries the per-card watermark across restart. User story phases may begin.

---

## Phase 3: User Story 1 — Restart with a real blocked card does not re-spam (Priority: P1) 🎯 MVP

**Goal**: After a rehydration where session-level `last_blocked_notified_at` is within `blocked_reminder_hours`, neither Slack nor GitHub reminder fires.

**Independent Test**: Seed `coordinare.state.json` with `phase=blocked`, populated `open_questions`, `last_blocked_notified_at = now - 1h`. Restart coordinare. Assert zero Slack `card_blocked` dispatches; zero new GitHub comments on card X's issue.

### Tests for User Story 1

- [X] T008 [P] [US1] Add `tests/unit/graph/nodes/test_handle_blocked.py::test_does_not_repost_when_session_watermark_within_window` — session-level `last_blocked_notified_at = now - 1h`, top-level `None`, `blocked_reminder_hours=24`. Assert no GitHub-comment side effect.
- [X] T009 [P] [US1] Add `tests/unit/graph/nodes/test_handle_blocked.py::test_reposts_when_session_watermark_older_than_window` — session-level `now - 25h`, `blocked_reminder_hours=24`. Assert reminder repost occurs (preserves existing legitimate cadence).
- [X] T010 [P] [US1] Add `tests/unit/graph/nodes/test_notify.py::test_suppresses_card_blocked_on_first_tick_after_restart_when_watermark_present` — session-level watermark non-null, dedup cache empty, populated `open_questions`. Assert `notification_service.dispatch` not called with `card_blocked`; assert dedup cache primed for the would-be key.

### Implementation for User Story 1

- [X] T011 [US1] In `src/coordinare/graph/nodes/handle_blocked.py` (current lines ~142, ~157–159), change the watermark lookup to read session-level first: `last = state.get("active_sessions", {}).get(card_id, {}).get("last_blocked_notified_at") or state.get("last_blocked_notified_at")`. Preserve `last is None` semantics only when both are `None`.
- [X] T012 [US1] In `src/coordinare/graph/nodes/handle_blocked.py` line ~207, write the advanced timestamp to **both** the session dict (`state["active_sessions"][card_id]["last_blocked_notified_at"] = now`) and the top-level mirror (existing write retained for backward-compat consumers). Add an inline `# WHY:` comment only if the dual-write would surprise a reader.
- [X] T013 [US1] In `src/coordinare/graph/nodes/notify.py`, before emitting `card_blocked`, add the FR-004 restart-suppression branch: if session-level `last_blocked_notified_at` is non-null AND the dedup cache has no entry for any key starting with `f"{EventType.card_blocked.value}:{card_id}:"`, skip dispatch and prime the cache with the to-be-computed key.

**Checkpoint**: US1 passes — a restart with a real, recent blocked watermark produces no spurious Slack or GitHub side effects.

---

## Phase 4: User Story 2 — Restart while a fresh performer is running suppresses stale blocked (Priority: P1)

**Goal**: When `active_sessions[card_id].phase` is `dispatching` / `monitoring_performer` / `monitoring_agent`, `notify` suppresses `card_blocked` for that card regardless of stale top-level `phase`.

**Independent Test**: Reproduce 2026-05-22 12:02. State has top-level `phase=blocked` from rehydration; `active_sessions[card_id].phase="monitoring_performer"`. Call `notify`. Assert no `card_blocked` dispatch; one `card_dispatched` event allowed if the harness drives that node.

### Tests for User Story 2

- [X] T014 [P] [US2] Add `tests/unit/graph/nodes/test_notify.py::test_suppresses_card_blocked_when_active_session_dispatching` — `phase=blocked` top-level, `active_sessions[card_id].phase="dispatching"`, populated `open_questions`. Assert no `card_blocked` dispatch.
- [X] T015 [P] [US2] Add `tests/unit/graph/nodes/test_notify.py::test_suppresses_card_blocked_when_active_session_monitoring_performer` — same, with `phase="monitoring_performer"`.
- [X] T016 [P] [US2] Add `tests/unit/graph/nodes/test_notify.py::test_suppresses_card_blocked_when_active_session_monitoring_agent` — same, with `phase="monitoring_agent"`.
- [X] T017 [P] [US2] Add `tests/unit/graph/nodes/test_notify.py::test_emits_card_blocked_when_active_session_phase_is_blocked` — guard against over-suppression: `active_sessions[card_id].phase="blocked"` and populated questions → dispatch IS called (legitimate path).
- [X] T018 [P] [US2] Add `tests/unit/graph/nodes/test_notify.py::test_replay_card70_restart_does_not_emit_card_blocked` — quickstart §2 replay; reconstruct the 2026-05-22 12:02 state and assert zero `card_blocked` dispatches.

### Implementation for User Story 2

- [X] T019 [US2] In `src/coordinare/graph/nodes/notify.py`, add the FR-005 active-session check immediately above the dedup-key construction for `card_blocked`: skip when `active_sessions.get(card_id, {}).get("phase") in {"dispatching", "monitoring_performer", "monitoring_agent"}`. See data-model.md §4 for the authoritative phase table.

**Checkpoint**: US2 passes — the exact 2026-05-22 incident does not reproduce.

---

## Phase 5: User Story 3 — Empty open_questions never produces a "needs input" Slack post (Priority: P2)

**Goal**: When `phase=blocked` and `open_questions == []`, `notify` skips dispatch entirely. The `"needs input"` fallback string is removed.

**Independent Test**: Unit-test `notify.py` directly with `phase=blocked`, `open_questions=[]`, populated card. Assert `notification_service.dispatch` not called.

### Tests for User Story 3

- [X] T020 [P] [US3] Add `tests/unit/graph/nodes/test_notify.py::test_suppresses_card_blocked_when_open_questions_empty` — empty list, no active-session suppression in play. Assert no dispatch.
- [X] T021 [P] [US3] Add `tests/unit/graph/nodes/test_notify.py::test_dedup_key_includes_open_questions_hash` — two emissions with different `open_questions` content produce different dedup keys (and both dispatch); two emissions with identical content collapse to one.
- [X] T022 [P] [US3] Add `tests/unit/graph/nodes/test_notify.py::test_no_path_emits_card_blocked_with_needs_input_summary` — grep-equivalent assertion: across all `dispatch` calls made under `phase=blocked`, no call's payload summary equals the literal `"needs input"`.

### Implementation for User Story 3

- [X] T023 [US3] In `src/coordinare/graph/nodes/notify.py` (current line ~91), remove the `else "needs input"` branch and replace with an early-return that skips `card_blocked` emission when `open_questions` is empty (FR-003).
- [X] T024 [US3] In `src/coordinare/graph/nodes/notify.py` (current line ~133), extend `dedup_key` to include a 12-hex-char SHA-256 prefix of `"\n".join(open_questions)` when `event_type == EventType.card_blocked` (FR-006). For other event types, key shape unchanged. Add `import hashlib` if not already present.

**Checkpoint**: US3 passes — operators no longer see content-free "needs input" Slack posts, and question-content changes produce new notifications.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T025 [P] Run `.venv/bin/ruff check src/coordinare/graph/nodes/notify.py src/coordinare/graph/nodes/handle_blocked.py src/coordinare/state_store.py src/coordinare/daemon.py` — zero warnings.
- [X] T026 [P] Run full `.venv/bin/pytest tests/unit/` — confirm no regressions in unrelated suites.
- [X] T027 Run quickstart.md §1 invocation; verify all seven listed tests pass.
- [X] T028 Confirm CLAUDE.md "Recent Changes" entry for 069 is present (added by `update-agent-context.sh` during planning); verify wording reflects the final implementation.
- [X] T029 Manual replay of `specs/069-blocked-notification-rehydration/quickstart.md` §2 — confirm replay test reproduces (and fixes) the 2026-05-22 incident.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (T001)**: No dependencies — baseline check.
- **Phase 2 (T002–T007)**: Depends on Phase 1. T002 → T003 → T004 are sequential (same files, additive edits). T005/T006/T007 are parallel after T002–T004 land.
- **Phase 3 (US1)**: Depends on Phase 2. Story-internal: tests (T008/T009/T010) parallel-write but T010 shares `test_notify.py` with US2/US3 tests — see "Same-file note" below.
- **Phase 4 (US2)**: Depends on Phase 2 only; **independent of US1**. Shares `notify.py` with US1/US3 — see "Same-file note".
- **Phase 5 (US3)**: Depends on Phase 2 only; **independent of US1 and US2**. Shares `notify.py` with US1/US2 — see "Same-file note".
- **Phase 6 (Polish)**: Depends on all desired user stories completing.

### Same-file note (notify.py and test_notify.py)

All three user stories edit `src/coordinare/graph/nodes/notify.py` and add cases to `tests/unit/graph/nodes/test_notify.py`. The three implementation tasks (T013, T019, T023+T024) target *different code regions* (restart-suppression branch, active-session check, empty-questions guard + dedup key) and are mergeable in any order, but the implementer should rebase between them and re-run the full notify test module each time. New test cases append to the test file and are parallel-writable as long as test names don't collide (they don't — names are unique).

### User Story Dependencies

- **US1 (P1)**: Independent of US2 and US3. Delivers FR-001/FR-002/FR-004.
- **US2 (P1)**: Independent of US1 and US3. Delivers FR-005.
- **US3 (P2)**: Independent of US1 and US2. Delivers FR-003 and FR-006.

### Within Each User Story

- Tests written first; verify they FAIL pre-implementation.
- Implementation lands; tests now PASS.
- Run full module before moving to next story.

### Parallel Opportunities

- T005, T006, T007 — three different test files, all run after T002–T004.
- All tests within a story (T008/T009/T010; T014–T018; T020/T021/T022) are parallel-safe (appended to test files, unique names).
- US1, US2, US3 implementation phases can be staffed in parallel by three developers, each owning one notify.py region and the matching tests.

---

## Parallel Example: Phase 2 Foundational Tests

```bash
# After T002–T004 land:
Task: "T005 round-trip test in tests/unit/test_state_store.py"
Task: "T006 v1-default test in tests/unit/test_state_store.py"
Task: "T007 daemon rehydration test in tests/unit/test_daemon_rehydration.py"
```

## Parallel Example: User Story 2 Tests

```bash
# All five US2 tests append unique cases to tests/unit/graph/nodes/test_notify.py:
Task: "T014 active-session dispatching suppression"
Task: "T015 active-session monitoring_performer suppression"
Task: "T016 active-session monitoring_agent suppression"
Task: "T017 active-session phase=blocked does NOT suppress"
Task: "T018 card #70 replay"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 (T001).
2. Phase 2 (T002–T007) — snapshot now carries the watermark.
3. Phase 3 / US1 (T008–T013) — restart no longer re-spams a legitimately-blocked card.
4. **STOP and VALIDATE**: run quickstart §1, confirm US1 tests green.

This MVP alone closes the operator-facing channel for the most common variant of the 2026-05-22 incident. Ship if needed.

### Incremental Delivery

1. MVP (US1) → Deploy.
2. Add US2 (T014–T019) → fresh-dispatch supersession → Deploy.
3. Add US3 (T020–T024) → no more "needs input" generic posts + content-hash dedup → Deploy.
4. Polish (T025–T029) → ruff/full-suite/quickstart replay.

### Parallel Team Strategy

After Phase 2 lands, three developers can take US1, US2, US3 in parallel. Coordinate via a shared rebase cadence on `notify.py` (three small, non-overlapping regions).

---

## Notes

- [P] tasks = different files OR different unique-named test cases in the same file.
- Tests for this feature are MANDATORY (not optional) — Constitution II + spec-listed Independent Tests.
- No commits should batch unrelated stories; one story per logical commit per the project's PR-scope discipline.
- After each phase, re-run the focused test command from quickstart.md §1 before proceeding.

---

## Phase 3 — Coordinare LLM configurability (2026-05-22)

Removes the two hardcoded Anthropic call sites in the coordinare process so every
LLM call honors `config.conducting.backend`. Performer-side adapters
(`claude_code`, `coordinare_service_inference`) remain out of scope.

- [X] T030 Generalize `ClaudeScorer` → `BackendScorer` in
  `src/coordinare/services/scoring.py`; route through
  `ConductingBackend.prompt(text, response_format="json")`; keep
  `ClaudeScorer = BackendScorer` alias.
- [X] T031 In `src/coordinare/__main__.py`, delete the orphan `ClaudeService`
  build + import, drop `"claude_service"` from the state dict, wire
  `BackendScorer(conducting_backend, provider_name=config.conducting.backend)`.
- [X] T032 Remove `ClaudeServiceProtocol` and `claude_service` field from
  `src/coordinare/graph/state.py` (dead — set but never read).
- [X] T033 Rewrite `tests/unit/services/test_scoring.py` and
  `tests/unit/test_scoring_coverage.py` against the `ConductingBackend.prompt`
  interface; drop the `_Claude` fixture + `claude_service` state key from
  `tests/integration/test_graph_execution.py`.
- [X] T034 Run `.venv/bin/ruff check` and `.venv/bin/pytest`; confirm full
  suite green.
- [X] T035 Make the `claude_code` performer adapter's `base_url`/`api_key`
  config-driven instead of relying on container env passthrough. Added
  `base_url` + `api_key_env` to `PerformerRoleConfig`; `dispatch_performer`
  plumbs them into `card_context`; `HTTPPerformerService` reads the api key
  from the env var named by `api_key_env` (defaults preserved) and injects
  `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` into the per-job `secrets` so the
  performer subprocess hits a proxy (e.g. LiteLLM) instead of the vendor
  endpoint. Tests added in `tests/unit/test_config.py`,
  `tests/unit/services/test_http_performer_service.py`, and
  `tests/unit/graph/nodes/test_dispatch_performer.py`.

---

## Phase 4 — check_board lifecycle hardening (2026-05-22)

Drive-by fixes uncovered while exercising 069 against the hermes backend.
See plan.md §"Phase 4" for motivation. All landed; documented here so
tasks.md is a faithful merge-record.

- [X] T036 `fix(check_board): keep phase=blocked for blocked cards to release slot` (commit `04713d5`) — preserve `phase=blocked` so slot accounting releases the concurrency slot; add regression test in `tests/unit/graph/nodes/test_check_board.py`.
- [X] T037 `fix(check_board): preserve in-flight working phase when TODO is empty` (commit `ed35a8e`) — do not clobber `monitoring_performer` / `monitoring_agent` with `idle` on an empty TODO column.
- [X] T038 `fix(check_board): re-dispatch readopted IN_PROGRESS cards instead of monitoring` (commit `356ea5a`) — readopted cards have no live performer; route to `dispatching`.
- [X] T039 `fix(handle_system_error): keep phase=system_error during wait so retry fires` (commit `56f713b`) — drop the `phase=blocked` transition during the retry wait window.
- [X] T040 `fix(check_board): fall back to session watermark for blocked-comment detection` (commit `4b3244c`) — read session-level `last_blocked_notified_at` before falling back to top-level (consistent with the US1 ordering established by T011).

## Phase 5 — LLM & env resilience (2026-05-22)

See plan.md §"Phase 5" for motivation.

- [X] T041 `fix(performer): treat empty inference env vars as unset; stamp agent_version` (commit `ae55aab`).
- [X] T042 `fix(env_bootstrap): drop literal ${VAR} placeholders before docker -e` (commit `9eb8a2a`).
- [X] T043 `fix(performer_endpoint): coerce env values to strings before validation` (commit `f12c16e`).
- [X] T044 `069: retry transient errors in OpenAiApiBackend and BackendScorer` (commit `4aae480`).
- [X] T045 `fix(service-inference): stream Anthropic responses to bypass 10-min timeout` (commit `10d5478`).
- [X] T046 `069: json-encode tool_result.content for Anthropic schema compatibility` (commit `095a9d6`).
- [X] T047 `fix(inference): unwrap Qwen/LiteLLM arg envelope; env-tune tool-call budget` (commit `893f7a9`) — also exposes `COORDINARE_INFERENCE_MAX_TOOL_CALLS`.
- [X] T048 `fix(service_inference): ship shell templates inside the package` (commit `d720d41`).
- [X] T049 `fix(hermes): surface full stdout as output so JSON roles can re-extract` (commit `8494214`).
- [X] T050 `docs(config): document COORDINARE_INFERENCE_* placeholders in every example` (commit `18d2373`).

## Phase 6 — Implementer feedback & handle_blocked enrichment (2026-05-22)

See plan.md §"Phase 6" for motivation.

- [X] T051 `fix(handle_blocked): persist generated questions to state so notify sees them` (commit `999e0a2`) — closes the empty-`open_questions` loophole the US3 dedup-key change exposed.
- [X] T052 `069: split Slack-delivery watermark from handle_blocked watermark` (commit `53399a1`) — two separate timestamps; Slack delivery no longer suppresses the GitHub reminder cadence.
- [X] T053 `069: mirror flat phase onto session in single-cycle ainvoke paths` (commit `2c308f5`) — closes a rehydration-test edge case where session phase lagged the top-level phase.
- [X] T054 `fix(implementer): require CI verification before return; enrich bounce body` (commit `f9f3a59`) — enforces spec 043's CI-ownership directive at the performer terminal boundary; bounce body now names the failing check.
- [X] T055 `feat(issue_comments): classify via conducting LLM with keyword fallback` (commit `a19e5bb`) — blocker-vs-info classification routes through `config.conducting.backend`; keyword path kept as a deterministic fallback.

## Phase 7 — Reminder-cooldown gate for card_blocked (2026-05-24)

**Goal**: Real fix for the Slack-spam regression where per-cycle `notify()`
calls were leaking through the per-channel `DeduplicationWindow` (600s for
slack-ops) every ~10 minutes for hours on end. The original FR-004 guard only
fired on the very first tick post-restart (gated on
`already_emitted_this_process` against `NotificationHistory`); once any
`card_blocked` landed in the in-process history, every subsequent cycle
bypassed the watermark check and relied solely on per-channel dedup.

The fix promotes `last_blocked_slack_delivered_at` to the *primary* gate via
an elapsed-time cooldown, so the watermark — which already survives restarts
— also throttles per-cycle re-emission within a single process.

- [X] T056 Add `NotificationsConfig.card_blocked_reminder_cooldown_seconds: int = 3600` (FR-004 cooldown; `0` disables).
- [X] T057 Plumb the cooldown through `NotificationService` as a read-only property so `notify()` can read it via `state["notification_service"]`.
- [X] T058 Replace FR-004 guard in `notify.py` with an elapsed-time check: suppress `card_blocked` when `(now - last_blocked_slack_delivered_at) < cooldown_seconds`. Log `notify.card_blocked_suppressed_reminder_cooldown` with elapsed/cooldown for observability.
- [X] T059 Update `FakeNotificationService` to expose the cooldown (default 3600); refresh existing FR-004 test to use a 5-minute watermark age; add positive (cooldown elapsed → re-emit + re-stamp) and disabled (`cooldown=0`) tests.
