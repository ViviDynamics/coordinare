# Research: Implementer Test-First Workflow with a Verified Hand-off

All decisions below were resolved by reading the code on branch `167-implementer-tdd-workflow` (based on main `c0e4bdf` which contains specs 164, 165, and 166) and by examining the 164 role-workflow layer, the 089 local test gate, the 165 push-path changes, and the 090/098/113 CI state polling patterns.

## R1. Turn primitive and agent_turn_runner in the Toolkit

**Decision**: The implementer workflow drives harness turns through a `run_agent_turn(brief: TurnBrief) -> TurnResult` method on the `Toolkit` class. `TurnBrief` is a dataclass with fields: `kind` (literal "tests", "implement", "repair"), `milestone_goal`, `scope`, `done_when`, `forbidden_paths: list[str]`, and `failing_output` (the raw failure excerpt when kind=="repair"). `TurnResult` is a dataclass with `exit_state` ("success" or "timeout" or "failed"), `output_tail: str`, `files_changed: dict[str, str]` (path -> change kind, e.g. "added", "modified"), `wall_time_ms: int`, and `harness_commits: list[str]` (SHAs of any commits the harness made). 

The runner is injected into the Toolkit at construction time as an `AgentTurnRunner` callable `(stand: Stand, score: Score, brief: TurnBrief, model: str, workspace: Path, wall_clock_ms: int) -> Awaitable[TurnResult]`. In production, `build_production_toolkit` in `workflows/adapter.py` (~line 149) constructs this from the role's configured backend adapter (lines 226-228 already inspect `workflow_name` and branch behavior; the implementer adds a third branch that instantiates the backend adapter from `settings.AGENT_BACKEND` the way `main.py` does at lines 164-173, calls `adapter.start(stand, score, model=settings.AGENT_MODEL, ...)` with a per-turn persona derived from personas.py, waits on `get_status()` until done or wall-clock expiry, then computes file changes via `git diff` and `git status` against the turn-start SHA and cleans up with `adapter.stop()`). The per-turn score passed to start() carries a modified `persona_instructions` field holding the turn persona; all other score fields are unchanged.

The adapter is wrapped in a `BackendRunner` helper (new, parallel to how 165 wraps the blueprint deploy test) that orchestrates the start/poll/stop lifecycle with wall-clock enforcement and exception handling (turning `TimeoutError` and `RuntimeError` from the backend into a "timeout" exit state).

**Rationale**: 164 FR-007 requires the workflow to own the loop, not the model. The turn brief must be narrow (one milestone, exact forbidden paths, fresh failing output) so the harness explores only what is relevant. The agent_turn_runner is a protocol, not a method, so tests can stub it with a scripted fake harness. The per-turn persona allows each turn to have a distinct system message (e.g. "write only the test file" vs "make these tests pass") without model confusion.

**Alternatives considered**: Calling the backend directly from the workflow (rejected: every adapter has a different protocol and start/stop/get_status dance, so a test harness would need N implementations). Storing the turn persona on score and having main.py or the adapter select it (rejected: the workflow needs to change the persona per turn, but main.py is not part of the workflow and the adapter is not a workflow step).

## R2. How the performer detects red and green via test command and summary parsers

**Decision**: The baseline and per-turn test status are detected by running the role's detected (or declared) test command via `Toolkit.run_command()` and parsing the output with format-specific summary parsers. The `CIDetectionResult` from `src/coordinare/services/ci_detection.detect()` (lines 191-208) already returns `test_command: str | None` for pytest, rspec, jest, npm test, make test, etc. When `test_command` is None, the workflow fails before the first turn with an environment-blocked hold (FR-004). 

The parser is selected by the detector's `stack` field: "python" -> pytest, "ruby" -> rspec, "node" -> jest, "make" -> generic (lines 79-188 of ci_detection.py). Each parser extracts a list of test names (when the runner supports it, e.g. pytest with `--collect-only -q`) or pass/fail counts (when not). The baseline is recorded as a `Baseline` object (defined in data-model.md) containing either `test_names: list[str]` or `pass_count, fail_count: int`. A test file is identified by convention: paths matching `**/test_*`, `**/*_test.*`, `**/*.spec.*`, `**/tests/**`, `**/spec/**` for the detected stack. Red is "at least one changed test file failed and every baseline test still passed"; green is "every baseline test still passed and every milestone test passes". The red/green detection is a pure function in a new module `workflows/implementer/baseline.py` with its own test, and is mutation-tested (Constitution II).

