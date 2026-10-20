# Source Evidence Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox tracking.

**Goal:** Make pinned absence point to inventory candidates, stop repeat requests for proven-absent paths, require existing-path requests for unread-source gaps, and make author correction feedback state the broken rule; then rerun pinned Elm.

**Architecture:** Absence evidence gains an optional, bounded, deterministic candidate list derived from the pinned inventory and covered by the existing evidence digest. Review admission rejects repeat absent requests and unread-source gaps without an existing-path request, through the existing repairable error path. `_validation_feedback` adds fixed guidance for operation-limit and source-path errors.

**Tech Stack:** Existing Python 3.12+, Pydantic v2. No new dependency.

**Spec:** [source-evidence-resolution-amendment.md](source-evidence-resolution-amendment.md), approved as written by the maintainer on 2026-10-20. This plan awaits maintainer review.

## Global Constraints

- Candidates: inventory files whose final path component equals the request's; at most 8; plus a count of further matches; order: most trailing path components shared with the request, then shorter paths, then lexical.
- Candidates are hints, never citations; nothing resolves a finding automatically; absence never proves an obligation does not exist.
- Journal 9, source protocol 4, source contract 2 unchanged; every budget unchanged (schema 8, coverage 8 ceiling, configured run 8/8/2, 2,048 reads, 3,600 s).
- Repeat-absent and gap-request rejections are atomic and use the existing schema repair allowance; exhaustion stays a terminal hold.
- Revision limit stays 16 combined add/reassess; over-limit revisions are still rejected whole; only feedback text changes.
- Feedback never echoes candidate values or source content beyond daemon-owned inventory paths.
- No language, framework, repository or model-specific logic.

## Review Focus

1. A requested name matching many inventory files (for example `README.md` at 40 paths): the nested exact-suffix match ranks first and the overflow count is exact. Test in Task 1.
2. `list_files` absence for a directory that does not exist: candidates come from directories with the same final component, not files. Test in Task 1.
3. A journal recorded before this change (absence result without candidates) resumes and re-delivers evidence without being refused or upgraded. Test in Task 1.
4. A decision with one existing-path request and one repeat-absent request is still rejected (no partial binding). Test in Task 2.
5. A new finding (not only a decision) of kind `unread_source` whose only request is absent is rejected the same way. Test in Task 2.

## Task 1: Absence candidates

**Files:** Modify `packages/service_inference/src/coordinare_service_inference/environment/scope_investigation.py` (`ScopeInspectionResult`), `src/coordinare/services/environment_scope_investigation.py` (`_request_evidence`, `_bound_result`, `investigation_evidence`). Create `tests/unit/test_353_source_absence_candidates.py`.

**Interfaces (produced):**
- `ScopeInspectionResult.candidates: list[str] | None = None` (max 8) and `more_candidates: int = Field(default=0, ge=0)`; validator: candidates only with `status == 'absent'`; `None` means recorded before this amendment.
- `absence_candidates(path: str, paths: Sequence[str], *, directories: bool) -> tuple[list[str], int]` in the daemon module: same final component, ordering per Global Constraints, cap 8, returns the overflow count. With `directories=True` it matches directory paths derived from the inventory.
- Absence evidence payload gains `'candidates'` and `'more_candidates'`; new results record the same values. `investigation_evidence` recomputes the payload with candidates only when the recorded result has them, so pre-amendment results still match their digest.

