# Source Review Decision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Native execution was selected by the maintainer before plan review, with independent final review. Steps use checkbox tracking.

**Goal:** Replace free-form source review statuses with one structured, evidence-bearing disposition per finding, persist the bound assessment, then prove genuine pinned Elm bootstrap before shipping issue #353 / PR #354.

**Architecture:** The source reviewer returns a discriminated union per retained finding. The daemon validates it atomically, derives the existing generic `FindingDecisionProposal` deterministically and applies the unchanged generic ledger. The typed assessment is persisted with its review identity and bound into a version 2 `ScopeContract`; journals move to version 9 / source protocol 4 and legacy state fails closed.

**Tech Stack:** Existing Python 3.12+, Pydantic v2 discriminated unions, strict provider JSON schemas. No new dependency, agent or keyword classifier.

**Spec:** [source-review-decision-amendment.md](source-review-decision-amendment.md), approved as written by the maintainer on 2026-10-20. This plan was approved by the maintainer on 2026-10-20 with native execution; the docs commit `3dfe0efc` is pushed together with this plan.

## Global Constraints

- Dispositions exactly `covered`, `source_correction`, `source_gap`, `duplicate`; derived status `resolved`, `resolved`, `open`, `resolved` with duplicate link.
- Source gap kinds exactly `missing_obligation`, `unread_source`, `source_conflict`, `unknown_applicability`, `incomplete_runtime_needs`, `incomplete_success_condition`. No runtime-wait or scope-acceptance-wait kind.
- No authored `status` field on source decisions; no prose inspection or keyword rewriting anywhere.
- Bounds unchanged: 32 findings/decisions, 128 criteria, 8 inspection requests per finding, 2048-byte UTF-8 text, existing ledger (128 KiB), contract (256 KiB) and journal (4 MiB) limits.
- Versions: source protocol 4, journal 9, `ScopeContract.schema_version` 2. Executable review protocol, `CoverageAssessment` and `FindingLedger` schemas unchanged.
- Legacy contracts and journals are readable for audit only, never upgraded to accepted; old manifest/execution evidence is invalidated, never relabelled.
- Spent budgets carry over: CI verified retries 2/2, review fix rounds 2/2, rebases 0. A further fix round or CI retry needs maintainer approval.
- Elm profile for the fresh run, unchanged from the last labelled experiment: claude-haiku-5-5 high, strict json_schema, 65,536 output tokens, 3,600-second attempt/phase/request/installer, 4 MiB conversation cap, 2,048 aggregate reads, schema 8 / coverage 4 / execution 2 repairs, private probe batch 1,800 seconds, at most two batches.
- Pinned Elm `bc224a78db6f669d56863599d653f14e7605de0b`; Dettmore only after genuine Elm acceptance.
- Private transcripts, source content and credentials never enter committed files, logs or operator summaries.

## Review Focus

1. A `covered` or `source_gap` decision cites a criterion ID that a later revision replaced: reject with the current criterion IDs listed, preserve the attempted assessment. Test in Task 2.
2. A previously resolved finding is reassessed as `source_gap` in a later review: the ledger reopens it and acceptance holds. Test in Task 2.
3. Duplicate chains or cycles (`F0002 -> F0003 -> F0002`): rejected by the existing ledger rule, no partial publication. Test in Task 2.
4. An `accept` verdict alongside a new `source_gap` finding or any `source_gap` decision: rejected as a verdict contradiction. Test in Task 2.
5. Recovery restores a typed assessment from an earlier review sequence than the ledger's latest: refuse rather than accept a stale binding. Test in Task 3.

## Pre-implementation gate

- [ ] Append an "Approved source review decision amendment" section to `specs/353-practical-env-discovery/tasks.md` listing Tasks 1-5 below, then run `/speckit.analyze` and resolve any CRITICAL finding before Task 1.

## Task 1: Shared disposition contracts, normalization and contract v2

**Files:** Modify shared `packages/service_inference/src/coordinare_service_inference/environment/scope.py`. Create `tests/unit/test_353_source_decision_contract.py`.

