# Feature Specification: Implementer Resume Rule for Milestones a Previous Run Already Did

**Feature Branch**: `171-implementer-resume`
**Created**: 2026-09-07
**Status**: Draft
**Input**: User description: "Spec follow-up to 167. Live: milestone one's implementation turn also implemented milestone two, milestone two's tests turn changed nothing, red could not be observed, and the run ended partial_progress. The next dispatch restarts from the brief on the same branch and repeats it, so the card never finishes. Design a resume rule in plan.py / driver.py: skip milestones whose done-when is already met and start from the first unmet one; when a tests turn changes no test file and the run's tests already cover the milestone, record it as already satisfied instead of failing red. Keep FR-005 strict for a genuinely vacuous tests turn."

## Problem

The implementer plans from the architect's brief and only from the brief. Every dispatch of a card rebuilds the same milestone list in the same order and runs milestone zero first, whatever is already on the branch.

That is fine while the model stays inside one milestone. It stops being fine the moment it does not, and the live round shows the whole failure:

1. Milestone zero's implementation turn wrote `src/m0.py`, `src/m1.py` and `tests/test_m1.py`. Spec 167's `reverted_foreign_test` rule (added in response to this round) reverts the foreign test file. Nothing reverts the foreign source file, because implementation turns do not enforce their source scope, so `src/m1.py` was committed under `feat(#N): milestone 0`.
2. Milestone one's tests turn then had nothing to observe. Its test file either changed nothing or was written and passed on the first run, because the code it was meant to drive was already on the branch. `red_check` returned false, the one reprompt returned false again, and the milestone failed with "red was not observed". The run reported `partial_progress` with milestone one as the next focus.
3. On the next dispatch the plan restarts at milestone zero. Milestone zero's tests now pass at baseline, so red cannot be observed there either. Run two fails earlier than run one. The card never finishes and no amount of re-dispatch changes that.

The stage has no notion of work already on its own branch. Everything it needs to acquire one is in the git history it wrote: this card's commits carry the `test(#N):`, `feat(#N):`, `fix(#N):`, `chore(#N):` and `refactor(#N):` prefixes the driver puts on them, and the baseline run already says which tests pass.

## Goals

- A dispatch onto a branch that already carries this card's commits starts at the first milestone that is not already done, instead of at milestone zero.
- A milestone whose code a previous run already wrote, but whose tests that run lost, is completed by writing the tests rather than by failing to observe red.
- A tests turn that changes nothing is not a failure when the milestone's tests are already present and passing because of a previous run.
- FR-005 stays strict for a genuinely vacuous tests turn: a test the model writes that passes without any code of ours behind it still fails the milestone after one reprompt.
- Every resume decision is a pure function over the git history, the milestone plan and a test result set, and is visible in the run record.
- A first run on a fresh branch behaves byte for byte as it does today.

## Non-goals

- Full enforcement of source scope on implementation turns. #279 instead reverts only source exclusively claimed by another planned milestone; current-scope overlaps and unclaimed helper files remain permitted. Empty or whole-tree current scopes retain their existing unbounded meaning and are not narrowed by another milestone. Reverting source files a milestone's scope did not name would stop the leak at its origin, but architect brief scopes are approximate prose and a milestone that legitimately touches a file its scope understated would lose correct work mid-turn and then fail its green check. Considered and rejected: the resume rules make the leak survivable, which is cheaper and safer than making an approximate scope authoritative.
- Resuming a milestone whose tests a previous run committed red. `run_milestone` resets the tree to the milestone's start commit when the milestone fails, so a failed milestone leaves nothing behind and this state cannot occur.
- Carrying the previous run record on the dispatch payload. `Score` has no such field and does not need one: the branch history is the record.
- Any change to the repair, chore, refactor or bug lane's own shape.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A re-dispatch starts where the last run stopped (Priority: P1)

A card's first run completed milestone zero and failed milestone one. Coordinare re-dispatches implementing on the same branch. The workflow sees its own `test(#N):` and `feat(#N):` commits, finds milestone zero's tests present and passing at baseline, skips it, and opens the first turn of the run on milestone one.

**Why this priority**: without it every re-dispatch fails on a milestone that is already finished, so no card that bounces once can ever finish.

**Independent Test**: run the workflow twice against one real clone with a harness whose milestone-zero implementation turn also writes milestone one's source. The second run's first brief is for milestone one, the record shows milestone zero satisfied by the prior run, and the run reaches `pr_opened`.

**Acceptance Scenarios**:

1. **Given** a branch carrying `test(#7): failing tests for milestone 0` and `feat(#7): milestone 0`, and a baseline in which `tests/test_m0.py` passes, **When** the workflow plans, **Then** milestone zero is skipped, the first brief carries `milestone_index` one, and the record marks milestone zero `satisfied_by: prior_run`.
2. **Given** a fresh branch with no commits of this card, **When** the workflow plans, **Then** no milestone is skipped whatever the repository already contains, and the run is identical to today's.
3. **Given** a branch where milestone one is done but milestone zero is not, **When** the workflow plans, **Then** nothing is skipped: only a contiguous prefix of finished milestones is skipped, so milestone zero still runs.
4. **Given** every planned milestone is already done, **When** the workflow runs, **Then** no turn runs, the workflow proceeds to the quality pass, the local gate, push, the pull request and CI, and reports `pr_opened` on green.

