# Implementation Plan: Security Workflow with the Scan in the Performer and Code-Verified Findings

**Branch**: `170-security-workflow` | **Date**: 2026-09-07 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/170-security-workflow/spec.md`

## Summary

The security stage becomes the sixth consumer of the spec-164 workflow layer, built as a sibling of the spec-169 reviewer that imports the reviewer's diff parser, survey, schema builder, anchor rules, poster and write-free check. The new part is a scan step that runs semgrep and bandit inside the performer, fails closed, and feeds tool findings into a gate that assigns severity and routing from a fixed category table. main.py maps the record onto the statuses coordinare already routes; coordinare stops running its own dispatch-time scan for a role that runs the workflow and lifts the blocking implementer-routed findings into the spec-169 `review_findings` carrier so the spec-167 repair lane fixes them.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (SecurityFinding, SecurityRecord, ScanResult), the 164 layer (`Toolkit`, `budget`, `schema_guard`), the 169 reviewer package (`diffparse`, `survey`, `models.model_findings_schema`, `gate` anchor rules, `post`, `report.write_free_check`), semgrep and bandit already in `Dockerfile.full`, structlog. No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py`, unchanged (schema stays v19; the security stage writes the existing `review_findings` field).
**Testing**: pytest, both trees in separate invocations (`tests/` with the 90 percent coverage gate; `agent/performer/tests`). Every gate rule mutation-checked in the real tree. Six eval fixtures stubbed in CI; live through the LiteLLM gateway inside the performer container.
**Target Platform**: the performer container (Debian, `coordinare-performer:full` or `:extra`); coordinare on macOS or Linux.
**Project Type**: single repository, two source trees (`src/coordinare`, `agent/performer/src/performer`).
**Performance Goals**: a security round under ten minutes at p90 including container start (SC-004); scan at most 120 seconds per tool; one findings call at 8000 completion tokens; one re-anchor call; 12 survey commands with 4000 characters kept each and one coverage pass.
**Constraints**: default off (`workflow: security`); byte-for-byte prose path without the flag, including coordinare's 083 floor; no writes to the repository; exactly one GitHub review; fail closed on any scanner problem; all model traffic through the gateway.
**Scale/Scope**: about 1500 lines of performer code and tests, about 200 lines of coordinare changes, six eval fixtures.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | Every gate rule (FR-008 to FR-012) is a pure function with its own test; the category and routing tables are module constants; the scanner normaliser is one module with a parity test against coordinare's copy over the shared fixtures. No new dependencies. | PASS |
| II. Testing Discipline | Unit tests per step (intake, scan, survey, findings, gate, post, report), per rule with a named mutation, the main.py mapping incl. the prose path unchanged, coordinare gating of the 083 floor both ways, the lift into `review_findings`, the payload contract. Six deterministic fixtures in CI; live rounds on demand. | PASS |
| III. UX Consistency | One operator key, `workflow: security`, like 164 to 169. Hold reasons name the tool or the unread files. The review shape matches the reviewer's. | PASS |
| IV. Performance by Design | Budgets stated in the spec table; SC-004 measured over the first ten live rounds. | PASS |
| V. Clarity Before Action | Every decision in research.md cites the code path it rests on; the four design choices were taken with the operator before the spec. | PASS |

Post-design re-check (after data-model and contracts): no gate changed.

## Project Structure

### Documentation (this feature)

