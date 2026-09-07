# Feature Specification: Implementer Test-First Workflow with a Verified Hand-off

**Feature Branch**: `167-implementer-tdd-workflow`
**Created**: 2026-09-06
**Status**: Draft
**Input**: User description: "Extend the spec-164 role-workflow layer to the implementer: a test-first state machine that drives the coding harness in bounded, focused turns per blueprint milestone (tests turn, observed red, implementation turn, observed green, commit by code), then a quality pass against what the repository's CI requires, the existing local test gate, push, PR, and a wait for green CI with bounded repair turns before the hand-off to the reviewer. Every check after code is written loops back to a repair turn before continuing. Default-off via workflow: implementer."

## Problem

The implementer is one long agentic run of a coding harness (codex or claude_code) with a persona and, since spec 165, an implementation brief. Inside the run nothing bounds what the harness does: it may write code before tests, write tests that pass without the code, wander across the whole plan in one turn, or stop with a red tree. The checks that exist (the spec-070 commit floor, the spec-072 checkpoint sentinel, the spec-089 local test gate, the spec-075 CI fix loop) all run after the fact, so a bad run is detected late and bounced whole rather than corrected at the step that went wrong.

The spec-075 CI loop is also a second mechanism with its own prompts and counters, separate from the run that produced the code, and the hand-off to the reviewer (`pr_opened`) happens before CI has been seen green by the performer that wrote the change.

## Goals

- The implementer works one blueprint milestone at a time in a test-first cycle whose red and green are observed by running the project's tests, not asserted by the model.
- Every loop is bounded by code: turns per milestone, repair attempts per gate, wall clock per turn, wait per CI run.
- Every check that runs after code is written (red, green, quality, local gate, CI) sends a failure back to a repair turn with the exact output and re-runs the check before the workflow continues.
- The hand-off to the reviewer happens only when CI is green. Nothing red is ever pushed.
- Commits are written by code, one per step, so the history reads as the plan the architect wrote.
- The work item's kind selects a lane: a feature runs the test-first cycle; a bug starts with a bounded investigation turn and its red step is the reproduction of the reported behaviour; a chore (copy, configuration, CI process, dependency bump) makes the change in one turn and is verified by the existing tests, the quality set and CI rather than by new tests. All lanes share the gates, the caps, the commit rules and the hand-off.
- Default off. A role without `workflow: implementer` behaves exactly as today, including the spec-075 loop.

## Non-goals

- Replacing the coding harness with single model calls. The harness keeps its tools and its exploration; the workflow owns the loop, the verification and the commits.
- A refactor turn. The reviewer remains the refactor gate.
- Parallel milestones. One branch, one writer (the documenter side run of spec 165 stays on its own tree).
- Judging test quality by a model. The only judgement is whether the tests fail without the code and pass with it.
- Changing the reviewer, security, QA or documenter stages, or coordinare's CI gate (spec 090) and bounce rules. They remain the outer net.
- Parsing CI workflow files to discover quality commands. The set is detected for the common case and declared for the exact case.
- Research cards (an explanation is wanted, not code). Spec 168 ends those at the architect: the blueprint carries the answer, coordinare posts it on the issue and moves the card to the review lane, and no implementer is dispatched.
- Classifying the work item. The kind comes from the architect's blueprint (`work_kind`, spec 168); without a blueprint the lane defaults to feature, overridable per role.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A two-milestone card is built test-first, one milestone at a time (Priority: P1)

The architect's blueprint names two milestones. The implementer workflow runs the tests once to record the baseline, then for the first milestone asks the harness for the failing tests only, runs them, sees the new tests fail and the baseline still pass, commits the tests, asks the harness to make exactly those tests pass, runs them, sees green with no regression, commits the implementation, and moves to the second milestone. The branch history reads: tests for milestone one, milestone one, tests for milestone two, milestone two.

**Why this priority**: This is the loop itself. Without it there is no test-first work and no bounded turns.

