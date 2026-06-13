# Implementation Plan: Implementer Local Test Gate

**Branch**: `089-implementer-local-test-gate` | **Date**: 2026-06-13 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/089-implementer-local-test-gate/spec.md`

## Summary

The implementer pushes code it has never executed: the pre-push gate (`_run_ci_check` in `agent/performer/src/performer/main.py`) runs only the detected *lint* command and ignores the detected *test* command. The first real execution is GitHub CI — an expensive round-trip for failures a local `pytest`/`rspec` would surface in seconds, and a violation of spec-043 (verify CI before handoff).

This feature adds a **local test-execution gate** for code-writing roles, inserted in the implementer done-path *after* lint passes and *before* push. It reuses spec-088's env-cache failure signals (`consume_services_start_failure()` / `consume_env_cache_health_failure()`) so a broken env-cache is classified **env-blocked** (routed to a same-stage hold, like `qa_env_blocked`) rather than misattributed as a code defect. A code-reason failure becomes `changes_requested` carrying the failing output, and a coordinare-side per-head self-fix counter (separate from spec-075's `bounce_counter`) bounds the re-dispatch loop before escalating to blocked. The gate is opt-in (default disabled → byte-identical to today) and degrades gracefully to pass-through when the coordinare package is unavailable (standalone performer). It is a cheap pre-filter *in front of* the spec-075 remote CI gate, not a replacement.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config + response models), structlog (observability), the existing `ci_detection.detect()` service, the existing `run_command` performer helper, langgraph (coordinare graph nodes). No new external dependencies.
**Storage**: JSON snapshot via `state_store.py` (existing) — extended with a per-head `local_fix_counter` on `PersistedSession`, parallel to spec-075's `bounce_counter`. No new store.
**Testing**: pytest (`.venv/bin/pytest`); ruff (`.venv/bin/ruff check`). Unit tests in `agent/performer/tests/unit/` (performer helper + done-path) and `tests/unit/` (coordinare routing/counter).
**Target Platform**: Linux performer container (env-cache active) + coordinare host process.
**Project Type**: single (coordinare package + performer subpackage in one repo).
**Performance Goals**: The gate adds at most one local test run per implementer turn. Test timeout configurable, default 600s (generous so slow suites are not mistaken for hangs). No new hot path.
**Constraints**: Disabled by default → zero behavioural change for legacy symphonies. Must not push known-red code. Env-blocked must consume 0 self-fix attempts.
**Scale/Scope**: One reusable performer helper + one done-path insertion + one new coordinare status route (mirroring `qa_env_blocked`) + one per-head counter + one opt-in config model. Touches `agent/performer/src/performer/main.py`, `agent/performer/src/performer/protocol.py`, `src/coordinare/graph/nodes/monitor_performer.py`, `src/coordinare/config.py`, `src/coordinare/state_store.py`, `src/coordinare/session.py`, `src/coordinare/daemon.py`, and role-contract prompt text.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. The test runner is factored into a single-purpose helper (`_run_test_check`) mirroring `_run_ci_check`; no clever indirection. Reuses existing `detect()`/`run_command`. Type annotations on all new public surfaces (`LocalTestResult`, config model). No dead code: the gate is wired end-to-end or not added.
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Unit tests cover the helper (pass / code-fail / env-blocked / no-test-cmd / timeout / standalone-fallback), the done-path branch selection, the coordinare env-blocked route, and the bounded counter + escalation. Deterministic: env signals and `run_command` are mocked; no real subprocess in unit tests. Disabled-by-default path asserted byte-identical (SC-005). Coverage must not regress.
- **III. User Experience Consistency** — PASS (limited surface). The operator-facing signal is the blocked-column reason text and structured logs; error output is actionable (the failing test tail). No new UI.
- **IV. Performance by Design** — PASS with documented budget. Budget: one local test run per turn, configurable timeout default 600s; disabled-by-default means no cost for symphonies that don't opt in. SC measures classification correctness and "0 red pushes", not latency; the latency budget is the timeout ceiling. No automated benchmark needed (no tracked perf-critical path changes).
- **V. Clarity Before Action** — PASS. The one genuine ambiguity (how the env-blocked outcome is signalled from a *pre-push* failure, since `changes_requested` enters the fix cycle) is resolved explicitly in research.md (new role-agnostic `env_blocked` terminal status, routed like `qa_env_blocked`). No `NEEDS CLARIFICATION` remain.

**Result**: PASS. No violations; Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/089-implementer-local-test-gate/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   ├── local_test_gate_helper.md
│   ├── env_blocked_status.md
│   └── local_test_gate_config.md
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
agent/performer/src/performer/
├── main.py              # ADD _run_test_check helper (mirrors _run_ci_check, ~line 100);
│                        #   insert local-test gate in implementer done-path after the
│                        #   line 2806 lint check, before the line 2818 push
└── protocol.py          # ADD "env_blocked" to the terminal-status literal/sets

src/coordinare/
├── config.py            # ADD LocalTestGateConfig (sibling of CIGateConfig, ~line 1319)
├── graph/nodes/
│   └── monitor_performer.py   # ADD env_blocked route (mirror qa_env_blocked ~line 2076);
│                              #   ADD local_fix_counter increment + escalate-to-blocked
│                              #   on the implementer changes_requested path
├── state_store.py       # ADD local_fix_counter to PersistedSession (v6 bump, parallel to bounce_counter)
├── session.py           # ADD local_fix_counter to the live session dict
└── daemon.py            # Hydrate/persist local_fix_counter (mirror bounce_counter at ~line 133/637)

agent/performer/tests/unit/   # helper + done-path tests
tests/unit/                   # coordinare routing, counter, escalation, config tests
```

**Structure Decision**: Single project. The feature spans the performer subpackage (where the gate executes, pre-push) and the coordinare package (where the env-blocked route, the self-fix counter, and the config live). No new packages or directories; all changes land in existing modules following the spec-075/spec-088 patterns they sit beside.

## Complexity Tracking

> No constitution violations — section intentionally empty.
