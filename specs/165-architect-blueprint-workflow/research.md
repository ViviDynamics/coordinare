# Research: Architect Role Workflow with a Blueprint Hand-off

All decisions below were resolved by reading the code on branch `165-architect-blueprint-workflow` (based on main `f9f3bb7`) and from measurements taken on the live website run of 2026-09-06.

## R1. Where the workflow lives and how the survey is bounded

**Decision**: a second consumer of the spec-164 layer at `agent/performer/src/performer/workflows/architect/`, registered as `"architect"` in `SUPPORTED_WORKFLOWS`, run through the existing `WorkflowAdapter` and `build_production_toolkit`. The survey step asks the model for a list of commands and runs each through a code-owned allow-list: the first token must be one of `ls`, `cat`, `head`, `tail`, `sed` (only with `-n` and a range), `rg`, `grep`, `find` (without `-exec`, `-delete`), `wc`, or `git` with a read-only subcommand (`log`, `show`, `diff`, `ls-files`, `status`, `blame`). Pipes are allowed only between allow-listed commands. Anything else is refused, recorded, and still consumes budget. Output is truncated to 4000 characters per command; the budget is 12 commands.

**Rationale**: the live rollout showed 55 tool calls, `bundle install`, file writes and `sleep` inside an architect round. A persona cannot forbid that reliably (the persona already does); an allow-list enforced before execution can. The 164 toolkit's `run_command` already exists; the allow-list wraps it.

**Alternatives considered**: a read-only sandbox mount (the container runs `danger-full-access` for codex; changing that is a harness change, out of scope); a turn cap on codex (does not stop writes within the allowed turns). The allow-list is the smallest change that makes the property testable.

## R2. The blueprint carrier

**Decision**: the architect workflow's report is `PerformerResponse.report = {"blueprint": {...}, "size": "small" | "large", "workflow_metrics": {...}}`. Coordinare's `monitor_performer` lifts `report["blueprint"]` and `report["size"]` into a new `PersistedSession.blueprint` (schema version 16 -> 17, backward compatible default `None`), replacing any prior value for the card. `dispatch_performer` derives three projections at dispatch time and places them in `card_context` as `implementation_brief`, `documentation_brief`, `verification_brief`, each registered in `specs/contracts/dispatch-payload.md` and declared on `Score` (the `extra="ignore"` trap from 164).

**Rationale**: `qa_findings` established the pattern (lift in monitor, inject in dispatch, contract row, Score field). One difference matters: `qa_findings` lives only in graph state and does not survive a daemon restart, which is acceptable for a per-bounce brief but not for a blueprint that drives the rest of the lifecycle. Hence the persisted field.

**Alternatives considered**: committing `blueprint.json` to the branch (rejected by the user: committed context drifts); carrying the whole blueprint to every role (rejected: readers must not see each other's slices, and payload size).

## R3. The documenter side-stage

**Decision**: the lifecycle stays strictly linear (`lifecycle_sequence`, one `performer_stage`, one `agent_dispatch` per session; `_advance_stage` walks the list). The side-stage is therefore not a lifecycle stage. It is an out-of-lifecycle dispatch following the env-bootstrap precedent (`env_cache.check_and_trigger` dispatches a performer that no card session owns). A `DocumentingSideRun` record on the session (`status`, `dispatched_at`, `blueprint_hash`, `head_sha`, `result`) tracks it; `check_board` triggers it once per blueprint hash when the documentation brief is non-empty and the card has advanced past architecting, and monitors it on subsequent cycles. It never blocks or advances the main lifecycle. The existing end-of-lifecycle documenting stage is untouched (SHA-gated, spec 125).

**Rationale**: a concurrent second stage on one session would require changing the single-dispatch model everywhere (`_advance_stage`, slot accounting, monitor). The env-bootstrap path already runs a performer beside the lifecycle with its own state and completion handling.

**Push safety (required for this decision)**: `workspace.push_branch` today tries a plain push and falls back to `git push --force` on any failure, including a non-fast-forward. Two performers on one branch would let the loser overwrite the winner. Change: before pushing, `git fetch origin <branch>` and `git rebase origin/<branch>` when the remote branch exists; push without force; force only when the remote branch does not exist yet (first push). A rebase conflict fails the performer with a named reason. This applies to every role, not only the documenter, and is covered by its own tests. The documenter additionally refuses to commit any path outside the symphony's documentation tree (default `docs/`).

**Alternatives considered**: the documenter on its own branch and PR (rejected: two PRs per card, and the wiki would lag the code PR); serialising documenter after implementer (rejected: that is the status quo with extra steps).

## R4. Personas and the prose path

**Decision**: when `workflow: architect` is set, the architect persona is not used; the workflow has its own step personas (survey, blueprint) in `workflows/architect/personas.py`. The `main.py` architecting post-processing (which commits `plan.md` and `tasks.md`) is skipped when the response carries a `report["blueprint"]`; the prose branch stays byte-for-byte for the non-workflow path. The implementer persona gains a brief-aware per-turn procedure: when `implementation_brief` is present, milestones come from it and the "read plan.md and tasks.md" instruction is replaced; a new Forbidden line says the implementer creates and edits no documentation. When `implementer_single_turn` is set the PARTIAL_PROGRESS instructions are omitted.

**Rationale**: 164 FR-005 (no-workflow path unchanged) is the standing promise, and the tests that pin it are extended to the architect role.

## R5. Sizing rule

**Decision**: `size = "small"` when `len(milestones) <= 1 and not data_model.changes and not interfaces`, else `"large"`. Pure function in `workflows/architect/size.py`, applied by code after validation; the model never sets it. Small drives `implementer_single_turn: true` in the implementer's card_context. An empty `docs` list means no side-stage regardless of size.

**Rationale**: the only sizes that change behaviour today are "one turn" versus "milestone loop" and "documenter or not". Two classes are enough; thresholds are documented and tested.

## R6. QA consumption

**Decision**: `workflows/qa/plan.py` `run_plan_step` reads `score.verification_brief["criteria"]` when present and passes them as the criteria list, marking the plan `criteria_source="blueprint"`; the prompt tells the model the criteria are fixed and it plans checks for them. Absent a brief, behaviour is unchanged (`acceptance_criteria` from the card).

## R7. Budgets and timeouts

**Decision**: blueprint call 8000 tokens with one doubled retry; survey proposal call 3000 (it returns a short list); read timeout 900 s; survey 12 commands at 4000 characters. From #264's measurements: a plan-shaped prompt with reasoning needs roughly 3500 tokens before the answer; 12k tokens of prompt prefill costs about 19 s, 50k about 39 s, so the survey cap keeps the blueprint prompt near 20k tokens.

## R8. Evaluation

**Decision**: `tests/eval/architect_scenarios/` with three generated fixture cards (trivial copy fix, mid-size feature, schema plus interfaces) and a stubbed model that answers from canned files, so the eval is deterministic and runs in CI as unit-level tests; a separate `--live` mode uses the gateway and is not a CI gate (Constitution II). Scoring: size class, no side effects (executed-check record), slice contents, and for the live mode a qualitative rubric.

## R9. Failure handling

**Decision**: a hollow or invalid blueprint raises inside the workflow; `WorkflowAdapter.get_status` reports `state="error"` with the reason; coordinare's existing system-error path retries and, on exhaustion, blocks. Per #263, this is never surfaced as a "Needs input" question; that fix is tracked there and this spec depends on it only for the wording of the block.
