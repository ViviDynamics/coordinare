# Tasks: Closer Workflow

**Tests**: required; one named mutation per rule test; both trees run separately.

## Phase 1: Foundations

- [X] T001 Extract `fetch_review_threads(owner, repo, pr, token, max_pages)` and `resolve_review_threads(owner, repo, thread_ids, token) -> (resolved_ids, failures)` in `agent/performer/src/performer/github.py`; `resolve_pr_review_threads` keeps its signature and return and calls them; tests including the unchanged reviewer behaviour
- [X] T002 [P] Register `"closer"` in `SUPPORTED_WORKFLOWS` and `KNOWN_WORKFLOWS`; `_STEP_BUDGETS["closing_judge"] = 4000`; adapter toolkit branch (no command runner needed, model call only); tests
- [X] T003 [P] `models.py` per data-model.md with `model_judgements_schema`; tests including the JSON schema
- [X] T004 [P] `classify.py`: `is_resolved`, `is_stale`, `is_answered`, `classify_thread`; tests with one named mutation each
- [X] T005 [P] `budgets.py`: `CloserBudgets.from_env`; tests

## Phase 2: Core

- [X] T006 `personas.py`: JUDGE persona and `render_threads`
- [X] T007 `judge.py`: one schema-guarded call over the answered threads
- [X] T008 `gate.py`: `quote_found`, `accept_judgement`, `verdict`, `to_resolve`, `run_gate`; tests with mutations
- [X] T009 `post.py`: `build_closing_review` and the poster reusing the reviewer machinery; tests
- [X] T010 `report.py` and `__init__.py`: the run sequence with the no-model path, holds on fetch, post and resolve failures, step timing and events
- [X] T011 `agent/performer/src/performer/main.py` closing_review branch on `report["closing"]`; prose path untouched; tests `agent/performer/tests/unit/test_closer_report_path.py`
- [X] T012 End-to-end scenarios `tests/unit/workflows/closer/test_workflow_end_to_end.py` against a fake GitHub: clean, answered, open, outdated, hallucinated quote, unsent judgement, missing judgement, fetch failure, post failure, resolve failure, zero threads
- [X] T013 Adapter seam test in `tests/unit/workflows/test_dispatch_branch.py`

## Phase 3: Eval and docs

- [X] T014 [P] `tests/eval/closer_scenarios/` (five fixtures, fake GitHub, stub model, scoring) and `src/coordinare/eval/closer_scenarios.py` registered in `tests/eval/test_scenario_runners.py`
- [X] T015 [P] `config.example.yaml` block and the onboarding subsection

## Phase 4: Verification

- [X] T016 Both trees green; ruff clean; mutation table recorded
- [X] T017 Adversarial Workflow review over the full diff; every confirmed finding fixed with a pinning test
- [X] T018 Rebuild images; live rounds `clean` and `answered`
- [X] T019 Requirement coverage table; PR; squash merge; images rebuilt from main

## Requirement coverage

| Requirement | Where it is met | Where it is proven |
| --- | --- | --- |
| FR-001 step order | `workflows/closer/__init__.py` `CloserWorkflow.run` | `test_workflow_end_to_end.py` step-order and metrics assertions |
| FR-002 full thread intake | `github.fetch_review_threads` (paged, full comments) | `agent/performer/tests/unit/test_github_threads.py` paging and comment-pagination tests |
| FR-003 pure classification | `workflows/closer/classify.py` | `test_classify.py`, mutations `classify.answered_before_stale`, `classify.string_timestamps` |
| FR-004 at most one call, answered only | `workflows/closer/judge.py`, `run` judge step | `test_workflow_end_to_end.py` only-ambiguous and judge-skipped tests |
| FR-005 gate discards unsent and unquoted | `workflows/closer/gate.py` `accept_judgement`, `quote_found` | `test_gate_rules.py`, mutations `accept.unsent_ok`, `accept.skip_quote`, `quote_found.*` |
| FR-006 verdict by code, no CI | `workflows/closer/gate.py` `verdict` | `test_gate_rules.py`, mutation `verdict.approve_with_open` |
| FR-007 exactly one review, posted first | `workflows/closer/post.py`, `run` post step | `test_workflow_end_to_end.py` post-before-resolve and post-failure tests |
| FR-008 resolve only stale and addressed | `workflows/closer/gate.py` `to_resolve` | `test_gate_rules.py`, mutations `to_resolve.include_open`, `to_resolve.drop_stale`, `to_resolve.no_dedup` |
| FR-009 fetch, post, resolve failures hold | `run` error paths | `test_workflow_end_to_end.py` three failure tests |
| FR-010 statuses coordinare handles | `agent/performer/src/performer/main.py` closing_review branch | `agent/performer/tests/unit/test_closer_report_path.py` |
| FR-011 unchanged without the workflow | the branch keys on `report["closing"]` only | `test_closer_report_path.py` prose-path test, `test_review_fixes.py` guard test |
| FR-012 per-step and per-call events | step timing in `run`; `workflows/adapter.py` `workflow.model_call` | step-duration assertions; the adapter event verified by driving `_model_caller` with a stubbed client |
| FR-013 every rule mutation-tested | `classify.py`, `gate.py` | 12 mutations, all killed (table in the pull request) |
| FR-014 zero calls when nothing is ambiguous | judge step skip | `test_workflow_end_to_end.py` clean test; live `clean`, `open`, `outdated` |
| SC-001 no approval with an open thread | `verdict` | all five fixtures, stubbed and live |
| SC-002 zero calls on resolved or stale | judge step skip | live `clean` 0 calls, `outdated` 0 calls |
| SC-003 every resolution justified | `to_resolve` reasons, `quote_found` | eval scoring `justification` check; live `answered` |
| SC-004 latency | no model call on the common path | live: 0 to 1 ms without a call, 5.7 s and 8.4 s with one |
| SC-005 five fixtures deterministic and live | `tests/eval/closer_scenarios` | 5/5 stubbed in CI, 5/5 live on the final image |