**Interfaces (produced):**
- `SourceGapKind = Literal[...]` (six kinds above).
- `_SourceDecision(FindingEvidence)`: `finding_id: FindingId`, `rationale: str`, `sources: list[str] = Field(min_length=1)`.
- `CoveredDecision`: `disposition: Literal['covered']`, `criterion_ids: list[str] = Field(min_length=1, max_length=128)`, unique.
- `SourceCorrectionDecision`: `disposition: Literal['source_correction']`, no further fields.
- `SourceGapDecision`: `disposition: Literal['source_gap']`, `gap_kind: SourceGapKind`, `affected_ids: list[str] = Field(max_length=128)` unique, `inspection_requests: list[ScopeInspectionRequest] = Field(max_length=8)` via `unique_requests`.
- `DuplicateDecision`: `disposition: Literal['duplicate']`, `duplicate_of: FindingId`.
- `SourceFindingDecision = Annotated[CoveredDecision | SourceCorrectionDecision | SourceGapDecision | DuplicateDecision, Field(discriminator='disposition')]`.
- `derive_finding_decision(decision: SourceFindingDecision) -> FindingDecisionProposal`.
- `ScopeFindingProposal` gains `gap_kind: SourceGapKind` (new findings use the gap representation). `ScopeFindingDecisionProposal` is removed once Task 2 migrates its users.
- `ScopeAssessmentProposal.decisions: list[SourceFindingDecision] = Field(max_length=32)`; `no_accepted_requests` reads requests only from `SourceGapDecision`.
- `SourceAssessmentRecord(FindingEvidence)`: `schema_version: Literal[1] = 1`, `review_sequence: int = Field(ge=1)`, `scope_digest: Digest`, `source_commit: Commit`, `inventory_digest: Digest`, `reviewer_inputs: dict[str, Digest] = Field(min_length=1)`, `assessment: ScopeAssessmentProposal`.
- `validate_source_assessment(record: SourceAssessmentRecord, *, ledger: FindingLedger) -> None`, raising `PydanticCustomError('source_assessment_binding', ...)`.
- `ScopeContract.schema_version: Literal[1, 2] = 2`, `source_assessment: SourceAssessmentRecord | None = None`, property `current -> bool`.