**Rationale**: The spec-089 local test gate already does this (main.py lines 2817-2869); the implementer workflow reuses the exact same parsers so the signal is consistent. The convention-based test file detection is simple and covers all four major runtimes. The "changed file" check uses git status/diff output, which is already computed for the report anyway.

**Alternatives considered**: Asking the model to list the new tests (rejected: the model can lie, and the harness commits can make the list stale). Parsing CI check run logs (rejected: that is remote state, not local state, and the loop must be fast).

## R3. Squashing harness commits and reverting out-of-scope paths

**Decision**: After every turn that produces files (tests or implementation), the harness may have made commits (despite a persona instruction forbidding it). Before the check runs, `git reset --soft` moves all commits back to the index: `git reset --soft <turn-start-SHA>`, then `git add --` the in-scope paths and `git commit -m <step-message>` makes one workflow commit. Out-of-scope changes (documentation tree edits when the harness is not the documenter, source edits during a tests turn) are identified by diff against the turn-start SHA and reverted: `git checkout -- <path>` for tracked files, `git rm <path>` for newly added files in the doc tree, `rm -f` for untracked files. The reverts happen BEFORE the check runs so the check sees the cleaned tree. The reverted paths are recorded in the run record for debugging.

The sequence for a tests turn is: (1) turn starts at SHA A (2) harness writes test files and makes commits to SHA B (3) `git reset --soft A` (4) `git add -- **/test_*` (5) `git checkout -- src/` (delete any source edits) (6) `git rm` any doc tree files added (7) `git commit -m "Tests for milestone X"` -> SHA C. A subsequent implementation turn starts at SHA C.

**Rationale**: 164 FR-007 requires one commit per step, written by code. The spec-165 push path rebase (lines 1123-1178 of workspace.py) works commit-by-commit, so squashing must happen before push. Out-of-scope edits are caught early (before the check, so they do not pollute the report) and recorded so the operator knows the harness explored beyond its remit.

**Alternatives considered**: Telling the harness not to commit via persona (rejected: personas can be misunderstood, and the squash is a mechanical guarantee). Merging the commits post-push (rejected: the PR history is wrong by then).

## R4. Quality pass: detected lint plus declared quality commands

**Decision**: After green implementation, the workflow runs the detected lint command (from `ci_detection.lint_command`, e.g. "ruff check ." or "bundle exec rubocop") and then each command listed in `score.workflow_env.get("QUALITY_COMMANDS", "")` (newline-separated). The first command that exits non-zero triggers a repair turn with that command's output in the `failing_output` field of the brief. After the repair turn completes (and milestone tests pass), the workflow re-runs the entire quality set from the top. At most two repairs per milestone (FR-010). If the third repair fails, the run ends with an environment_blocked hold naming the failing command.

The workflow_env is read from the dispatch payload, paralleling how the QA workflow reads `score.verification_brief` from the architect (lines 41-64 of qa/plan.py). Quality commands are opt-in: if QUALITY_COMMANDS is not set, only the lint command runs.

**Rationale**: Many repositories have a `make ci` or `npm run quality` that runs multiple checks in sequence. Declaring them allows the performer to give the model the exact sequence and outputs, which is faster than the model discovering them. The detected lint is always first (standard convention), so lint failures are consistent with coordinare's own lint gate.

**Alternatives considered**: Asking the model to discover quality commands (rejected: adds latency and the model may guess wrong). Running all commands and returning all failures at once (rejected: a repair turn addresses one failure at a time, and the first failure is often the real issue).

## R5. Local gate reuse and terminal outcomes

