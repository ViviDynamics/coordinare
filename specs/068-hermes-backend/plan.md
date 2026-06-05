# Implementation Plan: Hermes Performer Backend

**Branch**: `068-hermes-backend` | **Date**: 2026-05-21 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/068-hermes-backend/spec.md`

## Summary

Add `hermes` as a new backend identifier inside the performer container, implemented as a one-shot CLI adapter (`HermesBackend`) that conforms to the existing `BackendAdapter` Protocol in `agent/performer/src/performer/backends/base.py`. The adapter mirrors the `claude_code` / `junie` one-shot pattern (queue relay feedback and replay on next invocation, no persistent chat session), launches Hermes with an explicit job-scoped `HERMES_HOME`, disables messaging/gateway/cron/clarify/shared-memory toolsets via Hermes' `--toolsets` / `disabled_toolsets` mechanism, and translates Hermes lifecycle states into the shared `working` / `done` / `error` vocabulary. Coordinare's orchestration code is untouched: only the performer backends factory, the new `hermes.py` adapter, config example, and tests change. Session-level `settings.AGENT_TIMEOUT` (already applied to all backends in `agent/performer/src/performer/main.py`) bounds Hermes runs — no new timeout machinery.

**Cross-backend persona routing (added 2026-05-21, see FR-015..FR-018)**: Where a backend exposes a job-isolated native identity slot, `score.persona_instructions` is routed there instead of being embedded as a `## Role Instructions` block in the task prompt body. Concretely: Hermes → write `$HERMES_HOME/SOUL.md` at `start()`; Claude Code → pass `--append-system-prompt <text>` on the CLI; Codex → already writes `developerInstructions` on the thread (the duplicate prompt-body block is dropped). Junie, opencode, and opencode_compat have no job-isolated slot (`AGENTS.md` would live in the workspace and could land in commits), so they retain the existing prompt-body wiring unchanged.

## Technical Context

**Language/Version**: Python 3.11 (performer container; same runtime as existing backends)
**Primary Dependencies**: `hermes-agent` (PyPI; baked into performer image — installation is out of scope per spec assumption), `asyncio.subprocess` for non-interactive CLI invocation, `pydantic` v2 (existing), `structlog` (existing)
**Storage**: Job-scoped temp directory used as `HERMES_HOME` (created at `start()`, removed unconditionally on every terminal outcome per FR-011). No persistent state.
**Testing**: `.venv/bin/pytest` against `agent/performer/tests/` (existing harness); `.venv/bin/ruff check` for lint
**Target Platform**: Performer container (Linux) — same image as opencode/junie/claude_code
**Project Type**: Single project (performer is a sub-package under `agent/performer/`); no new top-level structure
**Performance Goals**: Bounded by per-session `AGENT_TIMEOUT`. Cold-start overhead budget is tracked in spec.md SC-007.
**Constraints**: MUST NOT touch `~/.hermes`; MUST NOT enable messaging/gateway/cron/clarify/global-memory toolsets; MUST clean up job-scoped profile dir on every terminal outcome (success / error / stop / timeout); MUST keep coordinare's source tree free of Hermes-specific orchestration branches (SC-006).
**Scale/Scope**: One adapter file (~300–450 LoC mirroring `junie.py`), one factory-registry edit, one config example update, ~8 unit tests + 1 smoke-test doc. Two concurrent Hermes jobs per performer must coexist (SC-004).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Compliance |
|-----------|-----------|
| I. Code Quality First | Adapter is single-purpose; mirrors `junie.py` style; ruff/type-check required pre-merge. No new deps beyond `hermes-agent` already assumed in image. |
| II. Testing Discipline (NON-NEGOTIABLE) | FR-013 mandates tests covering factory registration, prompt/env propagation, persona passthrough, status transitions, stop semantics, disabled toolsets, and malformed-output handling. No flaky deps — Hermes CLI is mocked at subprocess boundary like other one-shot backends. |
| III. UX Consistency | No UI surface; the relevant "UX" is coordinare's status vocabulary, preserved exactly (FR-009). Error states surface through existing performer channels. |
| IV. Performance by Design | Reuses existing `AGENT_TIMEOUT` budget (FR-010a); no new perf-critical path. Cleanup is bounded (rmtree of job-scoped dir). |
| V. Clarity Before Action | All four ambiguities surfaced in spec were resolved in the 2026-05-21 clarification session and are recorded in spec.md. No `NEEDS CLARIFICATION` markers remain. |

**Result**: PASS — no violations, no complexity-tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/068-hermes-backend/
├── plan.md              # This file
├── spec.md              # Feature spec (already authored)
├── research.md          # Phase 0 output — entrypoint choice, toolset gating, env mapping
├── data-model.md        # Phase 1 output — HermesBackend / Profile / CapabilitySet entities
├── quickstart.md        # Phase 1 output — smoke-test path (assessor → architect → implementer)
├── contracts/
│   ├── backend_adapter.md  # BackendAdapter Protocol conformance contract
│   ├── env_vars.md          # Required + forbidden Hermes env vars
│   └── status_mapping.md    # Hermes → working/done/error translation rules
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 output (NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
agent/performer/
├── src/performer/
│   ├── backends/
│   │   ├── __init__.py      # MODIFY: register `"hermes"` → ("performer.backends.hermes", "HermesBackend")
│   │   ├── base.py           # UNCHANGED — existing BackendAdapter Protocol is sufficient
│   │   └── hermes.py         # NEW — HermesBackend adapter (one-shot CLI pattern from junie.py)
│   └── main.py               # UNCHANGED — existing Score routing + AGENT_TIMEOUT already covers Hermes
└── tests/
    └── unit/
        └── backends/
            └── test_hermes_backend.py  # NEW — factory, prompt/env, persona, status, stop, toolset gating, malformed output

config.example.yaml           # MODIFY: add `backend: hermes` example + env-var documentation
config.example.hermes.yaml    # NEW:    focused per-backend example for hermes (primary)
config.example.{codex,claude_code,opencode,opencode_compat,junie}.yaml  # NEW: parallel per-backend examples

src/coordinare/                # UNCHANGED — FR-007 / SC-006: zero coordinare orchestration changes
```

**Structure Decision**: Single project, performer-sub-package only. All implementation is confined to `agent/performer/src/performer/backends/hermes.py`, one line in the factory dict in `agent/performer/src/performer/backends/__init__.py`, new tests under `agent/performer/tests/`, and the root `config.example.yaml`. No file under `src/coordinare/` is touched — this is verified by SC-006 as a measurable outcome.

## Complexity Tracking

> No constitution violations. Table intentionally omitted.
