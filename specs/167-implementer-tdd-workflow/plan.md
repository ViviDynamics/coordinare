# Implementation Plan: Implementer Test-First Workflow with a Verified Hand-off

**Branch**: `167-implementer-tdd-workflow` | **Date**: 2026-09-06 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/167-implementer-tdd-workflow/spec.md`

## Summary

Extend the spec-164 role-workflow layer to the implementer: a test-first state machine that drives the coding harness in bounded, focused turns per blueprint milestone. Each milestone follows a red-green cycle (tests turn, red check, implementation turn, green check, commit by code). After all milestones are green, quality commands run (lint plus any declared on the role), then the existing local test gate, push, PR open, CI polling with repair loops, and hand-off only when CI is green. Every check after code is written loops back to a repair turn with the exact failure output before continuing. Commits are written by code, one per step, so the history reads as the plan. Bounded by caps on turns, repair attempts, wall clock, and CI wait. Default-off via `workflow: implementer`.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (turn, run record, and gate rule models), the 164 layer (`workflows/`, `Toolkit` with `run_agent_turn` added, `budget`, `schema_guard`), spec-089 CI detection and local test gate, spec-165 push-path changes and brief parsing, spec-090 CI state polling helpers (`github.get_check_runs`, `summarise_check_runs`), structlog (observability). No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py` unchanged. Run record is transient (produced by workflow, travels in report, not persisted by coordinare). Performer is one-shot per spec FR-010.
**Testing**: pytest (`tests/unit`, `tests/eval`, `agent/performer/tests` run separately). Worktree runs need `PYTHONPATH=src:agent/performer/src`. Deterministic eval fixtures in `tests/eval/implementer_scenarios/` run in CI; live fixtures run on demand with `--live` flag.
**Target Platform**: the performer container (Debian, `coordinare-performer:extra`) for the workflow; the coordinare daemon for carrier dispatch and observation.
**Project Type**: single repository, two packages (coordinare, performer).
**Performance Goals**: single-turn round under 30m p90 including container start and CI wait (SC-005, provisional); harness turn under 20m (FR-016); CI wait up to 30m (FR-014).
**Constraints**: no new coordinare state (run record is transient). Prose path byte-for-byte unchanged when workflow flag is off (FR-017). Every gate rule is testable in isolation with a mutation test (Constitution II). Workflow is one-shot in the container; restart re-dispatches the implementer from the branch as pushed (or unpushed commits lost).
**Scale/Scope**: one new workflow package (workflows/implementer, ~10 modules), new Toolkit method, new adapter helper, five eval fixtures, gate rules and turn personas, ~2000 lines of performer code. No coordinare changes except config (KNOWN_WORKFLOWS). No schema bumps.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | Every gate rule is a pure function with its own test (red check, green check, baseline detection, vacuous test detection, no-progress check, squash/revert sequence, quality gate, CI gate). No dead code. Reuses existing modules (ci_detection, github, workspace) where possible. | PASS |
| II. Testing Discipline | Unit tests per step (intake, plan, per-turn cycle, quality, local gate, ci, report); per gate rule (mutation-tested per Constitution II standing rule). Deterministic CI eval with five fixtures and a scripted fake harness. Live eval on demand with real model. | PASS |
| III. UX Consistency | Operator surface is one key (`workflow: implementer`) matching 164/165. Block reasons name the condition (milestone, quality command, check name). Run record structure matches 164/165/166 patterns. | PASS |
| IV. Performance by Design | Budgets stated in spec table (20m turn, 30m CI wait, under 30m p90 for single-turn including container start). Provisional, replaced by SC-005 after ten live rounds. Phase durations logged in run record. | PASS |
| V. Clarity Before Action | Every decision in research.md cites code path (file:line) or measurement it rests on. No ambiguity about when the workflow is active (flag presence), what constitutes red/green (code-driven test parsing), how commits are made (squash sequence), how terminals are reached (state machine). | PASS |