- [ ] **Step 1: RED.** `test_nested_relative_reference_ranks_real_path_first` (inventory contains `pkg/scripts/gen.mjs`, `other/gen.mjs`; request `scripts/gen.mjs` -> `['pkg/scripts/gen.mjs', 'other/gen.mjs']`, 0). `test_common_name_caps_at_eight_with_exact_overflow` (40 `README.md` paths; the exact nested suffix first; `len == 8`, overflow `32`). `test_missing_directory_candidates_are_directories` (Focus 2). `test_absence_evidence_delivers_candidates_and_digest_binds_them` (run `investigate_scope_requests` on a fixture inspection; result and evidence carry the same list; changing one candidate in the stored result makes `investigation_evidence` raise `scope_investigation_identity`). `test_pre_amendment_absence_result_still_matches` (Focus 3: a result built with `candidates=None` and the old payload digest is accepted and delivered without candidates). `test_candidates_only_on_absent` (model rejects candidates on a file result).
- [ ] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_source_absence_candidates.py`; expect missing fields/function.
- [ ] **Step 3: Implement** the interfaces above. Ordering key: `(-shared_trailing_components, len(path), path)`. Only paths, never file contents, enter the payload.
- [ ] **Step 4: GREEN** plus `test_353_scope_investigation_flow.py`, `test_353_scope_investigation_recovery.py`, `test_353_scope_investigation_contract.py`, `test_353_source_decision_recovery.py`.
- [ ] **Step 5: Commit** `feat: point pinned absence evidence to inventory candidates`.

## Task 2: Admission rules for absent and unread-source requests

**Files:** Modify `src/coordinare/services/environment_scope_investigation.py` (`bind_scope_requests`), `src/coordinare/services/environment_scope.py` (`_validate_review`, `_messages` reviewer instructions). Create `tests/unit/test_353_source_absence_admission.py`.

**Interfaces:**
- Consumes Task 1 `ScopeInspectionResult.candidates`.
- `bind_scope_requests(..., results: Sequence[ScopeInspectionResult])`: before binding, any new request whose `(operation, path)` matches a recorded absent result in this attempt raises `ScopeInvestigationResponseError` with one error per offending request, code `scope_request_absent`, location `['scope_assessment', 'decisions'|'findings', i, 'inspection_requests', j]`, the recorded `candidates`, and fixed guidance ("This path is absent from the pinned inventory; request or read a listed candidate, or choose another disposition."). Nothing is bound or recorded.
- `_unread_source_errors(assessment, *, inventory: set[str], paths: Sequence[str]) -> list[dict]` in `environment_scope.py`: for every `SourceGapDecision` and new `ScopeFindingProposal` with `gap_kind == 'unread_source'`, require at least one request whose `read_file` path is in the inventory or whose `list_files` path is `''` or a prefix directory of an inventory path; else error code `scope_gap_request_invalid` at its `inspection_requests` location with fixed guidance. Raised through `_ResponseReferenceError` before ledger application.
- Reviewer instruction sentence: "An unread_source gap must request at least one file or directory present in the pinned inventory. Never re-request a path already reported absent; use its listed candidates."

- [ ] **Step 1: RED.** `test_repeat_absent_request_rejected_with_candidates` (scenario 2: second review requests the same absent path; one schema repair; ledger, bindings and results unchanged; feedback carries the candidates). `test_unread_source_with_only_absent_request_rejected` (scenario 3). `test_unread_source_with_existing_request_admitted`. `test_first_absent_request_alongside_existing_request_admitted`. `test_mixed_existing_and_repeat_absent_rejected_whole` (Focus 4). `test_new_unread_source_finding_needs_existing_request` (Focus 5). `test_reviewer_instructions_name_absent_rule`.
- [ ] **Step 2: Run RED.**
- [ ] **Step 3: Implement**; pass `budget.scope_investigation_results` into `bind_scope_requests`; run `_unread_source_errors` after the decision-ID and citation checks.
- [ ] **Step 4: GREEN** plus all `test_353_source_decision_*`, `test_353_scope_*` and `test_353_review_correction_contract.py`.
- [ ] **Step 5: Commit** `feat: reject repeat absent requests and unresolvable unread-source gaps`.

## Task 3: Actionable revision and source-path feedback

**Files:** Modify `src/coordinare/services/environment_admission.py` (`_validation_feedback`). Create `tests/unit/test_353_revision_feedback_guidance.py`.

**Interfaces:**
- `REVISION_LIMIT_GUIDANCE` and `SOURCE_PATH_GUIDANCE` constants. For `too_long` at `scope_revision/add|reassess` and for the combined `scope_revision_limit` error, attach the limit (16), the submitted count (from the error's `actual_length` context or the combined count) and the retention guidance. For `invalid_contract_value` at any location containing `sources`, attach the exact-path guidance.

- [ ] **Step 1: RED.** `test_oversized_revision_feedback_states_limit_count_and_retention` (scenario 4: 38 additions -> guidance contains `16`, `38` and "retained"; valid add content preserved on retry; retry consumes a schema repair, coverage counter unchanged). `test_combined_limit_feedback` (10 add + 10 reassess -> `16` and `20`). `test_malformed_source_path_gets_exact_path_guidance` (scenario 5: `./README.md/`). `test_feedback_never_echoes_candidate_values`.
- [ ] **Step 2: Run RED.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: GREEN** plus `test_353_scope_contract.py`, `test_353_scope_revision.py`, `test_353_scope_response_feedback.py`.
- [ ] **Step 5: Commit** `fix: state the broken rule in revision and source-path feedback`.

## Task 4: Verification, review, push and fresh Elm attempt

- [ ] **Step 1:** Focused suites (all `tests/unit/test_353_*`, `test_351_*`, `workflows/test_env_bootstrap.py`, performer contract lane), mypy, ruff, ruff baseline.
- [ ] **Step 2:** Independent review of the branch delta from `79d22f86` with the amendment, this plan and its Review Focus. Critical or Important findings come to the maintainer before a fix pass (review budgets are spent).
- [ ] **Step 3:** Exact-head preflight, quality guard, push, PR body update and readback, CI watch.
- [ ] **Step 4 (scenario 7):** With CI green, a fresh labelled pinned Elm attempt (`source-evidence-cov8-direct-haiku55-elm60m-high64k`) against `https://llm.vividynamics.com/v1` with the cov8 profile unchanged. Inspect any probe or manifest commands before judging. Readiness only with every acceptance gate; otherwise diagnose and report, no renewal.
