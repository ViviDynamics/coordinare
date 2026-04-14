# Implementation Plan: 043 — Performer CI Ownership

**Branch**: `043-performer-ci-ownership` | **Date**: 2026-04-14 | **Spec**: `specs/043-performer-ci-ownership/spec.md`
**Input**: Feature specification from `/specs/043-performer-ci-ownership/spec.md`

## Summary

Every performer that commits code must verify the repo's CI-equivalent checks pass before handing control back. Two complementary layers: (1) persona directives instructing each code-touching performer to run lint+tests locally via its existing shell access, and (2) a coordinare-side CI gate in `_advance_stage` that runs the detected CI command on a workspace clone before transitioning to `monitoring_pr`, routing back to the implementer on failure.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog, pydantic-settings, asyncio (all existing)
**Storage**: N/A — no persistence (CI detection is stateless)
**Testing**: pytest + .venv/bin/ruff check (existing)
**Target Platform**: Linux / macOS (wherever coordinare/performer runs)
**Project Type**: Single project — coordinare (src/) + performer agent (agent/performer/)
**Performance Goals**: CI detection < 100ms (filesystem stat calls only); CI gate execution bounded by the target repo's test suite runtime
**Constraints**: Must work without prior repo knowledge — detection is convention-based (Rakefile, package.json, pyproject.toml, Makefile, .github/workflows)
**Scale/Scope**: 6 performer personas updated + 1 new service + 1 coordinare gate node + performer-side pre-commit hook

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| I. Code Quality First | ✅ | Enforces lint/type-check before commit — directly advances this principle |
| II. Testing Discipline | ✅ | Ensures test suite passes before handoff — directly advances this principle |
| III. User Experience | N/A | No user-facing UI changes |
| IV. Performance by Design | ✅ | CI detection < 100ms (stat calls). Gate execution time bounded by target repo CI suite |
| V. Clarity Before Action | ✅ | No ambiguity — the spec was motivated by a concrete failure (PR #94) with a clear reproduction path |

**Quality Gates**: Lint & Format ✅, Type Check ✅ (pydantic models typed), Unit Tests ✅ (CI detection + gate transition tests), Coverage ✅ (new code fully tested)

No violations. No Complexity Tracking entries needed.

## Architecture Decisions

### AD-1: Persona-first, coordinare-gate as defence-in-depth

The performer's Claude Code backend already has full shell access — it can run any command in the workspace. The simplest and most flexible approach is to instruct performers to run CI locally via persona directives. The coordinare-side gate is a fallback for when a persona "forgets" (the AI model ignores the instruction) or when a non-Claude-Code backend doesn't have shell access.

**Alternatives rejected:**
- Coordinare-only gate (no persona changes): performers would still commit broken code, the gate would catch it but add a full retry round-trip
- GitHub Actions polling only: already implemented for post-PR check runs, but that happens AFTER push — we want to catch failures BEFORE pushing

### AD-2: Convention-based CI detection over configuration

Auto-detect the CI command by inspecting the workspace for well-known files (Rakefile, package.json, pyproject.toml, Makefile) rather than requiring users to configure it. This matches how most CI systems work and requires zero setup.

**Fallback**: If no convention is detected, skip the gate (log a warning). The persona instruction still tells the AI to look for and run CI — the gate is just an automated backstop.

### AD-3: CI detection runs in the performer workspace, not coordinare workspace

The performer has the full checkout with all dependencies installed. The coordinare-side workspace is a shallow clone used only for branch management. Running CI commands in the performer workspace ensures the right environment (Ruby gems, Node modules, Python venv) is available.

For the coordinare-side gate, re-use the performer's last workspace info (path) if still available, or skip the gate. Don't clone a fresh workspace just to run CI — that would double clone time.

### AD-4: Scope CI to lint only for the coordinare gate

Running a full test suite (which can take 10+ minutes) as a coordinare-level gate would stall the lifecycle. The gate should run the fast checks only (lint, type-check). Full test suites are the performer's responsibility via persona instructions.

## Project Structure

### Documentation (this feature)

```text
specs/043-performer-ci-ownership/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
└── tasks.md             # Phase 2 output
```

### Source Code (repository root)

```text
src/
├── coordinare/
│   ├── services/
│   │   ├── persona_service.py    # Updated: CI-ownership directive in 6 personas
│   │   └── ci_detection.py       # NEW: detect CI command from workspace files
│   └── graph/
│       └── nodes/
│           └── monitor_performer.py  # Updated: CI gate in _advance_stage

agent/performer/
└── src/performer/
    └── main.py               # Updated: run detected CI command before commit

tests/
└── unit/
    ├── services/
    │   └── test_ci_detection.py  # NEW: detection tests against fixture layouts
    └── graph/nodes/
        └── test_monitor_performer.py  # Updated: CI gate transition tests
```

**Structure Decision**: Single project, existing layout. One new service file (`ci_detection.py`), rest are updates to existing files.

## Files Changed

| File | Changes | Effort |
|------|---------|--------|
| `src/coordinare/services/ci_detection.py` | New — detect CI command from workspace file conventions | M |
| `src/coordinare/services/persona_service.py` | Add CI-ownership directive to implementer, qa, tech_writer, security, reviewer, closer personas | S |
| `src/coordinare/graph/nodes/monitor_performer.py` | CI lint gate in `_advance_stage` before `monitoring_pr` | M |
| `agent/performer/src/performer/main.py` | Run detected CI command before `commit_file` calls | M |
| `agent/performer/src/performer/workspace.py` | Add `run_command(cmd, cwd)` helper for running CI commands | S |
| `tests/unit/services/test_ci_detection.py` | New — detection tests for Ruby, Python, Node, Make, GitHub Actions layouts | M |
| `tests/unit/graph/nodes/test_monitor_performer.py` | CI gate: passes → monitoring_pr; fails → route to implementer | S |
| `tests/unit/test_workspace.py` | Test `run_command` helper | S |

~350 lines across 8 files. 4 commits.

## Complexity Tracking

No constitution violations to justify.