**Decision**: After the quality pass completes, the existing spec-089 local test gate runs unchanged (main.py lines 2815-2869). It is applied once, after quality, as the last check before push; if it fails the workflow ends with a changes_requested hold (not a repair turn, since local failures are not repairable in the workflow context and coordinare already handles them). If the local gate detects an environment-blocked condition, the workflow ends with an env_blocked hold, and coordinare does not re-dispatch the agent (spec 089 US2). 

Terminal outcomes map to coordinare's existing statuses: `pr_opened` (all green, handed off), `changes_requested` (local gate failed, not pushed), `partial_progress` (milestone failed or stuck, branch at last green, not pushed), `env_blocked` (environment signal or pending CI, hold on the current card). The prose path (no workflow: implementer) is byte-for-byte unchanged in main.py: the implementer role still reaches the local gate, and the gate's failure path is identical.

**Rationale**: The local gate is already the pre-push filter (spec 089). Reusing it (rather than reimplementing the environment detection) keeps the barrier between the workflow and main.py small. The terminal statuses are coordinare's existing ontology; no new state is needed.

**Alternatives considered**: Implementing the environment detection in the workflow (rejected: it is complex and maintained in main.py; duplication is a bug). Retrying the local gate after a repair (rejected: local failures are not code bugs, so a repair turn does not apply).

## R6. CI polling and log excerpt retrieval

**Decision**: After push and PR open, the workflow calls the existing `github.get_check_runs()` and `github.summarise_check_runs()` helpers (performer/github.py lines 62-127 and 159-185) in a poll loop with a 30-minute budget (FR-013, provisional per FR-016). On the first polling cycle after push, checks may be queued; the workflow waits. Once any check fails, `github.get_check_run_logs()` is called for each failed job (lines 108-157) with `max_chars=4000` to fetch the log excerpt. The log excerpt is placed in the repair brief's `failing_output` field. 

If the same set of failing checks appears on two consecutive polls (names matched by check name or job name), the workflow classifies it as "no progress" and ends the run (FR-013). The implementation stores the prior-poll failing check names in the run record and compares on the next poll.

The GitHub token passed to these helpers comes from `score.effective_github_token` (which is already configured by coordinare in the dispatch payload). No new token handling is needed.