```text
specs/170-security-workflow/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── security-record.schema.json
│   └── dispatch-payload-additions.md
├── checklists/requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
agent/performer/src/performer/workflows/security/
├── __init__.py          # SecurityWorkflow.run: intake, scan, survey, findings, gate, post, report
├── models.py            # SecurityFinding, ScanResult, SecurityRecord, CATEGORY_TABLE, ROUTING, SECURITY_CATEGORIES
├── scanner.py           # run_scan(files, repo_root, runner, budgets): semgrep + bandit, normalisers (parity with coordinare), ScannerUnavailable
├── budgets.py           # SecurityBudgets.from_env: scan timeout, semgrep config, survey and finding caps
├── personas.py          # FINDINGS (taint analysis over the fixed category set), REANCHOR
├── intake.py            # build_intake: reviewer diffparse + brief; no prior comments
├── findings.py          # run_findings_step / run_reanchor_step with the security schema (introduced_by, downgrade_reason)
├── gate.py              # anchor_ok_security, severity_for, routing_for, apply_downgrade, merge_scanner_findings, split_blocking, verdict, run_gate
├── post.py              # build_security_review (blocking inline, advisories in the body), post via reviewer.post.post_review machinery
└── report.py            # build_report with the executed write-free check
agent/performer/src/performer/workflows/__init__.py     # SUPPORTED_WORKFLOWS += security
agent/performer/src/performer/workflows/adapter.py      # security toolkit: command runner, no screenshot/dom
agent/performer/src/performer/workflows/budget.py       # _STEP_BUDGETS["security_findings"] = 8000
agent/performer/src/performer/main.py                   # security branch: report["security"] -> security_passed / security_failed / env_blocked
src/coordinare/config.py                                 # KNOWN_WORKFLOWS += security
src/coordinare/graph/nodes/dispatch_performer.py         # _role_runs_workflow(); skip the 083 floor and inject pr_diff for security under the workflow; reset review_findings on security dispatch
src/coordinare/graph/nodes/monitor_performer.py          # skip the 083 floor merge when the report carries "security"; lift blocking implementer-routed findings into review_findings
specs/contracts/dispatch-payload.md                     # pr_diff row: security under the workflow; review_findings producer row: security stage
src/coordinare/eval/security_scenarios.py                # runner (stub + --live), registered in tests/eval/test_scenario_runners.py
tests/unit/workflows/security/                          # models, scanner, intake, gate rules (mutation), post, e2e scenarios
tests/unit/services/test_security_scanner_parity.py     # both normalisers over tests/unit/services/fixtures/security_scanner
tests/unit/graph/nodes/test_security_workflow_gating.py # floor skipped under the workflow, kept without it; lift into review_findings
tests/eval/security_scenarios/                          # fixtures, fake scanner runner, stub model, recording poster, scoring, README
agent/performer/tests/unit/test_security_report_path.py # main.py mapping and the prose path unchanged
config.example.yaml, docs/onboarding/04-harnesses-and-shims.md
```

**Structure Decision**: a sibling package under `workflows/`, exactly as 165 to 169, importing from `workflows/reviewer/` rather than subclassing it (approach 1 of the brainstorm).

## Complexity Tracking

| Item | Why it is needed | Simpler alternative rejected because |
| --- | --- | --- |
| The scan runs through its own async subprocess runner, not `Toolkit.run_command` | `Toolkit.run_command` merges stderr into stdout and `performer.workspace.run_command` truncates output to 2000 characters, either of which corrupts the tools' JSON | routing the tools through the toolkit would need a second command path with different truncation and capture rules; the scanner runner is injectable for tests and records commands on the record and `metrics.commands_run` itself |
| A duplicated normaliser with a parity test (FR-020) | the coordinare daemon image ships `src/` and `packages/` only, so coordinare cannot import the performer package in production | a third shared package would touch both images' Dockerfiles for six functions |
| Coordinare gating on the role's configured workflow at dispatch and on the report shape at monitor | dispatch must decide before the performer runs; monitor can see whether the workflow ran from the report, which needs no config lookup and cannot disagree with what actually happened | gating both on config would miss a performer that fell back to prose |

## Phases

### Phase 0 Research (this plan)

research.md: R-a scan placement and fail-closed shape, R-b diff injection for security under the workflow, R-c anchor widening for surveyed unchanged files, R-d severity and routing tables, R-e scanner findings as rule findings and dedup, R-f coordinare gating both ways, R-g the lift into `review_findings` and the 022 findings mapping, R-h semgrep configuration and network.

### Phase 1 Design (after plan approval)

data-model.md (models, tables, rule predicates, canonical flow, persona placeholders), contracts/security-record.schema.json, contracts/dispatch-payload-additions.md, quickstart.md.

### Phase 2 Implementation

Foundations (models, scanner with parity test, budgets, registry, toolkit, coordinare gating and lift, contract) then the workflow core by hand (intake, scan, survey reuse, findings, gate with mutations, post, report, run, main.py branch), the eval fixtures and runner, docs.

### Phase 3 Verification (before merge)

Both trees green in separate invocations; mutation table recorded; adversarial Workflow review over the full diff with refute-by-execution; image rebuild; live rounds `clean` and `injection` in the performer container.

### Phase 4 Rollout

Merge (squash), rebuild images from main, enable `workflow: security` on one low-traffic symphony first.