No gate changed. Post-design re-check (after writing performer code): ready.

## Project Structure

### Documentation (this feature)

```text
specs/167-implementer-tdd-workflow/
├── spec.md                          # Approved spec
├── plan.md                          # This file
├── research.md                      # Decisions with file:line citations
├── data-model.md                    # TurnBrief, TurnResult, Baseline, RunRecord, state machine
├── quickstart.md                    # Enable workflow, run fixtures, live test checklist
├── contracts/
│   ├── turn-brief.schema.json       # Input to one harness turn
│   ├── run-record.schema.json       # Workflow run history (travels in report)
│   └── dispatch-payload-additions.md # None (no new dispatch fields)
└── tasks.md                         # Produced by /speckit.tasks after this plan
```

### Source Code (performer, ~2000 lines)

```text
agent/performer/src/performer/
├── workflows/
│   ├── __init__.py                  # + "implementer": (workflows.implementer, "ImplementerWorkflow")
│   ├── toolkit.py                   # + run_agent_turn(brief) -> TurnResult, + agent_turn_runner: AgentTurnRunner
│   ├── adapter.py                   # + build_production_toolkit: wire agent_turn_runner from backend adapter
│   ├── base.py                      # unchanged (WorkflowMetrics already has fields for turns, etc.)
│   └── implementer/
│       ├── __init__.py              # ImplementerWorkflow: intake -> plan -> cycle -> quality -> local_gate -> ci_wait -> report
│       ├── models.py                # TurnBrief, TurnResult, Baseline, MilestonePlan, RunRecord, PerMilestoneRecord, etc.
│       ├── personas.py              # TESTS, IMPLEMENT, REPAIR_TESTS, REPAIR_IMPLEMENT, REPAIR_QUALITY, REPAIR_CI
│       ├── intake.py                # Assemble milestones from brief or card
│       ├── baseline.py              # Run test command once, parse with ci_detection parsers
│       ├── cycle.py                 # Per-milestone loop: tests, red check, implement, green check, commit
│       ├── quality.py               # Lint + declared commands, repair loop, at most 2 repairs
│       ├── ci.py                    # Poll checks, repair loop on failure, at most 3 repairs, pending hold
│       ├── commits.py               # Squash harness commits, revert out-of-scope, record reverts
│       ├── gates.py                 # Pure functions: red_check, green_check, baseline_detection, vacuous_test_check, no_progress_check
│       └── report.py                # Assemble RunRecord and PerformerResponse
│   └── [existing workflows unchanged]
├── backends/
│   ├── base.py                      # BackendAdapter protocol unchanged
│   └── [all adapters unchanged]     # ClaudeCodeBackend, JunieBackend, etc.
└── models.py                        # Score unchanged (workflow_env already supported)

tests/
├── unit/workflows/implementer/      # Per module: models, intake, baseline, cycle, quality, ci, commits, gates, e2e with fake harness
├── eval/implementer_scenarios/      # Five fixtures with stubbed model (CI) and --live mode (real model)
└── contract/
    └── test_dispatch_payload.py     # Verify no new fields added
```

### Source Code (coordinare, no changes)

```text
src/coordinare/
├── config.py                        # + KNOWN_WORKFLOWS: "implementer"
└── [all other files unchanged]      # No state schema bump, no graph changes, no dispatch changes
```

**Structure Decision**: Mirror 164/165/166 exactly. The implementer is a sibling workflow under `workflows/`; every step (intake, plan, cycle, quality, ci, report) is a module with a pure-function gate. The gate rules live in a separate `gates.py` module so they are testable and mutation-testable per Constitution II. The run record is produced at the end and travels in the report; it is not consumed by coordinare and does not require persistence. The workflow is one-shot in the container (spec FR-010); restart re-dispatches, losing the intermediate state but keeping the PR and branch on the remote.

## Risks named by the research

