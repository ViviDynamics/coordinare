# Implementation Plan: Reviewer Workflow with Verified Findings and a Structured Hand-off

**Branch**: `169-reviewer-workflow` | **Date**: 2026-09-07 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/169-reviewer-workflow/spec.md`

## Summary

Extend the spec-164 role-workflow layer to the reviewer performer: a bounded review over the PR diff plus a read-only survey of the surrounding code, schema-validated findings whose anchors are verified against the diff, prior feedback dispositioned by rule, coverage enforcement before approval, and a verdict derived by code. One GitHub review is posted with inline comments per finding. The findings are lifted into coordinare state and injected into the implementer dispatch as a repair brief. Default-off via `workflow: reviewer`.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only. pydantic 2.x (finding, disposition, review record models), the 164 layer (`workflows/`, `Toolkit` with command_runner, `budget`, `schema_guard`), spec-165 brief parsing, spec-167 implementer lane plumbing, structlog (observability). No new external dependencies.
**Storage**: single-host JSON snapshot via `state_store.py`; schema version 18 to 19 adds `PersistedSession.review_findings`, cleared on reviewer re-dispatch. Older snapshots load with `None` default.
**Testing**: pytest (`tests/unit`, `tests/contract`, `agent/performer/tests` run separately); worktree runs need `PYTHONPATH=src:agent/performer/src`. Deterministic eval with five fixtures (clean, findings, hallucinated_anchor, prior_feedback, truncated); live eval on demand only.
**Target Platform**: the performer container (Debian, `coordinare-performer:extra`) for the workflow; the coordinare daemon for carrier dispatch, state lift, and reset.
**Project Type**: single repository, two packages (coordinare, performer).
**Performance Goals**: reviewing round under 10 minutes p90 including container start (SC-004, provisional); findings call 8000 completion tokens with one reprompt (FR-005); survey 12 commands plus one coverage pass (FR-003, FR-004).
**Constraints**: the reviewer writes nothing to the working tree; no write primitive in the toolkit (FR-014). Prose path byte-for-byte unchanged when workflow flag is off (FR-015). Every gate rule is a pure function with its own mutation test (Constitution II).
**Scale/Scope**: one new workflow package (about 8 modules), a persisted field, one payload field, one state reset rule, an eval with five fixtures, gate rules with mutation-test coverage.

## Constitution Check

| Principle | Check | Status |
| --- | --- | --- |
| I. Code Quality First | No dead code: every gate rule (FR-006 through FR-009) is a pure function with its own test. The toolkit is wired on day one without write primitive. No new dependencies. | PASS |
| II. Testing Discipline | Unit tests per step (intake, survey, findings, gate, post, report) and per gate rule; each rule test shown to fail under a mutation (standing rule, Constitution II). Contract test for the review_findings field in dispatch payload. Deterministic eval with five fixtures and a stubbed model in CI; live eval on demand only. | PASS |
| III. UX Consistency | Operator surface is one key (`workflow: reviewer`) matching 164/165/166/167. Block reasons name the condition (unanchored findings, unread files, post failure). Run record structure matches 164-167 patterns. | PASS |
| IV. Performance by Design | Budgets stated in spec table (8000 tokens per findings call, 12 commands per survey, under 10m p90 including container start). Provisional, replaced by SC-004 measurement from ten live rounds. | PASS |
| V. Clarity Before Action | Every decision in research.md cites code path (file:line) or measurement it rests on. No ambiguity about when workflow is active (flag presence), how anchors are verified (hunks + surveyed files), how prior comments are handled (rule-based addition to findings), how repairs are triggered (lane selection). | PASS |

No gate changed. Post-design re-check (after data-model and contracts): ready.

## Project Structure

### Documentation (this feature)

```text
specs/169-reviewer-workflow/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── finding.schema.json               # Model-facing findings call output
│   ├── review-record.schema.json         # Workflow run history (travels in report)
│   └── dispatch-payload-additions.md     # Row to add: review_findings, implementing-only
├── checklists/requirements.md
└── tasks.md                              # Produced by /speckit.tasks
```

### Source Code (performer, ~2000 lines)

```text
agent/performer/src/performer/
├── workflows/
│   ├── __init__.py                       # + "reviewer": (workflows.reviewer, "ReviewerWorkflow")
│   ├── adapter.py                        # reviewer toolkit: command_runner yes, screenshot/dom no
│   ├── toolkit.py                        # unchanged (already allows command_runner)
│   └── reviewer/
│       ├── __init__.py                   # ReviewerWorkflow: intake -> survey -> findings -> gate -> post -> report
│       ├── models.py                     # Finding, Disposition, ReviewRecord, ChangedFile, Hunk
│       ├── personas.py                   # REVIEW, SURVEY_COVERAGE personas
│       ├── intake.py                     # Parse diff into files/hunks, normalize prior comments
│       ├── diffparse.py                  # Unified diff parser: files, hunks, new-side line ranges
│       ├── survey.py                     # Read-only survey of code (inherit from architect)
│       ├── findings.py                   # Schema-guarded findings call and re-anchor
│       ├── gate.py                       # Pure functions: anchor validation, coverage, disposition, approval
│       ├── post.py                       # Post GitHub review, handle API errors
│       └── report.py                     # PerformerResponse.report shape + write-free check
├── models.py                             # Score: + review_findings (dict | None)
└── main.py                               # reviewing post-processing: workflow -> changes_requested or approved

