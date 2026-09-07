# Tasks: Security Workflow with the Scan in the Performer and Code-Verified Findings

**Input**: spec.md, plan.md, research.md, data-model.md, contracts/, quickstart.md in `specs/170-security-workflow/`
**Tests**: required (Constitution II); every gate rule test carries one named mutation applied in the real tree; both trees run in separate pytest invocations.

## Phase 1: Setup

- [x] T001 Create `agent/performer/src/performer/workflows/security/` with `__init__.py` (SecurityWorkflow placeholder, `STATES`), `models.py`, `scanner.py`, `budgets.py`, `personas.py`, `intake.py`, `findings.py`, `gate.py`, `post.py`, `report.py`
- [x] T002 [P] Register `"security"` in `SUPPORTED_WORKFLOWS` (`agent/performer/src/performer/workflows/__init__.py`) and `KNOWN_WORKFLOWS` (`src/coordinare/config.py`); test in `tests/unit/workflows/test_registry.py` and the config test
- [x] T003 [P] Add `_STEP_BUDGETS["security_findings"] = 8000` in `agent/performer/src/performer/workflows/budget.py`
- [x] T004 [P] Security toolkit branch in `agent/performer/src/performer/workflows/adapter.py` (command runner, no screenshot or dom); test `tests/unit/workflows/test_adapter_security_toolkit.py`

## Phase 2: Foundational

- [x] T005 [P] `models.py`: `SECURITY_CATEGORIES`, `CATEGORY_TABLE`, `ROUTING`, `BLOCKING`, `CWE_TO_CATEGORY`, `SecurityFinding`, `ScanResult`, `SecurityRecord`, `model_security_findings_schema(categories, max_findings)`; tests `tests/unit/workflows/security/test_models.py` (bounds, evidence rule, downgraded only for model, schema forbids severity/routing/verdict)
- [x] T006 [P] `scanner.py`: normalisers copied from `src/coordinare/services/security_scanner.py`, `ScannerUnavailable`, `build_semgrep_command`, `build_bandit_command`, async `run_scan(files, repo_root, runner, budgets) -> (list[dict], list[ScanResult])` with an injectable runner; default runner `asyncio.create_subprocess_exec` capturing stdout and stderr separately with a timeout; tests `tests/unit/workflows/security/test_scanner.py` (missing binary, timeout, empty stdout, unparseable JSON, non-zero exit with JSON is findings, ScanResult recorded)
- [x] T007 [P] Parity test `tests/unit/services/test_security_scanner_parity.py`: both normalisers over `tests/unit/services/fixtures/security_scanner/*` produce identical finding lists
- [x] T008 [P] `budgets.py`: `SecurityBudgets.from_env` (SECURITY_SCAN_TIMEOUT_S, SECURITY_SEMGREP_CONFIG, SECURITY_SURVEY_MAX_COMMANDS, SECURITY_SURVEY_MAX_OUTPUT_CHARS, SECURITY_MAX_FINDINGS capped at 30); tests
- [x] T009 [P] Coordinare dispatch gating in `src/coordinare/graph/nodes/dispatch_performer.py`: `_role_runs_workflow(state, role, name)`; skip `_run_security_floor` and set `state["scanner_findings"] = []` when the security role runs the workflow; inject `pr_diff` for security under the workflow; `reset_review_findings_for_reviewer` also clears on `security`; tests `tests/unit/graph/nodes/test_security_workflow_gating.py` (floor runs without the flag, skipped with it; pr_diff injected only with it; reset on security dispatch)
- [x] T010 [P] Coordinare monitor gating in `src/coordinare/graph/nodes/monitor_performer.py`: skip the 083 floor merge when `status["report"]["security"]` is a dict; in the `security_failed` block lift blocking implementer-routed findings into `state["review_findings"]` in the reviewer record shape when the report carries a security record and the target is implementing; tests in the same file (merge applies to a prose response, skipped for a workflow report; lift shape passes `_lift_review_findings` validation; nothing lifted when routed to architect)
- [x] T011 [P] `specs/contracts/dispatch-payload.md`: apply `contracts/dispatch-payload-additions.md` (pr_diff, scanner_findings, review_findings producer notes); contract test `tests/contract/test_security_payload.py` (pr_diff present for security only under the workflow)

