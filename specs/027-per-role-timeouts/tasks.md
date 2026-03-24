# Tasks: Per-Role Timeouts

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Verify `PerformerRoleConfig.timeout_seconds` already exists in `src/coordinare/config.py` — it does (added in 019); no config change needed
- [X] T002 Add `role_timeouts: dict[str, int]` to `CoordinareState` in `src/coordinare/graph/state.py` — mapping stage name → timeout seconds; populated at bootstrap

---

## Phase 2: US1 — Per-Role Timeout Enforcement (P1)

- [X] T003 [US1] In `src/coordinare/__main__.py` `_bootstrap_services`: build `role_timeouts` mapping from configured performers (read each role's `timeout_seconds`, fall back to global `transport_timeout_seconds`); store in `service_state["role_timeouts"]`
- [X] T004 [US1] In `src/coordinare/graph/nodes/monitor_performer.py`: after resolving service, check elapsed time since `agent_dispatch_at`; if `role_timeouts[stage]` is exceeded, set `phase = "blocked"` with timeout message
- [X] T005 [P] [US1] Write unit tests: role with custom timeout exceeded → blocked; role with default timeout not exceeded → continues; no role_timeouts → no enforcement

---

## Phase 3: Polish

- [X] T006 Run full test suite and linter
- [X] T007 Verify backward compatibility
