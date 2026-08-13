# Implementation Plan: Board-Simulation Benchmark — Phase 1 (evaluation substrate)

**Branch**: `134-board-sim-benchmark` | **Date**: 2026-07-23 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/134-board-sim-benchmark/spec.md`

## Summary

Build the evaluation substrate specs 135/136/137 stack on: run the **real**
coordinare daemon end-to-end against a **simulated** GitHub board, driving fresh
cards through the whole lifecycle (assessor → … → closer), and emit one structured,
schema-validated run artifact. Only the GitHub API is faked; performers, git, and
CI are real. The design injects a `FakeGitHubService` at the seam nodes already read
(`state["github_service"]`), constructs the daemon in a harness (bypassing
`__main__`), and drives it to terminal with hard time/cycle budgets. See the
approved implementation plan at
`~/.claude/plans/resilient-jumping-breeze.md` for the full narrative;
this file is the speckit-convention view.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv).
**Primary Dependencies (reused, no new runtime deps)**: pydantic 2.x (artifact +
protocol models), langgraph (the graph the daemon runs), `CoordinareDaemon`
(`src/coordinare/daemon.py:604`), `CoordinareGraphBuilder`
(`src/coordinare/graph/builder.py:54`), `http_performer_service` (the `/jobs`
dispatch), `pr_checks_service` (`CheckEntry`/`CheckRollup`), `required_checks_resolver`
/ CI-gate wiring in `monitor_performer`, `models/review.py` (`classify_reviewer`),
`ProjectConfiguration.from_yaml` (`config.py:1150`), structlog. Git is driven via
the existing performer git path and local `git`/`subprocess` for bare-repo setup.
**Storage**: None new in coordinare. The daemon's `state_store` is disabled
(`state_store=None`) for the harness. The run artifact is written to a harness-owned
run dir on local disk, not to coordinare state.
**Testing**: pytest via `make test` (unit + contract) / `make test-all` (adds
integration). New tests under `tests/unit/test_134_*.py` +
`tests/integration/test_134_end_to_end.py` (stubbed performer, deterministic, free).
**Target Platform**: Linux (dev + CI), same as coordinare.
**Project Type**: single project (coordinare).
**Performance / termination budget**: the run is bounded by `max_cycles` and a
wall-clock guard (SC-002) — every run terminates, no hang. The CI integration test
runs at zero model cost via a stubbed performer (SC-006).
**Scale/Scope**: one config, one run, a handful of seeded cards. Not a load test.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — PASS. No new external dependencies (all reused). New
  code is single-responsibility modules under `src/coordinare/bench/` + one
  `fake_github.py`. The Protocol is annotated; type-safe public surfaces. The
  approval "policy" is a plain callable, not a speculative class hierarchy (one
  implementation → no abstraction).
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Contract test (real service +
  fake both satisfy `GitHubServiceProtocol`), unit tests per fake method group +
  artifact schema, one deterministic integration test (stubbed performer → no
  flakiness, no external state). Existing suite must stay green (proves no behavior
  change to `GitHubService`).
- **III. User Experience Consistency** — N/A (no user-facing UI; a CLI +
  machine-read artifact). The CLI surfaces actionable errors on misconfig.
- **IV. Performance by Design** — PASS via the termination budget in Success
  Criteria (SC-002). This is a benchmark harness whose runtime is dominated by real
  models; the measurable budget is "always terminates within `max_cycles` /
  wall-clock", not a throughput target.
- **V. Clarity Before Action** — PASS. The two genuine ambiguities (cost source;
  CI-test performer) were resolved with the user before planning and recorded in
  spec (FR-012, SC-006) and research.md. Zero `NEEDS CLARIFICATION` remain.

**Result**: All gates pass. No Complexity Tracking entries required.

## Project Structure

### Documentation (this feature)

```text
specs/134-board-sim-benchmark/
├── plan.md              # This file
├── spec.md              # Feature spec
├── research.md          # Phase 0 — verified decisions + rationale
├── data-model.md        # Phase 1 — entities (artifact + protocol surface)
├── quickstart.md        # Phase 1 — how to run the benchmark
├── contracts/
│   ├── github-service-protocol.md   # the contract both real + fake satisfy
│   └── run-artifact.md              # the artifact schema
├── checklists/
│   └── requirements.md  # spec quality checklist
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/
├── graph/state.py                 # EXPAND GitHubServiceProtocol → node-called surface
├── services/
│   ├── github.py                  # UNCHANGED behavior (optionally annotate : Protocol)
│   └── fake_github.py             # NEW — in-process fake (board/git/PRs/reviews/CI/merges)
└── bench/                         # NEW package
    ├── __init__.py
    ├── artifact.py                # NEW — RunArtifact + submodels + validate()
    ├── approver.py                # NEW — gates_green(pr_state)->bool callable
    ├── fixtures.py                # NEW — manifest loader + file:// bare-repo materializer
    ├── recording_performer.py     # NEW — thin wrapper: delegates + records per-dispatch
    └── runner.py                  # NEW — run_board(): inject, drive, emit, teardown
scripts/
└── board_bench.py                 # NEW — CLI over run_board

tests/
├── unit/test_134_protocol_conformance.py   # real service AND fake satisfy the Protocol
├── unit/test_134_fake_github.py            # fake board/PR/review/CI/merge in isolation
├── unit/test_134_artifact_schema.py        # artifact validates + round-trips
└── integration/test_134_end_to_end.py      # one tiny fixture, STUBBED performer, loop closes

# Companion PR in sibling ViviDynamics/conductor-bench (separate repo):
#   whole-lifecycle fixture cards + BENCH.md rows
```

**Structure Decision**: Single-project layout. The seam (`fake_github.py`) sits with
the real service under `services/`; all harness code lives in a new self-contained
`src/coordinare/bench/` package so it is trivially removable and never imported by
production paths. The fixtures + `BENCH.md` truing land in the **separate**
`ViviDynamics/conductor-bench` repo, referenced by manifest.

## Complexity Tracking

No constitution violations — section intentionally empty.