## Phase 3: User Story 1 (vulnerable change caught) and User Story 2 (scanner findings are the floor)

- [x] T012 [US1] `personas.py`: FINDINGS (taint analysis over the fixed set, verbatim evidence, `introduced_by`, optional `downgrade_reason`, no severity or verdict), REANCHOR; `render_scan_findings`
- [x] T013 [US1] `intake.py`: `build_intake(score)` via the reviewer parser; brief; no prior comments; `as_text()`
- [x] T014 [US1] `findings.py`: `run_findings_step` and `run_reanchor_step` with the security schema under `Budget.for_step("security_findings")`
- [x] T015 [US1] `gate.py`: `anchor_ok_security`, `severity_for`, `routing_for`, `apply_downgrade`, `map_scanner_category`, `scanner_to_findings`, `merge_scanner_findings`, `split_blocking`, `verdict`, `run_gate`; tests `tests/unit/workflows/security/test_gate_rules.py` with one named mutation per rule (data-model "Rule predicates")
- [x] T016 [US1] `post.py`: `build_security_review` (blocking inline inside hunks, the rest and advisories in the body, downgrades named) and `post_security_review` reusing the reviewer poster machinery; tests `tests/unit/workflows/security/test_post.py`
- [x] T017 [US1] `report.py`: `build_report` with the executed write-free check
- [x] T018 [US1] `__init__.py`: `SecurityWorkflow.run` (intake, scan, survey, findings, gate, post, report), scanner hold before any model call, env_blocked on unread files or post failure, step timing and events
- [x] T019 [US1] `agent/performer/src/performer/main.py` security branch: on `report["security"]` map `security_failed` (findings in the 022 shape, cycle limit), `security_passed`, `env_blocked`; skip the committed report and advisory comments; prose path untouched; tests `agent/performer/tests/unit/test_security_report_path.py`
- [x] T020 [US1][US2] End-to-end scenarios `tests/unit/workflows/security/test_workflow_end_to_end.py`: clean, injection, secret (scanner only, model silent), dedup of a scanner and a model finding, hallucinated anchor re-anchored, surveyed unchanged sink in the body, introduced_by not changed dropped, downgrade recorded, scanner unavailable holds before any model call, post failure holds, dirty tree fails, step events and durations
- [x] T021 [US1] Adapter seam test in `tests/unit/workflows/test_dispatch_branch.py` for the security workflow

## Phase 4: User Story 3 (no scanner, no pass) and User Story 4 (advisories in one comment)

- [x] T022 [US3] Scanner hold scenarios in the e2e file (missing binary, timeout, unparseable), each with zero model calls and no review
- [x] T023 [US4] Advisory-only and coverage-hold scenarios in the e2e file

## Phase 5: Eval

- [x] T024 [P] `tests/eval/security_scenarios/{__init__,fixtures,stub_model,scoring,test_eval}.py` and `README.md`: six fixtures (clean, injection, secret, scanner_unavailable, advisory_only, downgrade), fake scanner runner, stub model in call order, recording poster, scoring (verdict, anchors, blocking from tools never dropped, one review and its event, coverage, write-free)
- [x] T025 `src/coordinare/eval/security_scenarios.py` (stub and `--live` with the real tools over a temporary repo) registered in `tests/eval/test_scenario_runners.py`

## Phase 6: Polish and verification

- [x] T026 [P] `config.example.yaml` `# workflow: security` block with the env keys; `docs/onboarding/04-harnesses-and-shims.md` subsection "Role workflows: security (spec 170)"
- [x] T027 Both trees green in separate invocations with the coverage gate; ruff clean; mutation table recorded
- [x] T028 Adversarial Workflow review over the full diff (diverse lenses, refute by execution); every confirmed finding fixed with a pinning test
- [x] T029 Rebuild `coordinare-performer:{base,full,extra}` from the branch; live rounds `clean` and `injection` in the container with the real semgrep and bandit and the gateway model, recorded against SC-001 and SC-002
- [ ] T030 Requirement coverage table FR-001 to FR-020 in this file; PR; squash merge; images rebuilt from main