src/coordinare/
├── state_store.py                        # schema 19: PersistedSession.review_findings (dict | None)
├── graph/nodes/monitor_performer.py      # lift report["review"] into the session as review_findings
├── graph/nodes/dispatch_performer.py     # inject_review_findings, reset_review_findings_for_reviewer
├── graph/nodes/check_board.py            # NOTE: no change
├── eval/reviewer_scenarios.py            # NEW: stubbed (CI) and --live eval runner over five fixtures
├── config.py                             # KNOWN_WORKFLOWS += "reviewer"
└── [implementer repair lane]             # workflows/implementer: plan.py select_lane add "repair"

tests/
├── unit/workflows/reviewer/              # Per module: models, diffparse, intake, survey, findings, gate, post, report, e2e
├── unit/workflows/implementer/plan.py    # select_lane with "repair" lane
├── unit/graph/nodes/                     # lift, dispatch reset/inject, review_findings field guard
├── contract/test_dispatch_payload.py     # + review_findings field (implementing-only)
└── eval/reviewer_scenarios/              # Fixtures (5), stub model, scoring, --live mode
```

**Structure Decision**: Mirror 164/165/166 exactly. The reviewer is a sibling workflow under `workflows/`; the carrier follows the blueprint/assessment path with lift in monitor and inject/reset in dispatch; gate rules are pure functions per Constitution II. Review findings are cleared on reviewer re-dispatch (like blueprint/assessment). The workflow is stateless within a round; state is on the card session where findings are persisted and reset.

## Complexity Tracking

| Item | Why it is needed | Simpler alternative rejected because |
| --- | --- | --- |
| Diff parser as separate module | Hunks/line-ranges are complex to parse from unified diff; a pure function with tests is reusable and testable (Constitution II). Other code may need this later (diff coverage, coverage gates). | Inlining it in intake makes testing harder; reusing architect's diff reading loses hunk structure needed for anchoring. |
| Gate rules as pure functions | Constitution II requires every rule with its own test; mutation testing is mandatory (standing rule). Bundling into one "validate findings" function hides which rule failed. | Monolithic gate cannot be tested or debugged per rule; mutation testing becomes impossible. |
| Review findings persisted on card | Implementer must read findings after restart (FR-012). Transient state (graph only, like qa_findings) loses it. | Daemon restart mid-lifecycle would abandon findings and leave implementer without repair context. |
| Reset on reviewer dispatch | A failed round must not leave stale findings behind (spec 166/167 precedent). | Without reset, implementer could consume stale findings from a prior round if next reviewer round times out. |
| Repair lane as a new lane | Findings change the milestones from the blueprint (file groups instead of blueprint milestones). A dedicated lane avoids conflating with chore/bug/feature. | Overloading an existing lane obscures the decision point (review_findings trigger); repair is its own lifecycle. |

## Phases

### Phase 0 Research (this plan)
Research the diff handling, diff parsing, prior-comment relay, posting, coordinare state, and repair-lane integration. Read code at file:line resolution. Output: research.md with R-a through R-f.

### Phase 1 Design (after plan approval)
Write data-model.md (ChangedFile, Hunk, Finding, Disposition, ReviewRecord). Write contracts/ (finding.schema.json, review-record.schema.json, dispatch-payload-additions.md). Write quickstart.md with enable/stubbed/live eval checklists.

### Phase 2 Implementation (after design approval)
Build performer workflows/reviewer in order: models, diffparse, intake, survey, findings, gate, post, report, e2e test with stubbed model. Build coordinare lift/reset/inject. Implement repair lane in implementer/plan.py. Build eval with five fixtures.

### Phase 3 Verification (before merge)
Run deterministic eval (all five fixtures must pass in CI). Run live eval on clean and findings fixtures to confirm against real gateway. Verify SC-001 through SC-006. Measure SC-004 on ten live rounds.

### Phase 4 Rollout (after merged)
Rebuild performer images. Enable per symphony with `workflow: reviewer` after live measurement confirms SC-004 and SC-005.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
