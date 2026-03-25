# Tasks: Smart Health-Check Retry

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Add `HealthCheckConfig(BaseModel)` to `src/coordinare/config.py` with `retries: int = 3` and `backoff_seconds: float = 1.0`; add `health_check: HealthCheckConfig` to `ProjectConfiguration`

---

## Phase 2: US1 — Retry with Backoff (P1)

- [X] T002 [US1] Replace health check in `src/coordinare/graph/nodes/dispatch_performer.py` with retry loop: retry up to `config.health_check.retries` times for "unreachable"/"unknown"; exponential backoff; "error" not retried; CancelledError propagated
- [X] T003 [P] [US1] Write unit tests: retry succeeds on 3rd attempt → dispatch proceeds; all retries exhausted → idle; retries=1 → no retry; "error" on first attempt → blocked immediately

---

## Phase 3: Polish

- [X] T004 Run full test suite and linter
- [X] T005 Verify backward compatibility
