# Tasks: The advocate and the curator become performer runs

**Feature**: `173-advocate-curator-performers`
**Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Worktree**: `~/Workspaces/ViviDynamics/coordinare-173`

Tests are required (Constitution II). Every gate rule gets a test shown to fail
under one named mutation applied in the real tree, with the file hash checked
after restore. The two suites run in SEPARATE invocations.

## Phase 1: Setup

- [X] T001 Create the two workflow package skeletons `agent/performer/src/performer/workflows/advocate/__init__.py` and `agent/performer/src/performer/workflows/curator/__init__.py`, and the test package dirs `tests/unit/workflows/advocate/` and `tests/unit/workflows/curator/`, each with the shared `_fakes.py` providing a fake GitHub (issues, labels, comments, board items) and a recording poster

## Phase 2: Foundational (blocks every user story)

- [X] T002 [P] Add `advocate_complete` and `curation_complete` to the `PerformerStatusType` literal in `agent/performer/src/performer/protocol.py`, leaving `FAILURE_STATUSES` untouched; test that both are in `TERMINAL_STATUSES` and in neither failure set
- [X] T003 [P] Add `project_id: str = ""` to `Score` in `agent/performer/src/performer/models.py`; test it survives a dispatch payload round trip and that `extra="ignore"` does not drop it
- [X] T004 [P] Register `project_id` in the field registry table of `specs/contracts/dispatch-payload.md` and extend the registry contract test in `tests/contract/` so a dispatch-injected field absent from `Score` still fails
- [X] T005 [P] Add `add_labels` (`addLabelsToLabelable`) and `add_item_to_project` (`addProjectV2ItemById` then set the status field) to `agent/performer/src/performer/github.py`, following the existing GraphQL POST call sites; tests in `agent/performer/tests/unit/test_github_labels_and_project.py` covering success, a GraphQL `errors` body, a non-success HTTP status, and an empty `project_id` returning a refusal rather than a silent no-op
- [X] T006 [P] Add `list_open_issues(owner, repo, token, *, first, max_pages)` to `agent/performer/src/performer/github.py`, returning each issue's node id, number, title, body, url and labels, paged in the shape `fetch_review_threads` uses. **The performer has no issue-listing call today** (`grep -c "issues("` on that module returns 0), so without this neither role can see anything to work on. Tests: paging, an empty repository, a GraphQL `errors` body, and a non-success HTTP status
- [X] T007 [P] Register `"advocate"` and `"curator"` in `SUPPORTED_WORKFLOWS` in `agent/performer/src/performer/workflows/__init__.py` and in `KNOWN_WORKFLOWS` in `src/coordinare/config.py`; add `advocate_classify` and `curator_judge` step budgets in `agent/performer/src/performer/workflows/budget.py`; test both registries agree, which is the drift a paired registry exists to catch
- [X] T008 Add the per-role fields to `EnvCacheState` in `src/coordinare/models/env_cache.py` (`<role>_in_flight` transient, `<role>_attempts`, `<role>_exhausted`, `last_<role>_run_at`, `last_<role>_succeeded`, `last_<role>_error`, `last_<role>_issues_seen`) and mirror the persisted subset onto `EnvCacheStateSnapshot` in `src/coordinare/state_store.py`, bumping `CURRENT_SCHEMA_VERSION` 19 to 20; test that a v19 snapshot loads with defaults and that `<role>_in_flight` is absent from the snapshot model
- [X] T009 [P] Trim `AdvocateConfig` in `src/coordinare/config.py` (remove `scoring_models`, add `scan_interval_seconds`) and add `CuratorConfig` with its `github_repo`-required-when-enabled validator and its `backlog_column` must-not-be-the-dispatch-column validator; tests for both validators, each with a named mutation
- [X] T010 [P] Add the shared per-role gate helpers in `src/coordinare/services/intake_dispatch.py`: `should_run` (enabled, not in flight, not exhausted, cooldown elapsed), `register_failure` (increment, trip the breaker at the bound), `register_success` (reset), each a pure function over `EnvCacheState`; tests with one named mutation per rule

**Checkpoint**: both registries agree, the schema loads old snapshots, and the gate helpers are proven before anything dispatches.

## Phase 3: User Story 1 - The advocate answers from documentation it can prove it read (P1)