**Independent Test**: Run the workflow against the `two_milestones` fixture with a scripted fake harness in a temporary repository with a bare remote: four commits in that order, every turn brief names one milestone, the red check saw failures, the green check saw none, and no turn touched the documentation tree.

**Acceptance Scenarios**:

1. **Given** an implementation brief with two milestones and `workflow: implementer` on the role, **When** the workflow runs, **Then** the harness is invoked with one milestone per turn, in order, and never with the whole plan.
2. **Given** a tests turn for a milestone, **When** the red check runs, **Then** at least one changed test file fails and every baseline test still passes, or the check does not pass.
3. **Given** a green check that passes, **When** the milestone completes, **Then** two commits exist for it, written by code, and the baseline for the next milestone includes the new tests.
4. **Given** the role has no `workflow` setting, **When** the implementer runs, **Then** the dispatch payload, persona and performer behaviour are byte for byte what they are today.

---

### User Story 2 - A vacuous or stuck milestone is bounded, not abandoned or forced through (Priority: P1)

The harness writes tests that pass without any implementation. The workflow reprompts once ("these pass without the behaviour; make them fail for the right reason"). If they still pass, the milestone fails. In a different run the implementation will not go green after three attempts, each carrying the fresh failure output. The workflow stops there, leaves the branch green at the last good commit (the red tests turn is reverted), and reports partial progress naming the failing milestone and the last failure, which coordinare already handles.

**Why this priority**: Bounding is the reason for the workflow. Without it a stuck harness burns the whole budget and hands off nothing useful.

**Independent Test**: Run the `stuck` fixture (harness never makes tests pass): exactly three implementation turns, the report is partial progress with the milestone as next focus, the working tree is clean at the last green commit, nothing was pushed. Run the `vacuous` variant: one reprompt, then a failed milestone.

**Acceptance Scenarios**:

1. **Given** a tests turn whose new tests all pass, **When** the red check runs, **Then** one reprompt turn is made and a second all-pass fails the milestone.
2. **Given** three implementation turns that leave the milestone's tests red, **When** the cap is reached, **Then** the run ends as partial progress with the milestone named, the red tests commit reverted, and no push.
3. **Given** a repair turn that makes the milestone tests pass but breaks a baseline test, **When** the green check runs, **Then** it does not pass and the next attempt's brief names the regressed test.

---

### User Story 3 - Quality, local gate and CI are gates with repair loops, and the hand-off waits for green (Priority: P1)

All milestones are green. The workflow runs the repository's quality commands (the detected lint command plus any the symphony declares). A failing command sends its output to a repair turn; the milestone tests run again, then the quality set runs again from the top. Clean, the workflow runs the existing local test gate, pushes, opens the PR, and polls the checks. A failing check sends the job's log excerpt to a repair turn, then tests, quality, push and poll again. Only green CI reports `pr_opened`; the reviewer sees a PR whose checks already pass.

**Why this priority**: This is the second half of the request: the performer cleans up what CI requires before the hand-off, and the hand-off waits for CI.

**Independent Test**: Run the `single` fixture with a fake quality command that fails once and a fake CI that fails once: one quality repair turn, one CI repair turn, the final report is `pr_opened`, the run record shows the attempts. Run with CI stuck pending past the wait budget: the report is an infrastructure hold, not a code failure.

**Acceptance Scenarios**:

1. **Given** a declared quality command exits non-zero, **When** the quality pass runs, **Then** a repair turn receives that command's output, the milestone tests and the full quality set run again, and the pass completes only when every command exits zero; a second failure after two repairs ends the run with the command named.
2. **Given** the quality set is clean, **When** the workflow proceeds, **Then** the spec-089 local gate runs unchanged, the push uses the spec-165 path (fetch, rebase, no force), and the PR opens without reporting `pr_opened`.
3. **Given** a check fails on the PR, **When** the CI phase runs, **Then** a repair turn receives the failing job's log excerpt, the tests and quality set run, the branch is pushed, and the checks are polled again; the same failing check names twice in a row count as no progress; three repairs exhausted end the run with the check named.
4. **Given** all checks are green, **When** the CI phase completes, **Then** the performer reports `pr_opened` with the run record, and the reviewer stage is dispatched exactly as today.
5. **Given** checks stay pending past the wait budget, **When** the CI phase times out, **Then** the performer reports the existing environment-blocked shape (a hold, not a bounce) naming the pending checks.