---

### User Story 2 - A milestone whose code is already on the branch is finished by writing its tests (Priority: P1)

The previous run left milestone one's source on the branch and its test file reverted. The milestone's code is present, its coverage is owed. The workflow runs it in the existing tests lane: one tests turn whose tests must pass against the code already there, regressions checked, committed as `test(#N): cover <goal>`.

**Why this priority**: this is the exact live shape. Without it the second dispatch skips milestone zero correctly and then fails at milestone one the same way the first run did.

**Independent Test**: the second run of the US1 scenario. Milestone one's brief is a `TESTS` persona, no `IMPLEMENT` turn runs for it, and the commit subject is `test(#7): cover milestone 1`.

**Acceptance Scenarios**:

1. **Given** milestone one's scope names `src/m1.py` and `tests/test_m1.py`, `src/m1.py` present from a `feat(#7):` commit and `tests/test_m1.py` absent, **When** the workflow plans, **Then** milestone one is re-laned to `tests` and its record shows `lane: tests`.
2. **Given** the re-laned milestone's tests turn writes a passing test file, **When** the lane check runs, **Then** the milestone completes, the test file is committed, and no implementation turn is asked for.
3. **Given** the re-laned milestone's tests turn writes a test that fails against the existing code, **When** the lane check runs, **Then** the milestone fails with the existing tests-lane reason ("new tests fail against the existing code"), which is a finding about the previous run's code and not a red-observation failure.
4. **Given** a milestone whose source paths are present but were introduced by `main` rather than by this card's commits, **When** the workflow plans, **Then** it is not re-laned and runs as a feature milestone.

---

### User Story 3 - A tests turn that changes nothing over covering tests is not a failure (Priority: P2)

A milestone's tests are already present and passing because of a previous run, but the plan did not skip it (the run reached it through the loop rather than the resume prefix). Its tests turn changes no test file. The workflow records the milestone as satisfied by the tests already there rather than failing to observe red.

**Why this priority**: the narrow backstop for the live symptom, behind US1 and US2 because the plan-time skip catches the common case first.

**Independent Test**: drive `_red_phase` with a harness whose tests turn writes nothing, a workspace whose milestone test file is present from a prior-run commit, and a runner that passes it. The milestone completes with `satisfied_by: existing_tests` and no implementation turn.

**Acceptance Scenarios**:

1. **Given** a tests turn that changes no test file, the milestone's test paths present and introduced by this card's prior commits, and every one of them passing with none failing, **When** the red step evaluates, **Then** the milestone is recorded satisfied and the run continues to the next milestone.
2. **Given** a tests turn that changes no test file and the milestone's test paths absent, **When** the red step evaluates, **Then** the existing reprompt runs and the milestone fails as today.
3. **Given** a tests turn that changes no test file and the milestone's test paths present but not from this card's commits, **When** the red step evaluates, **Then** the milestone fails as today: pre-existing passing tests are not evidence that this milestone is done.

---

### User Story 4 - A vacuous tests turn still fails (Priority: P1)

The model writes a test that passes on the first run and nothing of ours is behind it. FR-005 is unchanged: one reprompt, then the milestone fails.

**Why this priority**: the resume rules exist to let a bounced card converge, not to let the test-first discipline lapse. A rule that cannot tell a resume from a vacuous test is worse than no rule.

**Independent Test**: the existing FR-005 scenario in the end-to-end file, unchanged and still passing, plus one new case on a branch that does carry this card's commits.

**Acceptance Scenarios**:

1. **Given** a fresh branch and a tests turn that writes a passing test, **When** the red step evaluates, **Then** the reprompt runs and the milestone fails with "red was not observed".
2. **Given** a branch that carries this card's prior commits, and a tests turn that writes a passing test for a milestone whose source paths are **not** on the branch, **When** the red step evaluates, **Then** the milestone fails exactly as in (1). Prior commits somewhere on the branch never excuse a vacuous test here.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The workflow reads the commit subjects and touched paths of `<base>..HEAD` once per run, trying `origin/<base_branch>` then `<base_branch>` then `origin/main` then `main` and using the first ref that resolves. A history that cannot be read leaves `prior_paths` empty and every rule below inert.
- **FR-002**: `prior_paths` is the set of repository paths touched by commits on that range whose subject matches `^(test|feat|fix|chore|refactor)\(#<issue_number>\):`, where `<issue_number>` is the card's. When the card has no issue number the set is empty.
- **FR-003**: A milestone's declared test paths and declared source paths are derived from its scope string by the same test-path convention the cycle rules use. A milestone whose scope names no test path can never be judged done or re-laned.
- **FR-004**: A milestone is **done** when every declared test path is in `prior_paths`, exists in the workspace, has at least one passing test anchored to it in the result set being judged, and has no failing test anchored to it.
- **FR-005**: A milestone is **tests_only** when it is not done, it declares at least one source path, every declared source path is in `prior_paths` and exists in the workspace, and at least one declared test path does not exist.
- **FR-006**: Every other milestone is **open**.
- **FR-007**: The resume decision runs after the baseline is captured, judged against the baseline's passing and failing test names. Only a contiguous prefix of **done** milestones is skipped. The first non-done milestone and every milestone after it run, in plan order.
- **FR-008**: A skipped milestone gets a record with `implementation_successful` true and `satisfied_by` `prior_run`, and no turn is run for it. It counts toward `milestones_completed`.
- **FR-009**: A **tests_only** milestone runs in the existing tests lane, with that lane's checks, commit subject and failure reason unchanged. Its `MilestonePlan.lane` is `tests` and its `lane_source` is `resume`.
- **FR-010**: `run_milestone` dispatches on the milestone's own lane rather than the run's lane, so a per-milestone re-lane takes effect. Every milestone a plan produces today already carries the run's lane, so this changes no existing behaviour.
- **FR-011**: When a tests turn completes and changes no test file, the workflow judges the milestone again with the same rule against the **current** test result set. **done** records the milestone as satisfied with `satisfied_by` `existing_tests` and runs no implementation turn. Anything else takes the existing reprompt path.
- **FR-012**: When a tests turn changes a test file, the red rule is unchanged. A passing new test still fails the milestone after one reprompt, whatever the branch history holds.
- **FR-013**: An empty remaining milestone list is a valid run: no turn is run and the workflow proceeds to the quality pass, the local gate, push, the pull request and the CI wait.
- **FR-014**: `milestones_planned` stays the full plan length. The run record carries `resumed_from_milestone`, the index of the first milestone actually run, or null when nothing was skipped.
- **FR-015**: Each rule in FR-002, FR-003, FR-004, FR-005, FR-006 and FR-007 is a pure function with its own test file carrying a named mutation that must make a test fail.
- **FR-016**: `Baseline` carries the failing test names the baseline run reported, so FR-004 can be judged at plan time.
- **FR-017**: One structured event per run records the resume decision: how many prior-run commits and paths were seen, the state assigned to each milestone, and the index resumed from.

### Key Entities

- **prior_paths**: the frozen set of paths this card's own earlier commits put on the branch. The single source of "a previous run did this".
- **resume state**: one of `done`, `tests_only`, `open` per milestone. Judged against the baseline at plan time and against the live result set after a no-op tests turn.
- **satisfied_by**: on a milestone record, `this_run` (the default, the milestone ran), `prior_run` (skipped at plan time) or `existing_tests` (a no-op tests turn over covering tests).

### Rule predicates (each pure, each with a named mutation)

| Function | Signature | Mutation that must break a test |
| --- | --- | --- |
| `prior_run_paths` | `(entries, issue_number) -> frozenset[str]` | drop the issue-number match from the subject pattern, so another card's commits count |
| `declared_test_paths` | `(scope) -> list[str]` | return every scope segment instead of only the test paths |
| `declared_source_paths` | `(scope) -> list[str]` | return every scope segment instead of only the non-test paths |
| `tests_cover` | `(test_paths, present, passed, failed) -> bool` | drop the failing-name check, so a file with one failing test counts as covering |
| `resume_state` | `(milestone, prior_paths, present, passed, failed) -> str` | drop the `prior_paths` membership check, so tests that came from main count as a previous run's |
| `apply_resume` | `(plans, states) -> (skipped, remaining)` | skip every `done` milestone instead of a contiguous prefix, so a hole in the middle is skipped |
| `present_paths` | `(workspace, paths) -> frozenset[str]` | drop the inside-the-repository guard, so an absolute or traversing scope segment resolves outside the clone |

The rules live in a new `resume.py` beside the cycle rules rather than in
`plan.py`: the ``done`` predicate is judged against the baseline, which the plan
step does not yet have, so `build_plan` stays a pure function of the card and
the brief and the resume decision runs as its own step after the baseline.

## Success Criteria *(mandatory)*

- **SC-001**: The end-to-end scenario runs the workflow twice against one real clone with a harness that over-implements milestone one during milestone zero. Run one reports `partial_progress`. Run two skips milestone zero, runs milestone one in the tests lane, and reports `pr_opened`.
- **SC-002**: Every existing implementer test passes unchanged, including the FR-005 vacuous-tests scenario.
- **SC-003**: Each of the six rule predicates has a test file whose header names its mutation, and each named mutation, applied in the real tree, makes at least one test in that file fail.
- **SC-004**: A first run on a fresh branch produces the same brief sequence, the same commits and the same record as before this spec, with `resumed_from_milestone` null.
