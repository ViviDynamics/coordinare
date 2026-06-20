# Tasks: ENV_BLOCKED Infrastructure/Environment CI-Failure Classification

**Feature**: `095-env-blocked-ci` | **Spec**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md)
**Approach**: TDD (Constitution II; research R1/R6 — the #159 repro test drives env_blocked-over-inherited priority).

## Conventions

- New service: `src/coordinare/services/env_signature.py`. Extends: `services/failure_classification.py`, `config.py`, `state_store.py`, `graph/nodes/monitor_performer.py`, `notify.py`.
- Tests: `tests/unit/services/` + `tests/unit/graph/nodes/`. Deterministic (no live GitHub).
- Run: `.venv/bin/pytest <paths> -v` · Lint: `.venv/bin/ruff check <files>`.
- **[P]** = parallelizable (different file, no incomplete dep). Tasks on the same file are sequential.

---

## Phase 1: Setup

- [X] T001 Confirm injection points: read `services/failure_classification.py` (`Classification` literal + `classify_failure_origin`), `services/failure_signature.py` (`normalize_reason`), `config.py` `PersonaScopeConfig` + the 090 gate classes (~1350-1431), `state_store.py` `PersistedSession` + schema-version constant, and `graph/nodes/monitor_performer.py` (`_get_baseline_classification_gate_config` ~981, the L3 repair-mandate build ~1042, dispatch gating). Note the exact spots ENV_BLOCKED short-circuits.
- [X] T002 [P] Create test module skeletons with shared fixtures: `tests/unit/services/test_env_signature.py`, `tests/unit/services/test_failure_classification_env.py`, `tests/unit/graph/nodes/test_monitor_performer_env_blocked.py` (reuse existing 090 classification fixtures for head/baseline failures).

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: The pure env-signature matcher + the #159 reproduction test that pins env_blocked priority — everything else builds on these.

- [X] T003 [P] Add `EnvSignaturePattern` and `EnvBlockedGateConfig` (pydantic, `extra="forbid"`) to `src/coordinare/config.py`: `EnvSignaturePattern{id, regex, cause, action}`; `EnvBlockedGateConfig{enabled: bool = False, patterns: list[EnvSignaturePattern] = []}`. Attach `env_blocked_gate: EnvBlockedGateConfig` to `PersonaScopeConfig` (mirror the 090 gates). Default-off (FR-012).
- [X] T004 Write FAILING tests in `tests/unit/services/test_env_signature.py`: artifact-quota reason ("artifact storage quota has been hit" / "createartifact … quota") → EnvCause(artifact_storage_quota); runner-offline + spending-limit reasons → matched; a normal test-failure reason → None (no false positive, SC-005). MUST fail before T005.
- [X] T005 Implement `src/coordinare/services/env_signature.py`: `EnvCause{pattern_id, cause, action}` + pure `match_env_signature(reason, patterns) -> EnvCause | None` with built-in defaults (artifact-storage-quota, runner-offline, billing-limit) plus operator `patterns`; first match wins; no match → None (FR-002/FR-003). Make T004 pass.

**Checkpoint**: matcher works; fail-safe (unrecognized → None) proven.

---

## Phase 3: User Story 1 — Hold an infra-blocked card instead of bouncing (P1)

**Goal**: Infra failures classify `env_blocked` (before flake/inherited) and the card is held — no repair, no re-dispatch.
**Independent test**: infra-signature required failure → `env_blocked`, no repair mandate, zero re-dispatch; non-infra → existing 090 logic unchanged.

- [X] T006 [US1] Write the FAILING #159 reproduction test in `tests/unit/services/test_failure_classification_env.py`: a stable "failure" head check that ALSO fails on the baseline AND whose reason matches the artifact-quota signature → classified `env_blocked`, NOT `inherited` (Row 0 priority, FR-001, SC-007). Also: non-infra failure → existing classification unchanged (FR-003); indeterminate baseline + infra reason → still `env_blocked` (FR-011). MUST fail before T007.
- [X] T007 [US1] Extend `src/coordinare/services/failure_classification.py`: add `"env_blocked"` to the `Classification` literal and a **Row 0** in `classify_failure_origin` — if `match_env_signature(head_reason, patterns)` returns a cause, return `env_blocked` before all existing rows; evaluated on the HEAD failure alone. Thread the resolved patterns in. Make T006 pass.
- [X] T008 [US1] Add `_get_env_blocked_gate_config(state)` to `src/coordinare/graph/nodes/monitor_performer.py` (parallel to `_get_baseline_classification_gate_config`), and gate the env classification on it (disabled → never `env_blocked`).
- [X] T009 [US1] Write FAILING test in `tests/unit/graph/nodes/test_monitor_performer_env_blocked.py`: an `env_blocked` card builds NO L3 repair mandate and triggers NO performer re-dispatch/bounce (held) — SC-001. Then wire the hold guard in `monitor_performer.py` ahead of the repair-mandate/dispatch path. Make the test pass.

**Checkpoint**: US1 independently testable — infra failures are held, not bounced; non-infra unaffected.

---

## Phase 4: User Story 2 — Surface the real cause, once (P1)

**Goal**: One operator notification naming cause + action; deduped; secret-free.
**Independent test**: env_blocked → one notification (cause+action), re-eval same condition → no re-notify.

- [X] T010 [US2] Add per-card `EnvBlockedState{pattern_id, cause, action, notified_at}` (optional, default None) to `PersistedSession` in `src/coordinare/state_store.py`; bump the schema version; confirm old snapshots load with None (backward-compatible, parallel to 090 counters).
- [X] T011 [US2] Write FAILING test in `tests/unit/graph/nodes/test_monitor_performer_env_blocked.py`: first env_blocked eval emits exactly one operator notification naming cause + action (distinct from generic "tests failed"), fields are ids/names/conclusions/reasons/cause/action only (no secrets, FR-010); a second eval of the same condition emits NO new notification (dedup on `notified_at`, FR-006). MUST fail before T012.
- [X] T012 [US2] Implement the env-blocked operator notification in `src/coordinare/notify.py` (distinct message type with cause + action), set `env_blocked.notified_at` on send, and gate sending on it for dedup. Wire it from the `monitor_performer.py` hold guard. Make T011 pass.

**Checkpoint**: US2 independently testable — operator gets one actionable, secret-free signal per condition.

---

## Phase 5: User Story 3 — Auto-resume when the condition clears (P2)

**Goal**: When the infra signature is gone on a later eval, clear the hold and resume normal flow.
**Independent test**: env_blocked card → next eval no infra match → hold cleared, normal classify/dispatch resumes within one cycle.

- [X] T013 [US3] Write FAILING test in `tests/unit/graph/nodes/test_monitor_performer_env_blocked.py`: an `env_blocked` card whose next evaluation yields no infra-signature match → `env_blocked` state cleared to None, normal flow resumes (classify/dispatch), no manual reset (FR-008, SC-004). MUST fail before T014.
- [X] T014 [US3] Implement auto-clear in `src/coordinare/graph/nodes/monitor_performer.py`: when re-evaluating a card that has `env_blocked` set but the current failures match no infra signature, clear `env_blocked` to None and fall through to normal classification/dispatch. Make T013 pass.

**Checkpoint**: US3 independently testable — the hold is self-clearing.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T015 [P] Write `test_env_block_does_not_mask_introduced` in `tests/unit/services/test_failure_classification_env.py`: env_blocked on check A + introduced failure on check B → A held, B still classified INTRODUCED and surfaced (FR-009). Implement in `failure_classification.py`/wiring if needed to pass.
- [X] T016 [P] Write `test_disabled_gate_identical_to_baseline` in `tests/unit/graph/nodes/test_monitor_performer_env_blocked.py`: gate disabled → no `env_blocked` ever, classification/routing/notifications byte-identical to pre-feature (FR-012, SC-006).
- [X] T017 [P] Edge tests in `tests/unit/services/test_env_signature.py` / classification: infra signature on a NON-required check → no hold; flapping infra (clear then recur) → resume then re-block + re-notify-once; baseline indeterminate + infra reason → env_blocked (head-only).
- [X] T018 Document `env_blocked_gate` in `config.example.yaml` (under `persona_scope`, mirroring the 090 gate examples): `enabled` + built-in patterns note + a custom-pattern example.
- [X] T019 Run the full 090 classification + ci_gate + monitor_performer + snapshot-persistence regression suites together to confirm no cross-phase regression; `.venv/bin/ruff check` all edited files.
- [X] T020 Walk quickstart.md US1/US2/US3 + edge scenarios; confirm SC-001..SC-008 each have a covering test, observability/notifications carry no secret values (FR-010, SC-002), and `state_store.py` schema bump round-trips old snapshots. Verify no new external dependency was added.

---

## Dependencies & Execution Order

- **Setup (T001-T002)** → **Foundational (T003-T005)** → **US1 (T006-T009)** → **US2 (T010-T012)** → **US3 (T013-T014)** → **Polish (T015-T020)**.
- T004 → T005 (matcher: red→green). T006 → T007 (classification: red→green). T009, T011, T013 are red→green within their wiring tasks.
- T005 (matcher) blocks T007 (classification uses it). T007 blocks T008/T009 (wiring uses env classification). T010 (state) blocks T011/T012 (notification dedup) and T014 (clear).
- Same-file serialization: `failure_classification.py` (T007, T015), `monitor_performer.py` (T008, T009, T012-wire, T014), `config.py` (T003), `state_store.py` (T010), `notify.py` (T012) are sequential within each file.

## Parallel Opportunities

- T002 (test skeletons), T003 (config — separate file) can run alongside T001.
- T015, T016, T017 (separate test files / independent assertions) are [P].
- T018 (config.example.yaml) [P] alongside T019/T020.

## Implementation Strategy (MVP first)

- **MVP = US1 (T001-T009)**: infra failures are held instead of bounced (the #159 waste). Independently shippable.
- **US2 (T010-T012)** makes the hold actionable (operator signal) — bundle with US1 so a held card isn't silent.
- **US3 (T013-T014)** + Polish close the loop (auto-resume) and harden edges.