## Implementation notes (what landed where)

- Foundations: `tests/unit/workflows/security/{test_models,test_scanner,test_budgets}.py`, `tests/unit/services/test_security_scanner_parity.py` (synthetic semgrep and bandit severity matrices; the coordinare fixtures are source files for real tool runs, not tool JSON), `tests/unit/workflows/test_adapter_security_toolkit.py`, `tests/unit/graph/nodes/test_security_workflow_gating.py`, `tests/contract/test_security_payload.py`.
- Core: `tests/unit/workflows/security/test_gate_rules.py` (15 mutations killed in the real tree, hash-checked restore), `test_workflow_end_to_end.py` (T020, T022, T023 scenarios), `agent/performer/tests/unit/test_security_report_path.py` (T019), the adapter seam test in `tests/unit/workflows/test_dispatch_branch.py` (T021).
- Eval: `tests/eval/security_scenarios/` (six fixtures, fake scanner runner, recording poster) and `src/coordinare/eval/security_scenarios.py` registered in `tests/eval/test_scenario_runners.py` (T024, T025).
- T029: images rebuilt from the branch; live rounds `clean` (150s, passed) and `injection` (262s, failed with the SQL injection anchored) recorded in PR #271, plus a third `injection` round after the dedup fixes.

## Requirement coverage

| FR | where it is proven |
| --- | --- |
| FR-001 order by code | e2e `clean_change_passes_with_one_comment_review` (step events) |
| FR-002 intake | `intake.py` via the reviewer parser; e2e truncated scenario |
| FR-003 scan and per-tool results | `test_scanner.py`, parity test, e2e `clean` (two ScanResults) |
| FR-004 fail closed before any model call | e2e `a_broken_scanner_holds_before_any_model_call` (four kinds), `test_scanner.py` exit codes and partial results |
| FR-005 survey allow-list and coverage | reviewer survey reused; e2e surveyed sink and truncated scenarios |
| FR-006 one coverage pass | e2e `a_truncated_diff_runs_the_coverage_pass_and_unread_files_hold` |
| FR-007 one schema-guarded call, forbidden keys | `test_models.py` schema tests; e2e hallucinated anchor (3 calls) |
| FR-008 anchor rule and re-anchor | `test_gate_rules.py::test_anchor_widens...`, e2e hallucinated anchor, introduced_by, surveyed sink |
| FR-009 severity and routing tables | `test_severity_comes_from_the_category_table`, `test_routing_comes_from_the_category` |
| FR-010 downgrade | `test_downgrade_needs_a_model_finding...`, e2e downgrade scenarios |
| FR-011 scanner findings never dropped, dedup | `test_scanner_findings_are_rule_findings...`, `test_a_model_finding_at_a_scanner_anchor...`, `test_tool_findings_are_never_sliced_out...`, e2e secret and dedup |
| FR-012 verdict and coverage | `test_verdict_is_derived_by_code`, e2e coverage hold |
| FR-013 one review | `test_post.py` (via e2e recorded reviews), e2e advisory and blocking scenarios |
| FR-014 statuses and 022 shape | `test_security_report_path.py` |
| FR-015 lift into review_findings | `test_security_workflow_gating.py` lift tests, contract test |
| FR-016 floor gated both ways | `test_security_workflow_gating.py` dispatch and monitor tests |
| FR-017 write-free | e2e `a_dirty_tree_fails_the_round` |
| FR-018 timing and events | e2e `every_step_is_timed_and_logged` |
| FR-019 pure rules, mutation | 15 mutations killed (PR #271 table) |
| FR-020 normaliser parity | `test_security_scanner_parity.py` |