- [ ] **Step 1: Write RED tests.** `test_each_disposition_derives_generic_status` asserts `covered`/`source_correction` -> `resolved` with `duplicate_of=None`, `source_gap` -> `open`, `duplicate` -> `resolved` with `duplicate_of` set, with rationale and sources copied verbatim. `test_rejects_authored_status_and_runtime_wait_kinds` rejects a decision with `status`, a missing `disposition`, `gap_kind='pending_execution'` and `gap_kind='awaiting_scope_acceptance'`. `test_resolving_dispositions_reject_inspection_requests` rejects `inspection_requests` on `covered`, `source_correction` and `duplicate`. `test_covered_requires_unique_criteria` rejects `[]` and repeated IDs; `test_decision_citations_nonempty_unique` rejects `[]` and duplicate sources. `test_new_finding_requires_gap_kind`. `test_assessment_binding_matches_ledger_review` builds a ledger with `apply_evidence_review` from derived decisions and asserts `validate_source_assessment` passes, then fails separately for changed sequence, scope digest, reviewer input hash, source commit, one derived decision and one added finding. `test_contract_v2_requires_accepting_latest_assessment` rejects a v2 contract with no record, a record whose `review_sequence != len(ledger.reviews)`, verdict `revise`, or mismatched `review_inputs`/`sources`. `test_v1_contract_parses_for_audit_with_unchanged_digest` loads a saved v1 contract dict and asserts `current is False` and `digest()` equals the digest computed before this change (the serializer omits `source_assessment` for v1).
- [ ] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_source_decision_contract.py`; expect import errors for the new types.
- [ ] **Step 3: Implement** the types and functions above in `scope.py`. `validate_source_assessment` compares `[derive_finding_decision(d) for d in decisions]` with `ledger.reviews[seq-1].decisions`, and each added record's proposal with the finding projected through `model_dump(exclude={'inspection_requests', 'gap_kind'})`. Add a `model_serializer(mode='wrap')` to `ScopeContract` that drops `source_assessment` when `schema_version == 1`, matching the manifest serializer pattern. In `validate_scope_mapping`, raise `PydanticCustomError('scope_contract_refresh_required', ...)` unless the contract is current.
- [ ] **Step 4: Run GREEN.** The new file plus `tests/unit/test_353_scope_contract.py`, `tests/unit/test_353_review_findings_contract.py` and `tests/unit/test_353_scope_investigation_contract.py` (fixtures in those files that construct scope decisions are updated in Task 2; note any failures here as expected and confined to old `status` fixtures).
- [ ] **Step 5: Commit:** `feat: define structured source review dispositions`.

## Task 2: Atomic admission, provider schema and repair feedback

**Files:** Modify `src/coordinare/services/environment_scope.py` (`_validate_review`, `_discover_scope`, `_messages`, `_phase`), `environment_response_schema.py` (`response_schema`, `_review_constraints`), `environment_admission.py` (`_validation_feedback`), `environment_scope_investigation.py` (`bind_scope_requests`). Create `tests/unit/test_353_source_decision_flow.py`. Migrate source-review fixtures in `tests/unit/scope_chat_fixture.py` and the scope tests that emit `status` decisions (`test_353_scope_contract.py`, `test_353_scope_phase_contract.py`, `test_353_scope_investigation_flow.py`, `test_353_scope_investigation_recovery.py`, `test_353_scope_investigation_replay.py`, `test_353_scope_revision_budget.py`, `test_353_scope_response_feedback.py`, `test_353_scope_author_evidence.py`), and replace the remaining `ScopeFindingDecisionProposal` references in `environment_response_schema.py` and `test_353_scope_investigation_contract.py`.

**Interfaces:**
- Consumes Task 1 types.
- `_validate_review(...) -> tuple[ScopeAssessmentProposal, FindingLedger, SourceAssessmentRecord]`; `_discover_scope` publishes `budget.scope_assessment = record` with the ledger and passes `source_assessment=record` into `ScopeContract`.
- `_decision_reference_errors(assessment: ScopeAssessmentProposal, *, definition: ScopeDefinition, retained_ids: set[str]) -> list[dict[str, Any]]` with code `scope_decision_criteria_invalid`, locations `['scope_assessment', 'decisions', i, 'criterion_ids' | 'affected_ids']` and `['scope_assessment', 'findings', i, 'affected_ids']`; the first error carries `current_criterion_ids`. Raised through `_ResponseReferenceError` so `_phase` forwards every error with guidance.
- `response_schema(phase, *, ..., review_decision_ids=None, review_criterion_ids: Sequence[str] | None = None)`. `_phase` passes `budget.scope_definition` criterion IDs for `scope_assessment`.
- `SOURCE_DISPOSITION_GUIDANCE` constant in `environment_admission.py`.

- [ ] **Step 1: Write RED flows (spec scenarios 1-5 and Review Focus 1-4).** `test_covered_resolves_runtime_only_objection_without_execution` asserts a `covered` decision for a runtime-proof objection yields an accepted v2 contract with no probe, installer or check call. `test_unread_source_gap_holds_with_requests` asserts revise, open finding and bound inspection requests. `test_mixed_gap_stays_open_and_runtime_obligation_remains_required`. `test_old_status_decision_rejected_with_disposition_guidance` asserts the repair message contains `SOURCE_DISPOSITION_GUIDANCE`, the ledger is unchanged and the schema repair counter increments by one. `test_stale_criterion_id_rejected_with_current_ids` (Review Focus 1). `test_resolved_finding_reopened_as_gap_blocks_acceptance` (Focus 2). `test_duplicate_cycle_rejected_atomically` (Focus 3). `test_accept_with_gap_is_verdict_contradiction` (Focus 4). `test_source_correction_keeps_required_criteria` asserts the accepted contract retains every required criterion. `test_missing_decision_holds_within_budget`. `test_scope_schema_branches` asserts the provider schema exposes four `anyOf` branches without a `status` property or `discriminator` keyword, `finding_id`/`duplicate_of` enums from retained IDs, `criterion_ids`/`affected_ids` item enums from current criteria with `minItems: 1` on `criterion_ids`, `gap_kind` enum of exactly six values, and that the executable `assessment` schema is byte-identical to before.
- [ ] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_source_decision_flow.py`.
- [ ] **Step 3: Implement admission.** Order: parse envelope, decision ID set, assessment citations, `_decision_reference_errors`, then build `CoverageAssessmentProposal` from `derive_finding_decision` and findings projected without `inspection_requests`/`gap_kind`, then the unchanged `apply_evidence_review`, then `bind_scope_requests` (requests come only from `SourceGapDecision` and new findings), then the record. Nothing on the budget changes until every step passes. Replace the decision text in `_messages` with disposition instructions: one disposition per retained ID; `covered` cites current criterion IDs whose obligations and success conditions answer the objection; future execution is never a gap; list the six gap kinds. Remove the old status/duplicate wording; keep the phase contract and source-only reminders.
- [ ] **Step 4: Implement schema and feedback.** In `_review_constraints`, branch on phase: the executable path is unchanged; the scope path rewrites the decisions item to `anyOf` of the four branch definitions and applies the enums above. In `_validation_feedback`, allow fields `disposition`, `criterion_ids`, `gap_kind`, `inspection_requests` and the four tag names; map `union_tag_not_found`, `union_tag_invalid` and `extra_forbidden` at a `status` location to `SOURCE_DISPOSITION_GUIDANCE`. Guidance never echoes candidate values.
- [ ] **Step 5: Migrate fixtures** to `covered` decisions citing current criterion IDs (from the request's `scope_definition`) or `source_gap` decisions with explicit `gap_kind`. Do not change any test's asserted outcome.
- [ ] **Step 6: Run GREEN.** The new file, every migrated file, `test_353_scope_pipeline.py`, `test_353_scope_revision.py`, `test_353_review_findings_flow.py`, `test_353_structured_wire.py`.
- [ ] **Step 7: Commit:** `feat: admit source review decisions by explicit disposition`.

## Task 3: Persistence, recovery and fail-closed migration

**Files:** Modify `environment_attempt.py` (`AttemptBudget`), `environment_repair.py` (`RepairJournal`, `journal_for`, `_validate_scope_checkpoint`, `_save_scope_checkpoint`, budget restore, `restart_environment_attempt`). Create `tests/unit/test_353_source_decision_recovery.py`; update version expectations in `test_353_review_findings_journal.py` and `test_353_scope_investigation_recovery.py`.

**Interfaces:**
- `AttemptBudget.scope_author_protocol: Literal[4] = 4`; `AttemptBudget.scope_assessment: SourceAssessmentRecord | None = None`.
- `RepairJournal.version: Literal[9] = 9`, `scope_author_protocol: Literal[4]`, `scope_assessment: SourceAssessmentRecord | None = None`; `CURRENT_JOURNAL_VERSION = 9`.

- [ ] **Step 1: Write RED recovery tests (spec scenario 6, Review Focus 5).** `test_roundtrip_preserves_assessment_and_spent_counters` asserts the record, ledger, start/expiry and every counter survive save/restore. `test_recovery_rejects_changed_bindings` corrupts the record's scope digest, one reviewer input hash, source commit and one disposition (so its derived decision differs from the ledger) and expects `scope_protocol_invalid` or `scope_source_identity`. `test_recovery_rejects_stale_assessment_sequence` (Focus 5). `test_reviews_without_assessment_hold`. `test_save_refuses_assessment_regression`. `test_v8_journal_requires_restart` covers pending, held and accepted v8 journals and asserts `legacy_attempt_requires_restart`, with the stored dict unchanged and its counters not renewed. `test_restart_archives_v8_unchanged` asserts the archive holds the original v8 dict and the new attempt is a fresh sibling. `test_legacy_v1_contract_manifest_is_not_ready` stores a manifest bound to a v1 contract with passing verification evidence and asserts `accepted_environment(cache) is None`.
- [ ] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_source_decision_recovery.py`.
- [ ] **Step 3: Implement.** `journal_for` refuses every version 6, 7 and 8 payload with `legacy_attempt_requires_restart` (the v6/v7 accepted-contract upgrade path is removed because those contracts lack an assessment). `_validate_scope_checkpoint` requires `scope_assessment` present exactly when `scope_findings` has reviews, calls `validate_source_assessment`, requires `review_sequence == len(scope_findings.reviews)`, and in phase `accepted` requires `scope_contract.current` and `scope_contract.source_assessment == scope_assessment`. `_save_scope_checkpoint` copies the record and refuses a lower sequence. Restore copies it to the budget. `restart_environment_attempt` accepts versions 1-9 for archival and validates legacy data with protocol 4 for parsing only, archiving the original dict.
- [ ] **Step 4: Run GREEN.** The new file with `test_353_review_findings_journal.py`, `test_353_scope_investigation_recovery.py`, `test_353_execution_repair.py`, `test_353_review_findings_acceptance.py`, `test_353_runtime_cleanup_deadline.py`.
- [ ] **Step 5: Commit:** `feat: persist bound source assessments with fail-closed migration`.

## Task 4: Readiness regression, integration verification and push

**Files:** Add to `tests/unit/test_353_scope_runtime_acceptance.py`. Update `specs/353-practical-env-discovery/validation-results.md` only with verified facts.

- [ ] **Step 1: Write the readiness regression.** `test_covered_scope_still_requires_every_passing_check` drives the fixture pipeline with an all-`covered` accepted v2 contract, then (a) one required check fails, (b) one required check is missing from the report, (c) one service lifecycle step is unrun; each must leave `accepted_environment` `None` and the attempt held. Run it; it should pass immediately (it pins existing behavior under the new contract). If it fails, stop and diagnose before continuing.
- [ ] **Step 2: Focused verification.** All test files touched in Tasks 1-4, `uv run --extra dev mypy -p coordinare`, `uv run --extra dev ruff check src tests agent/performer/src`, ruff baseline ratchet.
- [ ] **Step 3: Independent review.** Use superpowers:requesting-code-review with a precise brief (spec, this plan, the branch diff from `86e6f427`), no session history. Review fix rounds are spent (2/2): if the reviewer reports Critical or Important issues, stop and bring them to the maintainer before fixing.
- [ ] **Step 4: Exact-head preflight and quality guard** per `.agents/state/353-preflight-commands.md`, then push (`3dfe0efc`, plan commit, Tasks 1-4), update and read back the PR body from `.agents/state/353-pr-body-scope-ledger.md`, and `watch_pull_request`. CI retries are spent: a red required job is diagnosed, never blindly retried.

## Task 5: Corrected-protocol diagnostic, fresh Elm attempt and shipping

**Files:** Private only, under `.agents/state/`. No committed transcripts.

- [ ] **Step 1: One-call diagnostic (spec scenario 7).** Derive `.agents/state/353-diagnose-source-decisions.py` from `353-diagnose-review-boundary.py`: rebuild the saved third-review request (`chat-019.json` inputs, re-hashed against the pinned snapshot) with the new `_messages` and schema, make one model call, write `profile.json`, `response.json`, `result.json` under `.agents/state/353-org-validation/source-decision-one-call-diagnostic/`. Report per finding the disposition and whether admission passed. Expected, not required: F0003/F0005/F0006 `covered`, F0007 `source_gap/unread_source`. No acceptance claim, no project execution, snapshot unchanged.
- [ ] **Step 2: Fresh pinned Elm attempt.** Derive a visibly new launcher `.agents/state/353-run-source-decisions-haiku55-elm60m-high64k.py` from the last one with the Global Constraints profile, fresh output directory and exact-head guard bound to the pushed head. Inspect every probe/manifest execution command, pinned input hash and image/runtime identity before it runs. Never resume or reuse the terminal run.
- [ ] **Step 3: Judge only real outcomes.** Elm is ready only when all seven acceptance items in the handoff hold: fresh accepted source and executable coverage, one manifest digest through install and offline clean consumer, every required check and service lifecycle step passed, full provenance and runtime identity, no secret leak, confirmed cleanup. A hold is diagnosed from its evidence and reported; it does not renew budgets.
- [ ] **Step 4: Remaining acceptance.** After Elm success, Dettmore at `bf498e843ba58362dc6abe122b91129f7c66351f` and the original spec's sample and baseline-repository retests, then the full PR adversarial and Copilot review, current-head required CI, and merge per merge-pr with `--squash --admin` only when every original requirement is proved.