- **Stall watchdog (R-e)**: the 077 watchdog kills a `working` turn with no event or token growth for `stall_timeout_seconds` (900 live). `run_agent_turn` must forward the inner harness's events and token growth through the outer adapter's status, and the quickstart tells the operator the turn wall clock must not exceed the stall timeout unless forwarding is in place. Pinned by a test that a running turn's status changes between polls.
- **Repair exhaustion is a bounded failure, not a hold**: quality and CI repair caps end the run as partial progress naming the command or check (FR-010, FR-013); only pending CI past the wait budget, a missing test runner and the 089 environment signal are holds (FR-004, FR-011, FR-014).
- **Whole-run wall time**: the performer's `AGENT_TIMEOUT` default is 7200s; the budgets in the spec table sum to roughly that for a four-milestone card. The eval records per-phase durations so the defaults can be tuned from live rounds rather than guessed.

## Complexity Tracking

| Item | Why it is needed | Simpler alternative rejected because |
| --- | --- | --- |
| Toolkit.run_agent_turn method | 164 FR-007 requires the workflow to own the loop; the turn brief must be narrow and the harness output must be captured per turn. A new Toolkit method keeps the turn execution protocol in one place. | A separate agent_turn_runner function outside Toolkit would require passing metrics, backend config, persona assembly to every call; a method is simpler. |
| Gate rules as pure functions | Constitution II requires every rule with its own test, and mutation testing is mandatory. Bundling them into one "check step" function hides which rule failed. | A monolithic check function cannot be tested per rule. Mutation testing would require mocking N rules instead of exercising one at a time. |
| Per-turn persona assembly | Each turn has distinct constraints (tests: no source; repair: given failure output; quality/CI: narrowly scoped). A shared persona cannot forbid different things. | A single persona with optional blocks is unverifiable (the model does not understand "if this is a repair turn, ignore the source constraint"). Per-turn personas are checked by code, not hoped for. |
| Squash and revert in code | Commits must be one per step (FR-007) and out-of-scope changes must be reverted (FR-008). A deterministic sequence (reset --soft, checkout, rm, add, commit) is testable and auditable. | Asking the harness not to commit is unverifiable. Manual squash post-hoc allows bad commits to be pushed and reverted by coordinare later. |
| Quality commands from workflow_env | Quality is repository-specific (lint + custom checks). A hardcoded list would require per-repo config outside the workflow. | Hardcoding "pytest + black + mypy" or "npm test + npm run lint" forces all repos into one pattern. workflow_env allows each symphony to declare what it needs. |
| Separate run record | The workflow produces structured history (turns, attempts, gates, timing) that coordinare needs for metrics and troubleshooting. Embedding it in the prose report loses structure. | A prose report ("Tests turned green after 2 attempts") is unquotable for metrics. The run record allows coordinare to extract turn_count, model_calls, phase durations for budgeting and alerting. |

## Phases

- **Phase 0 (research.md)**: complete; R1-R8 all resolved from code reads (file:line citations). R-a to R-d resolutions documented.
- **Phase 1 (this plan + data-model.md + contracts/ + quickstart.md)**: design, data shapes, live-test validation setup.
- **Phase 2 (tasks.md)**: produced by `/speckit.tasks`, organized by user story: US1 (happy path), US2 (bounding), US3 (gates), US4 (single turn), plus edge cases and gate-rule tests.

## Key Decisions

1. **Turn primitive**: New `run_agent_turn(brief: TurnBrief) -> TurnResult` on Toolkit, with injected `agent_turn_runner` callable that instantiates the role's backend adapter, runs it per-turn persona, polls until done or wall-clock, then computes files changed.

2. **Red/green detection**: Code-driven test parsing via `ci_detection.detect()` (spec 089) with format-specific parsers (pytest, rspec, jest). Baseline is per-test names or counts. Red check passes only when at least one changed test file fails and baseline still passes. Green check passes only when all baseline and milestone tests pass.