**Goal**: the advocate runs in a performer, never answers what it cannot ground, and the in-daemon path is gone.

**Independent test**: enable the advocate, open one answerable and one unanswerable issue, run a cycle. The first is answered citing a real document; the second is escalated. Coordinare makes no classification call.

- [X] T011 [P] [US1] `models.py` for the advocate package per data-model.md (`IssueCandidate`, `DocumentRead`, `Classification`, `IssueOutcome`, `AdvocateRecord`) plus the guarded response schema, which must forbid a verdict key; test the schema against `contracts/advocate-record.schema.json` and test that the two definitions do not drift
- [X] T012 [P] [US1] `docs.py`: read each configured path from the checkout, record `path`/`content`/`read`, tolerate a missing file, and fetch a non-default documentation branch explicitly; tests including a missing file and a non-default branch
- [X] T013 [US1] `intake.py`: fetch the open issues, and skip any already carrying the handled or escalation label, so "already handled" is read from durable evidence on the issue and never from memory (FR-010, FR-016). Tests: an already-handled issue is skipped, an unlabelled one is kept, and the skip is proven by a named mutation
- [X] T014 [P] [US1] `triage.py`: the sensitive-keyword rule, evaluated before any model call; tests with a named mutation, plus a test asserting zero model calls on a keyword match
- [X] T015 [US1] `classify.py`: one schema-guarded call over the unhandled candidates, with the persona passed as instruction and the documentation as a separate value; test that no persona text appears in the documentation value, with a named mutation putting it back
- [X] T016 [US1] `gate.py`: discard a judgement about an unsent issue; require at least one citation; require every cited document to be one read with `read=True`; anything failing escalates. Tests with one named mutation per rule
- [X] T017 [US1] `act.py` and `personas.py`: apply the label before posting the comment, post the reply or the escalation, raise the notification, preserve every existing escalation reason; tests covering all five outcomes and the label-before-comment ordering with a named mutation
- [X] T018 [US1] `report.py` and `AdvocateWorkflow.run` in `agent/performer/src/performer/workflows/advocate/__init__.py`: the step sequence, the executed `git status --porcelain`, step timing and events
- [X] T019 [US1] Add the advocate terminal branch to `handle_status` in `agent/performer/src/performer/main.py`, keyed on `report["advocate"]` with a required verdict, placed BEFORE the shared implementer tail; tests in `agent/performer/tests/unit/test_advocate_report_path.py` including the shared-tail trap: assert the run pushes no branch and opens no pull request
- [X] T020 [US1] Add the advocate decision and dispatch to `src/coordinare/services/intake_dispatch.py` and `src/coordinare/daemon.py`, following `_execute_wiki_init_dispatch`: hand-built `card_context` whose id key is `id` and NOT `card_id` (the transport reads `card_context["id"]` at `http_performer_service.py:823,1026`; nothing reads `card_id`, so the wiki-init precedent's synthetic id is silently dropped and reaches the wire as an empty string), `WorkspaceInfo(path=None, ...)`, direct `svc.dispatch_card`, the in-flight marker set BEFORE the call and rolled back in the `except`; test the ordering with a synchronous-raise fake, mutating the order to prove the test catches it
- [X] T021 [US1] Add the poll task and completion handler: clear the marker, write the outcome, reset or trip the breaker, and call `snapshot_save_fn`; test that the flush happens, with a named mutation removing it
- [X] T022 [US1] End-to-end scenarios in `tests/unit/workflows/advocate/test_workflow_end_to_end.py` against the fake GitHub: answerable, unanswerable, hallucinated citation withheld, sensitive keyword with zero calls, already-handled skipped, judgement for an unsent issue, unreadable documentation, and a run that writes nothing
- [X] T023 [US1] DELETE `src/coordinare/services/advocate.py`, `src/coordinare/services/scoring.py`, `src/coordinare/graph/nodes/advocate.py`, the `advocate_scan` node registration and its `START` edge in `src/coordinare/graph/builder.py` (rewiring `START` to `route_issue_comments`), the `advocate_history` state key and its fanout merge entry, and every test file that covered only the deleted path; verify with a repository-wide grep that no reference survives
- [X] T024 [US1] Regression test in `tests/integration/test_cardless_runs.py` for FR-027 and SC-008: with both roles unconfigured, no run is dispatched, no issue is touched, and the graph runs `START` straight to `route_issue_comments`. This is the claim that the deletion changed nothing for a deployment that never enabled the role, and unlike specs 164 to 172 there is no flag guarding a preserved path, so it needs a real test rather than a flag assertion
- [X] T025 [US1] Restart test in `tests/integration/test_cardless_runs.py` for SC-005: complete a run, restart between it and the next cycle, and assert no second reply is posted. The marker that prevents it is the issue's label, so the test must prove the decision survives an empty in-memory state
- [X] T026 [US1] `tests/eval/advocate_scenarios/` (five fixtures: answerable, unanswerable, hallucinated citation, sensitive keyword, already handled) plus `src/coordinare/eval/advocate_scenarios.py` registered in `tests/eval/test_scenario_runners.py`

**Checkpoint**: the advocate works end to end in a performer and the old path is gone, not dormant.

## Phase 4: User Story 2 - The curator proposes work, and a human decides (P2)

**Goal**: qualifying issues reach the backlog with a reason, and never the dispatch column.

**Independent test**: one well-scoped issue and one vague one; the first lands in the backlog with a reason, the second is untouched, neither is in the working column.

- [X] T027 [P] [US2] `models.py` for the curator package (`SelectionJudgement`, `CurationOutcome`, `CurationRecord`) plus the guarded schema; test against `contracts/curation-record.schema.json` and test the two do not drift
- [X] T028 [P] [US2] `candidates.py`: open issues minus those already on the board, minus those over `max_per_run`, by rule; tests including the already-on-board skip with a named mutation
- [X] T029 [US2] `judge.py`: one schema-guarded judgement per candidate, criteria supplied from configuration
- [X] T030 [US2] `gate.py`: discard a judgement about an unsent issue; require the reason's quote to appear verbatim in that issue's title or body; tests with one named mutation per rule
- [X] T031 [US2] `act.py` and `personas.py`: add to the backlog column only, apply the label, post the comment saying why; tests that the dispatch column is never a target (with a named mutation), and that the fake received no issue-body edit and no card move, which is the rest of FR-021 and is otherwise carried only by the persona
- [X] T032 [US2] `report.py` and `CuratorWorkflow.run`, plus the curator terminal branch in `handle_status` BEFORE the shared tail; tests in `agent/performer/tests/unit/test_curation_report_path.py` including no push and no pull request
- [X] T033 [US2] Add the curator decision, dispatch, poll and completion alongside the advocate's in `src/coordinare/services/intake_dispatch.py` and `src/coordinare/daemon.py`, reusing the Phase 2 gate helpers
- [X] T034 [US2] Narrow the advocate-label filter in `src/coordinare/graph/nodes/check_board.py` to the escalation label only; test that an issue carrying the handled label is now eligible and one carrying the escalation label is still excluded, with a named mutation
- [X] T035 [US2] End-to-end scenarios in `tests/unit/workflows/curator/test_workflow_end_to_end.py`: qualifies, does not qualify, unquotable reason rejected, already on board, missing `project_id` reported rather than silently skipped, a restart between a run and the next cycle adding no duplicate board item (SC-005), and a run that writes nothing
- [X] T036 [US2] `tests/eval/curator_scenarios/` (four fixtures: qualifies, does not qualify, unquotable reason, already on board) plus `src/coordinare/eval/curator_scenarios.py` registered in `tests/eval/test_scenario_runners.py`

**Checkpoint**: both roles run, and an issue the advocate answered can become work again.

## Phase 5: User Story 3 - The project describes the roles it actually has (P3)

- [X] T037 [P] [US3] Rewrite `DEFAULT_INSTRUCTIONS["advocate"]` and add `DEFAULT_INSTRUCTIONS["curator"]` in `src/coordinare/services/persona_service.py`, add both to `VALID_ROLES`, and add the `curator` field to `PersonasConfig` in `src/coordinare/config.py`; test that neither persona names an outcome the system prevents, in particular that the advocate persona does not mention the project board
- [X] T038 [P] [US3] Correct the advocate row and add the curator row in the `README.md` role table
- [X] T039 [P] [US3] Correct the advocate row and add the curator row in the `docs/quickstart.md` role table
- [X] T040 [US3] In `docs/onboarding/03-performer-lifecycle.md`, drop the advocate from the numbered table, renumber the remaining roles one through eight with no gap, and add the sentence saying both roles run outside the lifecycle on inbound issues; test the numbering is consecutive, which is the part a hand edit gets wrong

## Phase 6: Verification

- [X] T041 [P] `config.example.yaml` blocks for both roles and a "Role workflows: advocate and curator (spec 173)" subsection in `docs/onboarding/04-harnesses-and-shims.md`
- [X] T042 Both suites green in SEPARATE invocations with `PYTHONPATH=src:agent/performer/src`, coordinare with `--cov=coordinare --cov-fail-under=90` measured against the post-deletion tree; `ruff check` clean, run on its own and never piped through `tail`, which masks its exit code
- [X] T043 Apply every named mutation in the real tree, confirm each is killed, restore and verify file hashes; record the table
- [X] T044 Adversarial Workflow review over the full diff, every confirmed finding fixed with a pinning test, every refuted one dispositioned in writing with the execution that refuted it
- [X] T045 Rebuild `coordinare-performer:base` (repo-root context) then `:full` (context `agent/performer`, NOT the repo root), and run live rounds for both roles through the gateway, recording wall time against SC-009 and the zero-call case against SC-010
- [X] T046 Requirement coverage table appended to this file; open the pull request; squash merge; rebuild images from main; write the memory note

## Dependencies

- Phase 2 blocks every story. T008 (schema) and T007 (registries) block the most.
- US1 depends only on Phase 2. It is the MVP: delivered alone, the advocate is better and coordinare is smaller.
- US2 depends on Phase 2 and reuses the dispatch shape US1 establishes in T020 and T021, so it is cheaper after US1 but does not require it to be complete.
- US3 depends on nothing but is written last, since the personas it rewrites are consumed by US1 and US2.
- T023 (the deletion) must land in the same story as the advocate workflow, never later, so the two paths never coexist.

## Parallel opportunities

- Phase 2: T002 through T007, T009 and T010 are all different files.
- US1: T011, T012 and T014 are independent; T013 and T015 through T018 are sequential within the package.
- US2: T027 and T028 are independent.
- US3: T037, T038 and T039 are independent; T040 touches a table the others do not.

## Implementation strategy

US1 alone is a complete, shippable improvement, and it is the one that removes
code. Land it, verify it live, then add US2. US3 is a text-only pass that can
land with either. The riskiest single task is T019 and T032, the terminal
branches: get the shared-tail assertion failing first, then make it pass.

## Requirement coverage

| Requirement | Where it is met | Where it is proven |
| --- | --- | --- |
| FR-001 coordinare starts, never acts | `daemon._maybe_dispatch_intake`; `services/advocate.py` deleted | `test_173_daemon_intake.py`; `test_cardless_runs.py` retirement test |
| FR-002 bounded steps, budget, guarded | `workflows/advocate/__init__.py`, `workflows/curator/__init__.py`, `budget.py` entries | step-duration assertions; `test_173_coordinare_foundations.py` budgets |
| FR-003 startable without a card | `intake_dispatch.build_card_context` (no card, key `id`) | `test_intake_dispatch.py` identifier tests |
| FR-004 one in flight, marker before dispatch | `_maybe_dispatch_intake` ordering | `test_173_daemon_intake.py` rollback tests; mutation `intake.marker_ignored` |
| FR-005 marker not persisted | `EnvCacheState` only, absent from the snapshot | `test_173_coordinare_foundations.py`; `test_cardless_runs.py` restart test |
| FR-006 own terminal outcome | `main.py` intake branch before the shared tail | `test_advocate_report_path.py`; mutation `main.branch_after_the_tail` |
| FR-007 no push, no pull request | the branch returns first; run is read-only | push/PR assertions in `test_advocate_report_path.py`; `write_free_check` |
| FR-008 durable and flushed | `handle_run_result` then `flush_snapshot` | `test_intake_dispatch.py` flush test; mutation `intake.flush_dropped` |
| FR-009 rate limited, escalating | `should_run` backoff, capped exponent | `test_intake_dispatch.py`; mutations `intake.cooldown_ignored`, `no_backoff_on_failure` |
| FR-010 classify each, keyword first | `advocate/intake.py`, `advocate/triage.py` | zero-call keyword test; mutations `triage.keyword_never_matches`, `driver.keyword_after_the_model` |
| FR-011 answer only from documents read | `advocate/gate.py` `answer_is_grounded` | `test_gate_rules.py`; live `answerable` |
| FR-012 withhold and escalate | same gate, `_decide` escalation | live `hallucinated_citation`; mutations `gate.unknown_citations_ignored`, `prose_check_dropped` |
| FR-013 unsent judgement discarded | `gate.accept_classification` | mutation `gate.unsent_accepted` |
| FR-014 preserved outcomes | `_decide` branch table | parametrised branch test; mutation `driver.bug_report_gets_a_comment` |
| FR-015 reason and notification | `_escalate` with the original reason constants | end-to-end escalation tests |
| FR-016 handled read from the issue | `advocate/intake.py` label skip | `test_cardless_runs.py` restart test; mutation `intake.handled_not_skipped` |
| FR-017 one guarded judgement | `curator/__init__.py` single `call_model` | curator end-to-end tests |
| FR-018 backlog only, label, comment | `curator._promote` | `test_the_curator_never_moves_a_card...`; eval `DISPATCH_COLUMNS` check |
| FR-019 unquotable reason rejected | `curator/gate.py` | mutations `curator.quote_never_checked`, `empty_quote_accepted` |
| FR-020 no duplicate board entry | `curator/candidates.py` | mutations `curator.on_board_ignored`, `label_ignored` |
| FR-021 no body edits, no card moves | `_promote` adds only; persona forbids | `board.moved == []` assertion |
| FR-022 only escalated excluded | `check_board.py` narrowed | `test_173_board_filter.py`; updated `test_routing.py` |
| FR-023 persona as instruction | `personas.render_call`, `Toolkit.call_model` | `test_the_persona_never_travels_inside_the_documentation` |
| FR-024 personas match the jobs | `persona_service.DEFAULT_INSTRUCTIONS` | `test_the_advocate_persona_no_longer_names_the_board...` |
| FR-025 documentation corrected | README, quickstart, lifecycle walkthrough | numbering test; advocate-row test |
| FR-026 old path removed | four modules deleted | `test_the_retired_service_is_gone_from_the_tree` |
| FR-027 unchanged when unconfigured | both roles default off | `test_neither_role_runs_when_neither_is_configured` |
| FR-028 rules mutation-tested | 33 mutations | all killed, table below |
| SC-001 no ungrounded answer posted | the gate | live `hallucinated_citation` withheld a real model's answer |
| SC-002 no coordinare classification | the service is gone | retirement test |
| SC-003 no pull requests | terminal branch | push/PR assertions, both roles |
| SC-004 nothing in the dispatch column | `_promote` + config validator | curator tests; `CuratorConfig` validator test |
| SC-005 no duplicates across a restart | labels and the board are the markers | `test_cardless_runs.py` restart tests |
| SC-006 keyword costs no call | pre-model rule | zero-call assertion; live `sensitive_keyword` 0 ms |
| SC-007 previous behaviour preserved | branch table unchanged | full suite, 7942 passing |
| SC-008 unaffected when unconfigured | defaults off | `test_neither_role_runs...` |
| SC-009 under six minutes at p90 | bounded steps, batched calls | live: advocate 24 s worst, curator 21 s worst |
| SC-010 idle repository is free | intake short-circuit | `already_handled` and `already_on_board` at 0 calls |

## Mutation table

33 mutations applied in the real tree, all killed, file hashes verified after restore.

| area | mutations |
| --- | --- |
| advocate gate | 7 (unknown citations ignored, uncited answer allowed, prose check dropped, any backtick counts, unsent accepted, threshold ignored, answerable kind ignored) |
| advocate intake and triage | 3 (handled not skipped, escalated label ignored, keyword never matches) |
| advocate driver and act | 5 (keyword after the model, no-docs still answers, comment without label, unjudged ignored, bug report commented) |
| performer terminal branch | 3 (branch after the tail, shape check dropped, malformed falls through) |
| intake gate and dispatch | 8 (marker ignored, cooldown ignored, no backoff, breaker never trips, marker not cleared, flush dropped, id key is card_id, advocate gets a board) |
| curator | 8 (quote never checked, empty quote accepted, unsent accepted, on-board ignored, label ignored, cap ignored, board failure claimed as added, missing board skipped) |