---

### User Story 4 - A small card gets one cycle and no ceremony (Priority: P2)

The blueprint marked the card single-turn, or there is no blueprint. The workflow treats the whole card as one milestone with the card's criteria as done-when: one tests turn, one implementation turn, quality, gate, push, CI, hand-off. At most five harness turns plus repairs.

**Why this priority**: Most live cards are small; the workflow must not make them slower than today.

**Independent Test**: Run the `single` fixture with no brief: one milestone, two commits, the same gates, `pr_opened`.

**Acceptance Scenarios**:

1. **Given** no implementation brief, **When** the workflow plans, **Then** there is exactly one milestone whose done-when is the card's acceptance criteria.
2. **Given** a brief with `implementer_single_turn` true, **When** the workflow plans, **Then** the brief's milestones are collapsed into one.

---

### User Story 5 - A bug is investigated, reproduced by a failing test, then fixed (Priority: P1)

The blueprint marks the card a bug. Before any code changes, the workflow gives the harness one bounded, read-only investigation turn: reproduce the reported behaviour, localise it, and write down the suspected cause and the file references in a structured note (no edits; the turn is verified write-free the way the architect's survey is). The cycle then runs with one difference: the tests turn must produce a test that fails because of the reported behaviour, which is the proof the bug is understood, and the implementation turn gets the investigation note as well as the failing test. Green, quality, gate, push, CI and hand-off are the feature lane's.

**Why this priority**: Bugs are a large share of live cards, and a fix without a reproduction is the case most likely to come back.

**Independent Test**: Run the `bug` fixture: exactly one investigation turn before any edit, the tree unchanged after it, the note present in the following briefs, the red check saw the reproduction fail, then green and `pr_opened`.

**Acceptance Scenarios**:

1. **Given** a blueprint with `work_kind: bug`, **When** the workflow plans, **Then** the first turn is an investigation turn whose brief forbids edits and whose result leaves the tree unchanged (any change is reverted and recorded).
2. **Given** the investigation note, **When** the tests and implementation briefs are built, **Then** both carry the note and the tests brief asks for a test that reproduces the reported behaviour.
3. **Given** an investigation turn that times out or errors, **When** the cycle continues, **Then** it proceeds without a note (recorded), rather than failing the card.

---

### User Story 6 - A chore or refactor is changed once and verified by what already exists (Priority: P1)

The blueprint marks the card a chore (a copy change, a configuration or CI-process edit, a dependency bump) or a refactor (behaviour must not change). There is nothing to write a failing test for: for a refactor the baseline is the contract. The workflow gives the harness one change turn per milestone against the brief, then verifies: the baseline tests still pass, the quality set is clean, and after push the CI run is green (for a CI-process change, CI is the verification). Commits, caps, repair loops and the hand-off are the same.

**Why this priority**: Small chores are the most common live card, and forcing a red step onto them would either invent vacuous tests or fail every card.

**Independent Test**: Run the `chore` fixture: no tests turn, one change turn, baseline unchanged, quality and CI gates exercised, `pr_opened`. A change turn that breaks a baseline test gets a repair turn and is bounded by the implementation attempt cap.

**Acceptance Scenarios**:

1. **Given** `work_kind: chore`, **When** the workflow plans, **Then** no tests turn or red check is scheduled and each milestone is one change turn followed by the baseline check.
2. **Given** a change turn that regresses a baseline test, **When** the baseline check runs, **Then** a repair turn receives the failure and the attempt cap applies.
3. **Given** a chore whose scope is under the CI workflow directory, **When** quality runs, **Then** the detected lint still runs, and the CI phase is the verification the hand-off waits on.

---

### User Story 7 - Missing coverage is added without touching the code (Priority: P2)

The blueprint marks the card `tests`: existing behaviour lacks tests. Each milestone is one tests turn; the check inverts: the new tests must pass against the code as it is, and the baseline must still pass. A new test that fails is not fixed by the implementer; it is a finding recorded in the run record, the run ends as partial progress naming the test, and a human decides whether it is a bug. Quality, gate, push, CI and hand-off are the same.

**Why this priority**: Coverage cards are common after a QA round and must not turn the implementer into a bug fixer by accident.

**Independent Test**: Run the `tests` fixture: no implementation turn, the new tests pass, `pr_opened`. Run its failing variant: partial progress naming the failing test, nothing pushed.

**Acceptance Scenarios**:

1. **Given** `work_kind: tests`, **When** the workflow plans, **Then** each milestone is one tests turn followed by the inverted check and no implementation turn is scheduled.
2. **Given** a new test that fails against existing code, **When** the check runs, **Then** the run ends as partial progress naming the test with its output, and nothing is pushed.

---

### Edge Cases

- **The harness commits anyway.** The persona forbids it; if it does, code squashes the turn's commits into the step commit so the history is the workflow's.
- **A turn edits outside its scope.** A tests turn that changes source, or any turn that changes the documentation tree: the out-of-scope changes are reverted by code before the check runs and recorded in the run record. This enforces spec 165's "no documentation from the implementer" in the workflow itself.
- **The runner reports no per-test names.** The baseline falls back to the pass and fail counts the spec-089 parsers already read; regression means the pass count dropped.
- **No test command detected.** The workflow cannot observe red or green; it stops before the first turn with an environment-blocked hold naming the missing runner, rather than pretending.
- **A turn exceeds its wall clock.** The harness is killed, the turn counts as a failed attempt, and the tree is reset to the last commit before the next attempt.
- **The local gate's environment signal fires.** The existing spec-089 hold applies; the workflow ends with that shape.
- **The PR already exists** (a re-dispatch after a bounce). The push updates it; the CI phase polls the existing PR.
- **Repair turn regresses a milestone.** Any repair turn (quality or CI) re-runs the milestone tests first; a regression sends the run back to a green check for that milestone rather than forward.
- **No blueprint, or a blueprint without `work_kind`.** The lane is feature unless the role's `workflow_env` sets a default kind; the run record names the lane and why it was chosen.
- **A bug card whose reproduction cannot be expressed as a test** (the harness says so in the tests turn). The red check fails and the reprompt applies; a second miss fails the milestone as partial progress with the investigation note as the reason, so the human sees what was found.
- **Restart mid-run.** A workflow run is one-shot in the container. A daemon restart re-dispatches the implementer, which starts from the branch as pushed (or unpushed commits are lost with the container, as today). The run record in the report is the only durable trace.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** With `workflow: implementer` on the implementer role, the implementing stage MUST run the implementer workflow as a code-driven state machine: intake, plan, per-milestone cycle (tests turn, red check, implementation turn, green check, commit), quality, local gate, push and PR, CI wait, hand-off. The model MUST never choose the next state.
- **FR-002** The workflow MUST drive the role's configured coding harness through a bounded turn primitive: one narrow brief per turn (kind, one milestone's goal, scope and done-when, and for repair turns the exact failing output), a wall-clock limit per turn, and a report of the files changed and the time taken.
- **FR-003** Milestones MUST come from the implementation brief in order. Without a brief, or with a brief marked single-turn, the whole card MUST be one milestone whose done-when is the card's acceptance criteria.
- **FR-004** Before the first turn the workflow MUST run the project's test command once and record the baseline (passing tests by name when available, otherwise counts). No test command means an environment-blocked hold before any turn.
- **FR-005** The red check MUST pass only when at least one test file changed by the tests turn fails and every baseline test still passes. All new tests passing MUST cause exactly one reprompt turn; a second all-pass MUST fail the milestone.
- **FR-006** The green check MUST pass only when the milestone's tests pass and the baseline still passes. Up to three implementation turns per milestone, each carrying the current failing test names and output excerpt. Exhaustion MUST fail the milestone.
- **FR-007** Commits MUST be written by code: one for the tests and one for the implementation per milestone, one per quality or CI repair that changes files. Harness-made commits MUST be squashed into the step commit.
- **FR-008** A tests turn MAY change only test paths; no turn MAY change the documentation tree. Out-of-scope changes MUST be reverted before the check and recorded in the run record.
- **FR-009** A failed milestone MUST end the run as the spec-072 partial-progress checkpoint naming the milestone as next focus and the last failure as reason, with the branch left at the last green commit (a red tests commit reverted) and nothing pushed.
- **FR-010** The quality pass MUST run the detected lint command and then each command declared on the role's `workflow_env` in order, stopping at the first non-zero exit; a failure MUST trigger a repair turn with that command's output followed by the milestone tests and the full quality set again; at most two repairs; exhaustion MUST end the run naming the command.
- **FR-011** The spec-089 local test gate MUST run unchanged after the quality pass, and its environment hold MUST apply.
- **FR-012** Push MUST use the spec-165 push path. The PR MUST be opened (or updated when it exists) without reporting `pr_opened`.
- **FR-013** The CI phase MUST poll the PR's checks until they conclude or a wait budget expires. A failed check MUST trigger a repair turn with the failing job's log excerpt, then the milestone tests, the quality set, a push and another poll; at most three repairs; the same set of failing check names on two consecutive polls MUST count as no progress and end the run early; exhaustion MUST end the run naming the check.
- **FR-014** Green CI MUST be reported as `pr_opened` with the run record. Checks pending past the wait budget MUST be reported as the existing environment-blocked hold naming the pending checks.
- **FR-015** Any repair turn MUST re-run the milestone tests before its own check; a regression MUST route back to a green check.
- **FR-016** Every turn wall clock, attempt cap and wait budget MUST be a documented default overridable through the role's `workflow_env`, and the whole run MUST remain inside the existing agent timeout.
- **FR-017** Without `workflow: implementer`, the implementing stage MUST behave byte for byte as today, including the spec-075 CI loop.
- **FR-018** Every state MUST log its duration and outcome, and every turn its kind, milestone, wall time and files changed, in the same events specs 164 to 166 emit, so budgets can be measured from live logs.
- **FR-020** The plan MUST select a lane from the blueprint's `work_kind` (spec 168 enum: `feature`, `bug`, `chore`, `refactor`, `tests`, `research`, `docs`): `feature` runs the cycle of FR-005 and FR-006; `bug` runs FR-021 then the cycle; `chore` and `refactor` run FR-022 (refactor keeps its own label in the run record and its commits read `refactor(#n): <goal>`); `tests` runs FR-023. `research` and `docs` MUST never reach the implementer (spec 168 ends them earlier); if one does, the workflow MUST stop before any turn with an environment hold naming the kind. Without a blueprint or a kind the lane MUST be feature unless the role's `workflow_env` declares a default; the run record MUST name the lane and its source.
- **FR-021** The bug lane MUST begin with one bounded investigation turn whose brief forbids edits and asks for a structured note (reported behaviour, reproduction, suspected cause, file references). Any change it makes MUST be reverted and recorded. The note MUST be carried into the milestone's tests and implementation briefs, and the tests brief MUST ask for a test that fails because of the reported behaviour. A failed or timed-out investigation turn MUST be recorded and the cycle MUST continue without a note.
- **FR-022** The chore lane MUST schedule no tests turn and no red check. Each milestone MUST be one change turn followed by the baseline check (no regression); a regression MUST trigger a repair turn under the implementation attempt cap. Quality, the local gate, push, PR, the CI wait and the hand-off MUST apply unchanged.
- **FR-023** The tests lane MUST schedule, per milestone, one tests turn and an inverted check: the changed test files MUST pass against the existing code and the baseline MUST still pass. A new test that fails is a finding, not a bug to fix: it MUST be recorded in the run record with its output and reported as partial progress naming the failing test, and the hand-off MUST NOT happen with it in the tree. Quality, the local gate, push, PR, CI wait and hand-off apply unchanged.
- **FR-019** Every rule (red check, green check, caps, no-progress, scope revert, commit squash, quality ordering, pending hold) MUST be a pure function or a unit with its own test, and each rule test MUST be shown to fail under a mutation of the rule it guards.

