# Implementation Plan: Documenter Workflow for a Living Wiki That Humans and Agents Can Read

**Branch**: `171-documenter-workflow` | **Date**: 2026-09-07 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/171-documenter-workflow/spec.md`

## Summary

The documenting stage becomes the seventh consumer of the spec-164 workflow layer. Code decides which pages a run may touch (from the documentation brief and the changed files, or from the repository layout in init mode), gathers each page's evidence through the spec-165 allow-list, makes one schema-guarded write call per page with a no-change option, enforces a page contract that encodes the researched documentation model (llms.txt-shaped index, one Diátaxis kind per page with required headings, ADR-shaped decisions, cited paths that exist, links that resolve, no changelog lines), regenerates the README index by code, refreshes short pointer sections in the agent instruction files, and makes exactly one commit through the existing `commit_files`. main.py maps the record onto `docs_committed`, so coordinare is untouched.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (plan, result and record models), the 164 layer (`Toolkit`, `budget`, `schema_guard`), the 169 reviewer package (`diffparse`, `survey` allow-list runner, `report.write_free_check`), `performer.workspace.commit_files` and `get_head_sha`, structlog. No new external dependencies; Markdown is parsed with a small line-based scanner in the package (no markdown library).
**Storage**: none; `state_store.py` unchanged. Coordinare state and schema untouched.
**Testing**: pytest, both trees in separate invocations; every gate rule mutation-checked in the real tree; six eval fixtures stubbed in CI, `feature` and `init` live through the gateway inside the performer container.
**Target Platform**: the performer container; coordinare unchanged.
**Project Type**: single repository, performer tree only plus config and docs.
**Performance Goals**: update rounds under ten minutes at p90 including container start, init under twenty (SC-004); 8 pages per run; 6 gather commands per page; 6000 completion tokens per write plus one reprompt.
**Constraints**: default off (`workflow: documenter`); byte-for-byte prose path without the flag; one commit, documentation paths only; no model call when the plan is empty; all model traffic through the gateway.
**Scale/Scope**: about 1600 lines of performer code and tests, docs and config example; no coordinare code.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | Every rule (plan selection, citation and link checks, each contract check, README generation, pointer template) is a pure function with its own test; the Markdown scanner is one module; no new dependencies. | PASS |
| II. Testing Discipline | Unit tests per step and per rule with a named mutation; end-to-end scenarios against a real temporary git repository; six deterministic fixtures in CI; live rounds on demand; the prose path pinned byte for byte by a main.py test. | PASS |
| III. UX Consistency | One operator key, `workflow: documenter`, like 164 to 170. Drop reasons name the failed check. `docs_committed` and `files_modified` are the same surface coordinare reads today. | PASS |
| IV. Performance by Design | Budgets stated in the spec table; SC-004 measured over the first ten live rounds. | PASS |
| V. Clarity Before Action | research.md cites the sources the documentation model rests on and the code paths every reuse rests on. | PASS |

Post-design re-check (after data-model and contracts): no gate changed.

## Project Structure

### Documentation (this feature)

```text
specs/171-documenter-workflow/
├── spec.md, plan.md, research.md, data-model.md, quickstart.md, tasks.md
├── contracts/docs-record.schema.json
├── contracts/dispatch-payload-additions.md
└── checklists/requirements.md
```

### Source Code (repository root)

```text
agent/performer/src/performer/workflows/documenter/
├── __init__.py      # DocumenterWorkflow.run: intake, plan, gather, write, gate, pointers, commit, report
├── models.py        # WikiPage, PagePlan, PageEvidence, PageResult, DocsRecord, KINDS, REQUIRED_HEADINGS, DOC_PATHS
├── markdown.py      # line scanner: frontmatter, headings, fenced blocks, links, backticked paths, blockquote
├── inventory.py     # build_inventory(workspace): pages, kinds, citations, links; repository layout for init
├── plan.py          # build_plan(mode, brief, changed_files, inventory, layout, cap)
├── gather.py        # per-page evidence through the reviewer allow-list runner
├── personas.py      # WRITE persona per kind (templates and writing rules), placeholders
├── write.py         # run_write_step: one schema-guarded call per page
├── gate.py          # citations_exist, links_resolve, contract checks, is_changelog_line, verdict per page
├── index.py         # generate_readme(project, summary, pages), llms.txt shape
├── pointers.py      # render_pointer_section, replace_between_markers
├── commit.py        # apply writes and retirements, revert stray paths, commit_files once
└── report.py        # DocsRecord assembly with the executed write-free check before the commit
agent/performer/src/performer/workflows/__init__.py     # SUPPORTED_WORKFLOWS += documenter
agent/performer/src/performer/workflows/adapter.py      # documenter toolkit: command runner, no screenshot/dom
agent/performer/src/performer/workflows/budget.py       # _STEP_BUDGETS["doc_write"] = 6000
agent/performer/src/performer/main.py                   # documenting branch: report["docs"] -> docs_committed / env_blocked, prose path untouched
src/coordinare/config.py                                 # KNOWN_WORKFLOWS += documenter
src/coordinare/eval/documenter_scenarios.py              # runner (stub + --live)
tests/unit/workflows/documenter/                        # markdown, inventory, plan, gate (mutations), index, pointers, commit, e2e
tests/eval/documenter_scenarios/                        # six fixtures on generated repositories
agent/performer/tests/unit/test_documenter_report_path.py
config.example.yaml, docs/onboarding/04-harnesses-and-shims.md
```

**Structure Decision**: a sibling package under `workflows/` as 165 to 170, importing the reviewer's parser and allow-list runner rather than subclassing.

## Complexity Tracking

| Item | Why it is needed | Simpler alternative rejected because |
| --- | --- | --- |
| A line-based Markdown scanner | the contract checks need headings, fenced blocks, links, backticked paths and the README blockquote | a Markdown library adds a dependency for six token kinds |
| The README generated by code, never written by the model | the index is the agent's entrypoint and must link every page exactly once in a fixed shape | gating a model-written README would drop it on most runs anyway |
| Gather through the reviewer's allow-list runner rather than free reads | every evidence read is recorded and refusable, as for the architect and reviewer | direct file reads would bypass the record the spec requires |

## Phases

### Phase 0 Research (this plan)

research.md: the documentation model and its sources (R-a to R-f), the reuse seams (R-g to R-j).

### Phase 1 Design

data-model.md (models, kinds, required headings, rule predicates with mutations, the canonical flow), contracts, quickstart.

### Phase 2 Implementation

Foundations (models, markdown scanner, inventory, plan, index, pointers, budgets, registry, toolkit) then the core by hand (gather, write, gate, commit, run, main.py branch), eval fixtures on generated repositories, docs.

### Phase 3 Verification

Both trees green; mutation table; adversarial review with refute by execution; image rebuild; live rounds `feature` and `init`.

### Phase 4 Rollout

Merge (squash), rebuild images from main, enable on one symphony.
