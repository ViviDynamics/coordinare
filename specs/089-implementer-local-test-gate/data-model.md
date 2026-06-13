# Phase 1 Data Model: Implementer Local Test Gate

## Entities

### `LocalTestResult`

Outcome of one local test run, returned by the `_run_test_check` performer helper. Plain dataclass / pydantic model local to `performer.main` (not persisted).

| Field | Type | Notes |
|-------|------|-------|
| `passed` | `bool` | True if tests ran green OR were skipped (no `test_command` detected / standalone fallback). |
| `output` | `str` | Combined stderr+stdout tail of the run; empty when passed/skipped. Truncated before it reaches a PerformerResponse comment (≤500 chars, matching the lint gate). |
| `env_blocked` | `bool` | True when a failure coincided with an env-cache signal (`consume_services_start_failure()` or `consume_env_cache_health_failure()`). |
| `env_reason` | `str \| None` | Human-readable env-cache failure description when `env_blocked`; else None. Sourced from `consume_services_start_failure()` text or a fixed "env-cache health check failed" string. |
| `command` | `str \| None` | The detected `test_command`, or None when none detected (skip / pass-through). |
| `duration_seconds` | `float` | Wall-clock of the test run; 0.0 when skipped. |

**Derived branch selection** (in the done-path):
- `passed` → proceed to push (existing path, unchanged).
- `not passed and env_blocked` → return `status="env_blocked"`, `reason=env_reason`. No attempt consumed.
- `not passed and not env_blocked` → return `status="changes_requested"`, `comments=[{body: output}]`, `local_test_failed=True`. Coordinare increments the counter.

---

### `local_fix_counter` (coordinare session state)

Per-head-SHA integer tracking coordinare-observed local-test self-fix re-dispatches, **separate from** spec-075's `bounce_counter`.

| Location | Field | Type | Default |
|----------|-------|------|---------|
| `PersistedSession` (`state_store.py`) | `local_fix_counter` | `dict[str, int]` | `{}` (v6 schema bump; v1–v5 snapshots load with `{}`) |
| live session dict (`session.py`) | `local_fix_counter` | `dict[str, int]` | `{}` |
| `daemon.py` hydrate/persist | — | mirrors `bounce_counter` handling (lines ~133, ~637) | — |

**Transitions** (in `monitor_performer.py`, on the implementer `changes_requested` path when `status.local_test_failed` is set):
1. `count = local_fix_counter.get(head_sha, 0) + 1`; write back.
2. `count <= max_fix_attempts` → re-dispatch (existing changes_requested behaviour). 
3. `count > max_fix_attempts` → route card to **blocked**, reason = failing test output; do NOT push, do NOT re-dispatch.

A new head SHA (agent committed a fix) starts a fresh count — matches spec-075 per-head semantics.

---

### `LocalTestGateConfig` (config)

Symphony/persona-scoped opt-in, sibling of `CIGateConfig` in `config.py`. `pydantic.BaseModel`, `model_config = ConfigDict(extra="forbid")`.

| Field | Type | Default | Bounds |
|-------|------|---------|--------|
| `enabled` | `bool` | `False` | — |
| `timeout_seconds` | `int` | `600` | `ge=60, le=7200` |
| `max_fix_attempts` | `int` | `2` | `ge=0, le=20` |

Default `enabled=False` → gate dormant → behaviour byte-identical to pre-089 (SC-005).

---

### `PerformerResponse` additions (`protocol.py`)

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `local_test_failed` | `bool` | `False` | Marks a `changes_requested` response as originating from the local test gate, so the coordinare counts it against `local_fix_counter` (not generic reviewer feedback). |
| (`reason` reused) | `str \| None` | existing | Carries the env-cache reason on an `env_blocked` response (same field `qa_env_blocked` uses). |

New terminal status literal: `"env_blocked"` added to the terminal-status sets in `protocol.py` and the matching `_terminal_markers` / break-conditions in `main.py` and `monitor_performer.py`.

---

## State / status vocabulary delta

| Name | Kind | Added where | Routed like |
|------|------|-------------|-------------|
| `env_blocked` | terminal performer status | `protocol.py`, `main.py`, `monitor_performer.py` | bails to the blocked column (`phase="blocked"` with env-cache reason in `system_error_reason`/`open_questions`); still calls `mark_runtime_health_failed` to invalidate the cache; consumes 0 self-fix attempts (per spec FR-005/SC-003) |
| `local_test_failed` | response flag | `protocol.py` `PerformerResponse` | gates `local_fix_counter` increment |
| `local_fix_counter` | persisted session field | `state_store.py` / `session.py` / `daemon.py` | `bounce_counter` (parallel, independent) |
| `LocalTestGateConfig` | config model | `config.py` | `CIGateConfig` (sibling) |