3. **Squash and revert**: After each turn, `git reset --soft <turn-start>`, revert out-of-scope paths, add in-scope, commit. Reverted paths recorded in run record. Squash happens before check runs so the check sees a clean tree.

4. **Quality pass**: Detected lint + declared commands (newline-separated in workflow_env), first failure triggers repair turn, at most 2 repairs, then local gate.

5. **CI polling and repair**: Reuses existing `github.get_check_runs()` and `get_check_run_logs()`. Repair on fail, at most 3 repairs. No-progress check: same failing checks on two consecutive polls end the run. Pending past timeout -> env_blocked hold.

6. **Terminal outcomes**: `pr_opened` (all green), `changes_requested` (local gate failed), `partial_progress` (milestone failed, branch at last green, not pushed), `env_blocked` (environment hold or CI pending). No new coordinare states.

7. **Run record**: Transient (produced by workflow, travels in report, not persisted). Carries turn history, phase durations, scope reverts, metrics for coordinare observability.

8. **Evaluation**: Five fixtures (single, two_milestones, vacuous, stuck, ci_pending) with scripted fake harness, deterministic and runnable in CI. Live mode available for performance measurement.

## What Could Go Wrong

After code is written, research these potential contradictions or impossibilities:

1. **Backend lifecycle**: Can a backend adapter be started twice in one container with different personas? Does `get_status()` cleanly report "done" without the adapter itself failing? Can we reliably detect turn completion without a blocking wait on the process?
   - **Mitigation**: Read backends/claude_code.py and backends/codex.py fully. Test start/poll/stop lifecycle with multiple invocations in a unit test before writing the workflow.

2. **Git squash atomicity**: Does `git reset --soft` lose any state if the working tree has concurrent modifications (harness still writing)? Does `git checkout --` fail safely when paths are untracked?
   - **Mitigation**: Write workspace.py's squash helper with full error handling. Test with a real repository and a concurrent file writer.

3. **Test parser precision**: Can pytest --collect-only fail when there are syntax errors in the test file? If so, does the workflow treat that as "no tests passed" (red) or an error?
   - **Mitigation**: Test ci_detection parsers with broken test files. Ensure the workflow treats parser failure as a turn failure (not a gate pass).

4. **GitHub API rate limits**: Can a workflow exhaust the token's rate limit during CI polling? Does a 429 response cause the workflow to hang or fail gracefully?
   - **Mitigation**: Read github.py's error handling. Ensure rate-limit errors are treated as transient (retry with backoff) or infrastructure holds (env_blocked).

5. **Wall-clock enforcement**: Does asyncio.wait_for with a timeout correctly kill the backend subprocess? If the backend has child processes, does `adapter.stop()` kill them all?
   - **Mitigation**: Test timeout behavior with a real harness and a long-running turn. Verify no zombie processes remain after timeout.

6. **Score field mutations**: If we modify score.persona_instructions per turn, does the backend's existing code (main.py, adapter) correctly use the modified value, or does it cache it?
   - **Mitigation**: Trace main.py and claude_code.py to confirm persona_instructions is read from score each time, not cached at adapter construction.

7. **Log endpoint scope**: Does the job-logs endpoint require additional GitHub scopes beyond `repo`? Do classic PATs and fine-grained tokens both support it?
   - **Mitigation**: Test with both token types against a real PR with a failed check.

8. **Executor timeout**: If a harness turn takes 20m and the performer's actor timeout is 120m, is there enough time for quality, local gate, push, PR, and CI polling? Does the performer's timeout enforce strictly, or does it allow some grace period?
   - **Mitigation**: Calculate total budget: baseline ~30s, per turn ~20m × ~4 turns = ~80m, quality ~5m, local gate ~2m, CI wait ~30m = ~120m (tight). If live rounds exceed this, increase budgets or reduce quality checks.
