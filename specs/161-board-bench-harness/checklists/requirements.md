# Specification Quality Checklist: Rank Backend Harnesses Per Role

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-09-04
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

Validation performed 2026-09-04. Findings from the pass, and how they were resolved:

1. **File and symbol names removed.** The first draft named `score.py`,
   `PersonaDispatch` and `benchmarks/spaces/default.yaml` in requirements. These are
   implementation details and were replaced with capability language ("per-dispatch
   record", "search space"). Concrete file paths belong in plan.md, where they now go.

2. **Zero [NEEDS CLARIFICATION] markers, deliberately.** Both decisions that would
   otherwise be marked (harness-failure semantics, model scope) were settled by the
   requester before drafting and are recorded in Clarifications instead.

3. **Terminal markers retained intentionally.** The marker vocabulary
   (`changes_requested`, `malformed_output`, `env_blocked`, ...) is domain vocabulary
   from the observed system, not an implementation choice. Naming the markers is what
   makes FR-002 through FR-006 testable, so the "no implementation details" item is
   judged to pass. Removing them would make the central distinction untestable.

4. **Scope boundary tightened.** Added explicit out-of-scope entries for writing live
   config (FR-023) and for repairing any defect the benchmark reveals, both of which
   were ambiguous in the first draft.

5. **Zero-vs-absent evidence made explicit.** FR-013, FR-021 and the edge cases now
   distinguish "no evidence" from "a score of zero", which the first draft conflated.
   This was the most substantive correctness gap found in review.

## Post-analysis remediation (2026-09-04)

`/speckit.analyze` found ten issues; nine were remediated and one cannot be fixed by
editing artifacts. Recording them because three were genuine design defects, not wording.

**Critical, fixed:**

1. **F1 — pooling silently disabled the tie rule.** FR-025 pooled dispatches into one row
   per (role, backend), collapsing the sample to a single value. FR-020's tie rule needs a
   standard deviation, so it would have degraded to exact-equality without any error.
   Fixed by retaining `per_run_scalars` alongside the pooled counts, and by flagging
   `single_run` so an unmeasured band is never reported as a measured tie.
2. **F2 — FR-011's guarantee was unsatisfiable.** It promised existing scalars be
   "byte-identical", while the plan adds a view discriminator to `ScoreObject`. Verified
   that any new field changes its 13-key JSON output. Reworded as a guarantee about
   computed **values**, with an explicit `SCORE_SCHEMA_VERSION` bump and a
   `CONFIG_COMPARISON` default.

**High, fixed:**

3. **F3 — T003 could not run.** Verified there is no `runs/` directory and no `run.json`
   anywhere in the tree, so "locate an existing artifact" would silently no-op. Now
   generates one via the stub runner, which still needs no inference host.
4. **F4 — US2 was not actually independent.** FR-018 required a failure be "recorded as a
   defect", which needs US1's classifier, contradicting the parallel-work claim. FR-018
   narrowed to *recording* the failed dispatch; classification stays US1's job.
5. **F5 — unit error in the objective.** The cost term divided a raw token count by
   `cost_budget_usd`. Now converts via `estimate_cost_usd()` first, and omits the term
   when tokens are unknown rather than assuming zero cost.

**Medium/low, fixed:** F7 (FR-018 test moved to a sweep test file), F8 (added FR-027 for
`no_viable_harness`), F9 (round-trip guard stated for `HarnessRollup`), F10 (T001 scoped
to a smoke check).

**Not fixed, and not fixable here:**

6. **F6 — constitution Gate 8.** The constitution requires an approving review from a
   non-author. Solo mode cannot provide one. The adversarial `Workflow` review is a
   substitute, not an equivalent. This is recorded as an open deviation in plan.md rather
   than reinterpreted, and it is the requester's call to accept or to review the PR
   personally.

Mutation checks were extended to cover the new rules (F1, F2, F5) so each fix is pinned by
a test that has been proven to fail when the rule is broken.