**Rationale**: The existing GitHub helpers are proven (used by the QA workflow and main.py's CI polling for 075). Fetching logs is the performer's only new GitHub surface; the log endpoint is a standard REST endpoint and does not require new scopes beyond what coordinare already provides. The 4000-character tail is enough for a typical error message and fits in a model context window.

**Alternatives considered**: Asking coordinare to fetch the logs (rejected: the performer is already talking to GitHub for check status, so one more call is natural). Summarizing logs with a model call (rejected: that consumes turn budget and adds latency).

## R7. Backend adapter wiring and per-turn persona assembly

**Decision**: The production agent_turn_runner is built in `build_production_toolkit` (workflows/adapter.py ~149) by (1) selecting the backend from `settings.AGENT_BACKEND` (the same enum used in main.py lines 164-173), (2) instantiating it with `BackendAdapter()` (e.g. `ClaudeCodeBackend()` or `JunieBackend()`), (3) creating a new `Score` with `persona_instructions` set to the turn persona from `workflows/implementer/personas.py` (new module, defines TESTS and IMPLEMENT personas for the implementer role, plus REPAIR persona for both tests and implementation repairs), and (4) calling `adapter.start(stand, score, model=settings.AGENT_MODEL)` followed by a poll loop on `get_status()` that breaks when state=="done" or wall-clock exceeds the budget. On wall-clock expiry the adapter is `stop()`-ed and the exit state is "timeout". The model name can be overridden per-role via `settings.AGENT_MODEL`, following the existing pattern for other roles.

The per-turn Score modification is shallow: only `persona_instructions` changes, all other fields (including `model`, `max_tokens`, `workspace_path`, `github_token`) are inherited from the caller's score.

**Rationale**: Every role already has an adapter wiring in main.py (lines 164-180). Reusing it here (via a helper function in adapter.py) keeps the adapter selection logic in one place. The Score modification is minimal: a shallow copy with one field changed keeps the turn execution close to the existing adapter protocol.

**Alternatives considered**: Passing the persona as a separate argument to adapter.start() (rejected: the adapter protocol does not include persona; it is already part of `score.persona_instructions`). Building a special "turn runner" adapter that is not a BackendAdapter (rejected: every BackendAdapter implementation would need a parallel TurnAdapter; inheritance or composition is more debt).

## R8. Evaluation fixtures and live mode testing

**Decision**: Five fixtures (spec requirement SC-006) are created as temporary repositories with bare remotes and scripted fake harnesses:

1. `single`: one milestone, simple tests and implementation, quality command passes first time, CI checks pass first time. Tests the full happy path in under 5 minutes.
2. `two_milestones`: two milestones, same as single but repeated. Tests milestone sequencing and baseline carry-forward.
3. `vacuous`: tests that all pass without any implementation. Tests the reprompt gate and milestone failure.
4. `stuck`: tests that never pass after three implementation attempts. Tests attempt exhaustion and partial progress reporting.
5. `ci_pending`: like single but CI checks stay pending past the wait budget. Tests the pending hold outcome.

Each fixture is a real git repository with a bare remote, a working tree, and a `conftest.py`-style fake `agent_turn_runner` that edits files and makes commits based on `turn.kind` and a hardcoded script (e.g. for `stuck`, the runner ignores the failing output and writes code that does not address the failures). Tests verify: commit order, turn count, report shape, attempt counts, final state.

In live mode (`--live` flag passed to the eval), the fake runner is swapped for the real adapter and the gateway model, running in a performer container. The live fixtures are run on demand only (not in CI) and their results are logged against the performance budgets in the spec table. The deterministic CI fixtures run in `tests/eval/implementer_scenarios/` as unit tests.

**Rationale**: The workflow is the implementer's loop; fixtures with a scripted harness can test the loop logic in isolation without a real model or container. Live mode measures performance and catches latency regressions. Deterministic eval is Constitution II (tests must be deterministic and testable in CI).

**Alternatives considered**: Only live mode fixtures (rejected: live mode is slow and requires a gateway, so catching logic bugs in CI is vastly cheaper). Only deterministic fixtures (rejected: performance and real-model integration are critical for the hand-off property).

---

## R-a. Turn persona shape and assembly

**Decision**: Each turn kind has a distinct system message assembled in `workflows/implementer/personas.py`:

- `TESTS`: "Write only test files for this milestone. Do not write or edit source code or documentation. When done, commit with message '[TESTS] <milestone_goal>'. Your tests should fail without the implementation. If the tests pass without code, the implementer will reprompt."
- `IMPLEMENT`: "Make exactly these tests pass: <failing_test_names or counts>. Do not write tests or documentation. When done, commit with message '[IMPLEMENT] <milestone_goal>'. Do not break any baseline tests."
- `REPAIR_TESTS`: Like TESTS but after a reprompt: "These tests passed without implementation; make them fail for the right reason by changing the test logic."
- `REPAIR_IMPLEMENT`: Like IMPLEMENT but after a failure: "These tests are still failing. Here is the failure output: <failing_output>. Make exactly these tests pass. Do not add new tests or documentation."
- `REPAIR_QUALITY`: "This quality command failed: <command>. Fix the reported issues. Do not add tests or documentation. Command: <command>. Output: <failing_output>."
- `REPAIR_CI`: "This CI check failed: <check_name>. Fix the reported issues. Do not add tests or documentation. Log excerpt: <failing_output>."

Each persona is a string template with `<placeholders>` filled at runtime from the brief. No markdown formatting; the harness receives plain text that is never shown to a human.

**Rationale**: Clear, distinct personas keep the harness focused. Forbidding documentation in every persona enforces spec 165's "no documentation from the implementer" constraint. The reprompt personas acknowledge the prior attempt and redirect.

## R-b. Terminal status mapping and PR state

**Decision**: The performer's final state is reported as one of coordinare's existing statuses:

- `pr_opened`: CI green, handed to reviewer. `perf.state = "done"`, report contains run record with `pr_opened: true`.
- `changes_requested`: local gate failed (not pushed). `perf.state = "changes_requested"`, reason is gate failure, report contains run record with partial progress if a milestone was stuck, else the gate failure.
- `partial_progress`: milestone failed (red never turned green, or stuck on three attempts) or a repair exhausted. `perf.state = "partial_progress"`, reason is milestone name + last failure, report contains run record with `next_focus` set to that milestone.
- `env_blocked`: environment hold (no test command, local gate environment signal, or CI pending past timeout). `perf.state = "env_blocked"`, reason names the condition, report contains run record with that reason.

These map directly onto coordinare's existing `check_board` and `dispatch_performer` paths (dispatch_performer.py ~line 1620 matches on state). When the workflow is off (`workflow: implementer` not set), the performer's final state is determined by main.py's post-processing as today (prose path, byte-for-byte unchanged).

**Rationale**: No new coordinare states are needed; the workflow uses the existing state machine. The report carries the run record for observability and metrics (SC-001, SC-005).

## R-c. Squash and revert sequence, with order guarantees

**Decision**: The commit squash happens in this order:

1. `git reset --soft <turn-start-SHA>` (move all commits to the index; does not touch the working tree)
2. For a tests turn: `git checkout -- src/ app/ lib/ ...` (any source directory, per stack) (revert harness source edits)
3. For any turn: `git rm -r docs/` (if present, revert documentation edits added by the harness)
4. `git rm -f` any newly-added doc files (both in and out of the doc tree, if they are not in the in-scope set)
5. `git add --` the in-scope paths (test files for tests turns, source + test files for implement turns)
6. `git commit -m <step-message>`

The reverted paths are recorded in `RunRecord.scope_reverts: list[dict]` with fields `path`, `kind` (e.g. "reverted_source", "reverted_doc", "removed_untracked"), and a brief reason.

**Rationale**: The sequence is deterministic (always revert source first, then docs). Recording reverts ensures the operator knows the harness stepped out of line, for debugging and for training future harnesses.

## R-d. Log excerpt endpoint and scope

**Decision**: Log excerpts are fetched via the GitHub REST API: `GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs`. This is a standard endpoint (performer/github.py line 138) that returns the full log as plain text. The endpoint is public (no special scope needed beyond `repo`), and the performer already has the token (score.effective_github_token, a classic PAT or fine-grained token with `actions:read` scope from coordinare). The response follows redirects (lines 142-147 of github.py) and returns up to 4000 characters from the tail of the log (line 154-155). No additional scope is required.

**Rationale**: This is an existing endpoint used in other specs (e.g., for artifact log review). No new GitHub surface is needed, and the token coordinare provides already has the needed scope.

**Alternatives considered**: GraphQL query for check run details (rejected: the REST endpoint is simpler and does not need a second API). Summoning the GitHub UI to view logs (rejected: the performer cannot open browsers).

## R-e. Coordinare's stall watchdog versus a 20-minute harness turn

**Decision**: `run_agent_turn` forwards the inner harness adapter's events and token growth to the outer WorkflowAdapter status while a turn is running, and the outer `get_status` reports `progress` as `<state>:<milestone>:<turn kind>:<attempt>` plus the inner adapter's own progress text. The default turn wall clock (20 minutes) is documented as needing `stall_timeout_seconds` at or above it, or the forwarding above.

**Rationale**: the 077 stall watchdog in `src/coordinare/graph/nodes/monitor_performer.py` (near the comment "077 stall watchdog", around line 3706) kills a turn that is still `working` but shows no new events and no token growth for `stall_timeout_seconds` (the live config sets 900). The 164 `WorkflowAdapter.get_status` (workflows/adapter.py, around line 274) reports only the current step name as progress and no events while a step runs, so a quiet 20-minute implementation turn would look wedged at 15 minutes and be killed mid-turn. Forwarding the inner adapter's events and token counts is what codex and claude_code already do for a prose run, so the watchdog keeps its meaning (a hung upstream read) and a working turn is never mistaken for one.

**Alternatives considered**: capping the turn wall clock under the stall timeout (rejected: real implementation turns on the live fleet take longer than 15 minutes and the cap would be the binding constraint); disabling the watchdog for the implementer role (rejected: it exists precisely for hung upstream reads, which a harness turn can suffer too).