### Key Entities

- **Turn brief**: what one harness turn is asked to do: kind (tests, implement, repair), the milestone (goal, scope, done-when), the failing output when repairing, and the forbidden paths.
- **Turn result**: exit state, output tail, files changed by path, wall time, and any commits the harness made.
- **Baseline**: the set (or count) of passing tests before a step, grown after each green milestone.
- **Run record**: per milestone the turns and attempts and outcomes; quality and CI attempts; phase durations; commits made; scope reverts. Travels in the report; not consumed by other roles.
- **Milestone plan**: the ordered milestones the run works, derived by code from the brief or the card.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** On the live fleet, every PR the workflow hands off has green CI at hand-off time, measured over the first ten live rounds after enablement.
- **SC-002** Zero pushes of a red tree: on every fixture and live round, the local test gate never sees a failing test and no commit in the pushed history has failing tests at its own head.
- **SC-003** Zero implementer-authored documentation changes reach a PR once the workflow is enabled.
- **SC-004** No run exceeds its turn caps: at most one tests turn plus one reprompt and three implementation turns per milestone, two quality repairs, three CI repairs, on every fixture and live round.
- **SC-005** A single-turn card completes in under 30 minutes at the 90th percentile including container start and CI wait, measured from the first ten live single-turn rounds.
- **SC-007** On every `bug` fixture and live bug round, an investigation turn precedes the first edit and the red check's failing test names appear in the run record.
- **SC-008** On every `chore` fixture and live chore round, no tests turn runs and the hand-off still waits for green CI.
- **SC-009** On every `tests` fixture and live tests-lane round, no implementation turn runs and a new test that fails against existing code ends the run as partial progress naming it.
- **SC-006** The nine fixtures (`single`, `two_milestones`, `vacuous`, `stuck`, `ci_pending`, `bug`, `chore`, `refactor`, `tests`) pass deterministically with the scripted fake harness in CI against a real temporary repository, and `single` and `two_milestones` are runnable live through the gateway.

