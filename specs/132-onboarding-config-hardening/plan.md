# Implementation Plan: Onboarding config hardening

**Branch**: `132-onboarding-config-hardening` | **Date**: 2026-07-31 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/132-onboarding-config-hardening/spec.md` (issue #180)

## Summary

Remove three first-run onboarding walls in Coordinare: (1) point the example
`agent_executable` default at the portable `bin/run-performer` wrapper and reduce the
checked-in `bin/performer` script (which carries a hardcoded personal path) to a minimal
compatibility wrapper that delegates to `bin/run-performer`; (2) ship the
example notifications block **off** by default (empty `channels`/`routing`) with the live
Slack/email channels preserved as commented references, so a fresh copy validates without
secrets; (3) improve observability so a symphony paused via `enabled: false` announces
itself with a clear startup line instead of a per-cycle warning. All changes are confined
to an example config, one wrapper script, one daemon log path, onboarding docs, and tests.
No new runtime behavior, dependencies, data model, or public API.

## Technical Context

**Language/Version**: Python 3.12+ (prod 3.14.5 via uv); POSIX/bash for `bin/` wrappers
**Primary Dependencies**: pydantic v2 (config validation), structlog (logging), langgraph/daemon (unchanged). **No new dependencies.**
**Storage**: N/A — no persisted state added or changed
**Testing**: pytest (`tests/unit/`), run via `make test-all`; lint via `make lint` (ruff)
**Target Platform**: Linux/macOS host running the coordinare daemon
**Project Type**: Single project (existing `src/coordinare/` layout)
**Performance Goals**: N/A — no performance-sensitive path touched (see Constitution Check IV)
**Constraints**: Changes scoped to onboarding/example-config hardening only; no spec-133 (personal account) behavior
**Scale/Scope**: ~1 example file, 1 script reduced to delegator, 1 daemon log-line change, docs audit, 1 new unit test module

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

- **I. Code Quality First** — PASS. Reducing `bin/performer` to a thin delegator keeps a
  single source of launch logic (`bin/run-performer`) — no duplication, single
  responsibility, minimal. No hardcoded personal paths remain.
- **II. Testing Discipline** — PASS (with new tests). Add unit tests: (a) the shipped
  `config.example.yaml` loads and validates with **no** notification secrets in the env;
  (b) `agent_executable` in the example resolves to an existing, executable
  `bin/run-performer`; (c) a symphony with `enabled: false` produces exactly one startup
  "paused" line and no per-cycle warning. Coverage must not regress.
- **III. User Experience Consistency** — PASS. The paused-symphony log line is actionable
  and unambiguous, matching the "error/status messages must tell the user what's happening"
  principle. Applies to operator-facing log output.
- **IV. Performance by Design** — PASS (bounded, not timed). No performance-critical path is
  touched. Rather than an arbitrary millisecond budget (no such project convention exists),
  the measurable bound is **event-count**: SC-006 requires the paused-symphony status to be
  emitted exactly once at startup with zero recurring per-cycle entries — directly
  preventing the per-cycle log growth the current code exhibits.
- **V. Clarity Before Action** — PASS. The single genuine fork (`bin/performer` remove vs.
  keep-as-delegator) was surfaced and is now approver-resolved (keep as delegator). No
  `NEEDS CLARIFICATION` markers remain.

**Result**: No violations. Complexity Tracking table not required.

## Project Structure

### Documentation (this feature)

```text
specs/132-onboarding-config-hardening/
├── spec.md              # Feature spec (/speckit.specify)
├── plan.md              # This file (/speckit.plan)
├── research.md          # Phase 0 decisions (/speckit.plan)
├── quickstart.md        # Phase 1 verification steps (/speckit.plan)
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 (/speckit.tasks — NOT created yet)
```

No `data-model.md` and no `contracts/` — this feature introduces no new entities and no
new request/response interfaces (Phase 1 design artifacts are intentionally omitted as N/A).

### Source Code (repository root) — files touched

```text
config.example.yaml                         # MODIFY: agent_executable default (~L85) → bin/run-performer;
                                            #         commented agent_executable ref (~L909) → match;
                                            #         notifications channels/routing → [] with live blocks
                                            #         moved to commented reference (~L742-760)
bin/performer                               # MODIFY — reduce to a minimal compatibility wrapper that
                                            #   delegates to bin/run-performer (no duplicate launch logic;
                                            #   not deleted). Replaces the pre-existing working-tree edit.
bin/run-performer                           # UNCHANGED — canonical portable wrapper (the new default)
src/coordinare/daemon.py                     # MODIFY: paused-symphony logging (~L2961-2963 multi-symphony
                                            #   loop + legacy single-symphony branch ~L2972): clear
                                            #   startup line, deduped so it does not recur every cycle
README.md / onboarding docs                 # MODIFY (audit): update any stale performer-path or
                                            #   notification-default references (FR-008)
tests/unit/test_132_onboarding_config_hardening.py   # NEW: validation + example-default + paused-log tests
```

**Structure Decision**: Single-project layout, unchanged. All edits live in existing
locations; the only structural change is deletion of `bin/performer`.

## Implementation Approach (per user story)

- **US1 (P1) — performer launch**: Edit `config.example.yaml` `agent_executable` default
  and its commented twin to `bin/run-performer`. Reduce `bin/performer` to a minimal
  compatibility wrapper that `exec`s `bin/run-performer` (resolving it relative to the
  script's own directory), replacing the pre-existing working-tree edit — no duplicated
  launch logic, not deleted. Document "launch from repo root" per research R2.
- **US2 (P2) — notifications off**: In `config.example.yaml`, set `notifications.channels: []`
  and `notifications.routing: []`; move the current Slack + email channel definitions into
  an adjacent commented reference block. No validator code change.
- **US3 (P3) — paused-symphony clarity**: In `daemon.py`, replace the per-cycle
  `symphony.disabled_skip` warning with a one-time, human-readable startup line
  (`symphony '<name>' is paused (enabled: false)`); ensure both the multi-symphony loop and
  the legacy single-symphony branch are covered and the message does not repeat every cycle.

## Complexity Tracking

No constitution violations — table intentionally empty.

## Resolved decision (approver-confirmed)

**`bin/performer`** (FR-002) is **kept as a minimal compatibility wrapper that delegates to
`bin/run-performer`** — no duplicated launch logic, not deleted in this PR. No open forks
remain for the gate.

## Phase notes

- **Phase 0 (research)**: complete → [research.md](./research.md).
- **Phase 1 (design)**: no data model / contracts (N/A); verification steps captured in
  [quickstart.md](./quickstart.md). Agent-context update script skipped — no new technology
  is introduced, so it would be a no-op.
- **Phase 2 (tasks)**: deferred to `/speckit.tasks` — not run yet (awaiting plan review per
  the team-mode gate).
