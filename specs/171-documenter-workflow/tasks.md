# Tasks: Documenter Workflow for a Living Wiki That Humans and Agents Can Read

**Input**: spec.md, plan.md, research.md, data-model.md, contracts/, quickstart.md in `specs/171-documenter-workflow/`
**Tests**: required (Constitution II); every gate rule test carries one named mutation applied in the real tree; both trees run in separate pytest invocations.

## Phase 1: Setup and foundations

- [x] T001 Create `agent/performer/src/performer/workflows/documenter/` with placeholder modules and `DocumenterWorkflow` (`STATES`)
- [x] T002 [P] Register `"documenter"` in `SUPPORTED_WORKFLOWS` and `KNOWN_WORKFLOWS`; `_STEP_BUDGETS["doc_write"] = 6000`; adapter toolkit branch (command runner, no screenshot or dom); tests
- [x] T003 [P] `models.py` per data-model.md with constants; tests `tests/unit/workflows/documenter/test_models.py` incl. schema compliance against `contracts/docs-record.schema.json`
- [x] T004 [P] `markdown.py`: frontmatter, headings (level, text, has link, has code), fenced blocks with language, links (text, target), backticked path tokens, blockquote under H1; tests
- [x] T005 [P] `inventory.py`: `build_inventory(workspace, tree)`, `extract_citations`, `repository_layout(workspace)`; tests on a temporary repo
- [x] T006 [P] `plan.py`: `is_doc_path`, `select_pages`, `init_skeleton`, `build_plan`; tests with mutations
- [x] T007 [P] `index.py`: `generate_readme`, `readme_shape_ok`; tests with mutations
- [x] T008 [P] `pointers.py`: `render_pointer_section`, `replace_between_markers`; tests with mutations
- [x] T009 [P] `budgets.py`: `DocumenterBudgets.from_env`; tests

## Phase 2: Core (US1, US2, US3)

- [x] T010 [US1] `personas.py`: WRITE persona per kind with the writing rules and placeholders; `render_write_persona`
- [x] T011 [US1] `gather.py`: code-built commands through the allow-list, recorded; tests
- [x] T012 [US1] `write.py`: one schema-guarded call per page; tests
- [x] T013 [US1][US3] `gate.py`: `citations_exist`, `links_resolve`, `contract_failures` (each check), `is_changelog_heading`, `accept_retire`, `gate_page`; tests with one named mutation per rule
- [x] T014 [US1] `commit.py`: file list from survivors plus README and pointers, deletions from retirements, `commit_files` once; stray path revert; tests on a temporary repo
- [x] T015 [US1][US2] `__init__.py`: `DocumenterWorkflow.run` with the empty-plan early exit, dirty-tree hold, step timing and events
- [x] T016 [US1] `agent/performer/src/performer/main.py` documenting branch on `report["docs"]` with a known verdict; prose path untouched; tests `agent/performer/tests/unit/test_documenter_report_path.py`
- [x] T017 [US1][US2][US3] End-to-end scenarios `tests/unit/workflows/documenter/test_workflow_end_to_end.py` on a real temporary git repository: feature, trivial, shape, hallucinated citation, unchanged, retire rules, dirty tree hold, commit failure hold, events
- [x] T018 Adapter seam test in `tests/unit/workflows/test_dispatch_branch.py`

## Phase 3: Init and pointers (US4, US5)

- [x] T019 [US4] init skeleton end-to-end on a generated repository with five packages; cap and deferred
- [x] T020 [US5] pointer replacement end-to-end with a stale section; byte-for-byte rest

## Phase 4: Eval and docs

- [x] T021 [P] `tests/eval/documenter_scenarios/` (fixtures generating repositories, stub model, scoring, README) and `src/coordinare/eval/documenter_scenarios.py` registered in `tests/eval/test_scenario_runners.py`
- [x] T022 [P] `config.example.yaml` block and `docs/onboarding/04-harnesses-and-shims.md` subsection

## Phase 5: Verification

- [x] T023 Both trees green in separate invocations with the coverage gate; ruff clean; mutation table recorded
- [x] T024 Adversarial Workflow review over the full diff; every confirmed finding fixed with a pinning test
- [x] T025 Rebuild images from the branch; live rounds `feature` and `init` in the container with the gateway model
- [ ] T026 Requirement coverage table; PR; squash merge; images rebuilt from main

## Implementation notes (what landed where)

- Foundations (agent): `models.py`, `markdown.py`, `inventory.py`, `plan.py`, `index.py`, `pointers.py`, `budgets.py` with `tests/unit/workflows/documenter/test_{models,markdown,inventory,plan,index,pointers,budgets}.py`; registry, budget and toolkit entries with `test_adapter_documenter_toolkit.py`.
- Core (by hand): `personas.py`, `gather.py`, `write.py`, `gate.py`, `commit.py`, `report.py`, `__init__.py`; `test_gate_rules.py` (19 mutations killed in the real tree, hashes checked), `test_workflow_end_to_end.py` on a real temporary git repository with a local committer, `test_review_fixes.py` (one pinning test per confirmed review finding), the adapter seam test in `tests/unit/workflows/test_dispatch_branch.py`, `agent/performer/tests/unit/test_documenter_report_path.py`.
- Eval: `tests/eval/documenter_scenarios/` (six fixtures on generated repositories) and `src/coordinare/eval/documenter_scenarios.py` registered in `tests/eval/test_scenario_runners.py`.
- Live (T025): three rounds of `feature` and `init` in `coordinare-performer:full` rebuilt from the branch with glm-5.3-flash through the gateway; the first exposed the retire-for-missing-page and route-as-citation habits, fixed before the review; results in PR #272.

## Requirement coverage

| FR | where it is proven |
| --- | --- |
| FR-001 order by code | e2e `feature` (step events) |
| FR-002 intake | `test_inventory.py`, `test_markdown.py`, e2e |
| FR-003 plan by code, cap | `test_plan.py`, `test_review_fixes.py` (README slot, kind default, modules merged, init order) |
| FR-004 doc paths only | `test_plan.py`, `test_review_fixes.py` (traversal refused) |
| FR-005 gather through the allow-list | `test_review_fixes.py::test_gather_quotes_paths_with_spaces`, e2e |
| FR-006 one write per page, retire rule | e2e retire scenario, `test_gate_rules.py::test_retire_only_for...`, persona test |
| FR-007 citations and links | `test_gate_rules.py`, `test_review_fixes.py` (fences, doubled links, boundaries), e2e hallucinated citation |
| FR-008 page contract | `test_gate_rules.py::test_the_contract_names_every_failed_check` (11 checks, each mutation-killed), e2e shape |
| FR-009 README shape by code | `test_index.py`, `test_review_fixes.py`, e2e `feature` |
| FR-010 decisions under decisions/ | `test_review_fixes.py::test_a_decision_page_outside...` |
| FR-011 pointers | `test_pointers.py`, `test_review_fixes.py`, e2e pointers |
| FR-012 one commit, doc paths, holds | e2e feature, dirty tree, commit failure |
| FR-013 statuses, init PR | `test_documenter_report_path.py` |
| FR-014 prose path unchanged | `test_documenter_report_path.py` (no report key; docs dict without verdict) |
| FR-015 timing and events | e2e `feature` |
| FR-016 pure rules, mutation | 19 mutations killed (PR #272 table) |
| FR-017 writing rules in the persona | `personas.py` WRITING_RULES; persona test |