### Performance budgets (Constitution IV, provisional until measured live)

| Item | Budget | Source |
| --- | --- | --- |
| Harness turn wall clock | 20 minutes | FR-002, FR-016 |
| Turns per milestone | 1 tests + 1 reprompt + 3 implementation | FR-005, FR-006 |
| Quality repairs | 2 | FR-010 |
| CI repairs | 3 | FR-013 |
| CI wait | 30 minutes | FR-013, FR-014 |
| Single-turn round | under 30 minutes p90 | SC-005; replaced once ten live rounds are logged |

## Assumptions

- The spec-165 implementation brief is present on the dispatch when the architect workflow is on; the plan falls back to the card when it is not.
- The spec-089 detector's test command and summary parsers are the source of red, green and baseline; the lint command it detects is the first quality command.
- The performer's existing GitHub helpers (check runs, PR open) provide the CI polling and PR operations; no new GitHub surface is needed beyond fetching a failing job's log excerpt.
- The coding harness is one of the adapters the role already runs; a harness that cannot be given a per-turn persona is not supported by this workflow.

## Rollout

Default off. Enabled per symphony by setting `workflow: implementer` on the implementer role after the five fixtures pass in CI and `single` and `two_milestones` have each been run once live in a performer container with turn and round durations recorded against the budgets. The performer images must be rebuilt (base, full, extra) before enabling. Recommended order: enable on a symphony that already runs `workflow: architect`, so the milestones come from a blueprint.
