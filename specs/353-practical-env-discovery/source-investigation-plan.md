# Source Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Preserve the selected native execution method, with independent final review. Steps use checkbox tracking.

**Goal:** Enforce source investigation before scope revision and prove genuine pinned Elm bootstrap before shipping issue #353 / PR #354.

**Architecture:** Keep the existing generic manifest, runner and finding ledger. Add scope-specific structured inspection requests and a daemon-owned queue; admit revisions only after evidence delivery and cited responses. Reuse verified author evidence within an attempt while independent reviewers continue fetching the complete evidence set afresh.

**Tech Stack:** Existing Python 3.12+, Pydantic v2, asyncio and strict provider schemas. No new dependency or agent framework.

**Spec:** [source-investigation-amendment.md](source-investigation-amendment.md), approved by the maintainer's `approve` response. This implementation plan was approved by the maintainer’s subsequent `approve` response. Native execution is authorized.

## Global Constraints

- Pinned Elm: `bc224a78db6f669d56863599d653f14e7605de0b`; no other repository runs before Elm succeeds.
- Independent source and executable coverage plus every required deterministic clean-consumer check; no automatic finding closure or failed/unrun-check waiver.
- Eight requests per finding, 32 retained findings; existing 512 aggregate reads, phase/context bounds, call cap and original absolute deadline remain unchanged.
- Keep Haiku 5.5 high, strict JSON schema, 65,536 output tokens, 2 MiB conversation ceiling, 3,600-second attempt/request/phase/installer limits, 1,800-second private probe batch and 8/4/2 repair allowances for the recorded comparison.
- Preserve current runtime identity/resource controls, immutable manifest digest across installer and consumer, offline clean checks, lifecycle evidence and confirmed cleanup.
- No ecosystem catalogs, project recipes, repository source edits, constitution change, provider change, new privileges or egress change.
- Source content and credentials stay out of logs and journal metadata. Existing private evidence handling remains applicable.
- Required CI latency failure remains unresolved; no blind retry, retry-budget reset or 50 ms assertion change. Broad suite runs in CI; preflight and quality guard precede every push.

## Review Focus

1. A directory page is mistaken for file inspection or complete directory coverage: Task 2 pins exact operation/cursor identity and partial-page semantics.
2. A completed request is reused for another scope, commit, inventory or finding: Tasks 1 and 4 reject rebinding and corrupt recovery.
3. A file exists in inventory but fetch fails; treating failure as absence would hide obligations: Task 2 holds and retains the finding.
4. Author caching leaks into independent review or bypasses context/byte limits: Task 3 tests fresh reviewer fetches, hash drift and oversized cached context.
5. An inspection completes during cancellation but its finding or spent budget is lost: Task 4 tests interruption at fetch, checkpoint and revision boundaries.

## File Boundaries and Interfaces

- Create shared `packages/service_inference/src/coordinare_service_inference/environment/scope_investigation.py`: request/result/binding models only. It imports existing finding/path/digest primitives, not daemon code.
- Modify shared `environment/scope.py`: scope-only finding and decision proposal subclasses carrying inspection requests. Generic `findings.py`, executable assessment schemas, `ScopeContract` and manifest formats retain their current contracts.
- Create daemon `src/coordinare/services/environment_scope_investigation.py`: request binding, bounded execution and response admission.
- Modify `environment_scope.py` and `environment_response_schema.py`: source-review wire types, atomic ledger/queue admission and investigation-before-revision transitions.
- Modify `environment_evidence.py`, `environment_inspection.py`: bounded in-memory author evidence reuse; no reviewer reuse.
- Modify `environment_attempt.py`, `environment_repair.py`: journal version 8, source author protocol 3, queue persistence and original-budget recovery.
- Update existing validation harness reporting and add a replay script under `scripts/`; diagnostic artifacts remain ignored under `.agents/state/`.

## Task 1: Define scope-only inspection contracts and atomic bindings

**Files:** New shared `scope_investigation.py`; shared `scope.py`; daemon `environment_response_schema.py`, `environment_scope.py`; new `tests/unit/test_353_scope_investigation_contract.py`; explicit scope-only response fixtures in `tests/unit/test_353_scope_contract.py` and `tests/unit/scope_chat_fixture.py`.

**Interfaces:** `ScopeInspectionRequest` is a discriminated union of `{operation: 'read_file', path: str}` and `{operation: 'list_files', path: str, cursor: int >= 0}`. File paths use existing safe-path validation; directory root is canonical empty string. `ScopeFindingProposal(FindingProposal)` and `ScopeFindingDecisionProposal(FindingDecisionProposal)` each require `inspection_requests: list[ScopeInspectionRequest]`, maximum eight, with no default. Explicit `[]` means no delegated investigation is requested; omission is a schema error under protocol 3, never a silent empty queue.

`ScopeInspectionBinding` contains daemon ID `I` plus a full canonical SHA-256, finding ID, originating review sequence, scope digest, source commit, inventory digest and request. `ScopeInspectionResult` records binding ID, status `file | listing | absent`, content/page digest and file hash when applicable; it does not store file contents. Bound queue history permits at most 256 requests and 256 KiB serialized metadata, with explicit holds on overflow.

- [x] **Step 1: Write failing contract tests.** `test_rejects_ninth_request_and_exact_duplicates` asserts eight accepted, nine rejected, and duplicate `(operation,path,cursor)` rejected. `test_scope_requests_do_not_change_executable_assessment_schema` asserts generic manifest findings reject the new field. `test_binding_changes_for_each_identity_component` varies finding, review, scope, commit, inventory and request independently and asserts distinct IDs. Reject omission of `inspection_requests`, traversal, URL paths, invalid cursor/type and unknown fields. Update synthetic source-review fixtures explicitly; never inject empty requests into real model responses or replayed legacy transcripts.
- [x] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_scope_investigation_contract.py`; expect missing contract types or unsupported fields, not fixture failures.
- [x] **Step 3: Implement contracts and scope wire schemas.** Keep requests separate from inspected `sources`. Explicitly project scope-only proposals to base `FindingProposal`/`FindingDecisionProposal` before existing `apply_evidence_review`. After successful ledger validation assign new request bindings to the daemon-assigned finding IDs. Reject requests on resolved decisions or accepting verdicts. Validate the complete candidate queue before publishing either ledger or queue; no partial admission or automatic deduplication.
- [x] **Step 4: Run GREEN.** Run the new file plus `tests/unit/test_353_scope_contract.py`, `tests/unit/test_353_review_findings_contract.py` and `tests/unit/test_353_structured_wire.py`; assert existing manifest wire behavior and finding closure remain unchanged.
- [x] **Step 5: Commit** only the contracts, scoped schema wiring and their tests: `feat: bind source inspection requests to scope findings`.

## Task 2: Enforce investigation before revision and evidence-backed responses

**Files:** New `environment_scope_investigation.py`; modify `environment_scope.py`; new `tests/unit/test_353_scope_investigation_flow.py`.

**Interfaces:** `bind_scope_requests(*, assessment: ScopeAssessmentProposal, ledger: FindingLedger, scope_digest: str, inventory_digest: str, retained: Sequence[ScopeInspectionBinding]) -> list[ScopeInspectionBinding]` binds new and retained-open-finding requests using Task 1 types.

`async investigate_scope_requests(inspection: SourceInspection, bindings: Sequence[ScopeInspectionBinding]) -> list[ScopeInspectionResult]` executes only generic read/list operations under the original phase and attempt deadline. `validate_investigation_responses(*, bindings: Sequence[ScopeInspectionBinding], results: Sequence[ScopeInspectionResult], responses: Sequence[FindingResponse], inputs: Mapping[str,str]) -> None` rejects missing results, wrong identities and omitted file citations. Failures use stable bounded codes, never raw model/source values.

- [x] **Step 1: Write RED flows.** `test_nested_request_is_read_before_revision` uses unfamiliar paths and asserts fetch/result delivery precede the first revision model call. `test_old_file_read_does_not_satisfy_pending_request` asserts pending binding remains. `test_missing_file_differs_from_failed_fetch` distinguishes pinned absence from existing-but-unavailable content and retains the blocking finding. `test_listing_page_does_not_satisfy_file_or_next_page` asserts operation/cursor separation and retains `next_cursor` semantics.
- [x] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_scope_investigation_flow.py`; expect the premature revision transition to fail.
- [x] **Step 3: Implement queue execution and admission.** After an admitted `revise`, reserve coverage repair once, checkpoint requests, then execute pending investigations before author revision. Use bounded file batches of at most eight and the current directory paginator; charge real fetches to original read budgets. Only a safe path absent from the complete pinned inventory gets absence evidence. Existing-file read errors, policy denials and byte/deadline exhaustion hold. Deliver results and content to the author; successful file results require associated response citations. Keep findings open until explicit fresh independent decisions. Empty queues still permit existing source-backed revisions; they never imply acceptance.
- [x] **Step 4: Run GREEN.** Run the new flow tests with `test_353_scope_phase_contract.py`, `test_353_scope_revision.py`, `test_353_scope_pipeline.py` and `test_353_review_findings_flow.py`. Assert no probe, installer or clean-check call occurs during investigation.
- [x] **Step 5: Commit:** `feat: require source investigation before scope revision`.

## Task 3: Reuse author evidence without weakening independent inspection

**Files:** `environment_evidence.py`, `environment_inspection.py`, `environment_scope.py`; new `tests/unit/test_353_scope_author_evidence.py`; update `tests/unit/test_353_scope_revision_budget.py` only for the intentional fetch-count change.

**Interfaces:** `AuthorEvidenceCache` owns `(source_commit, inventory_digest)` and bounded exact `{path, content, sha256}` entries from admitted author evidence. `capture(inspection: SourceInspection) -> None` retains verified content; `restore(inspection: SourceInspection, expected_inputs: Mapping[str,str]) -> list[dict[str,str]]` verifies identity/hash and admits available cache entries against current phase byte bounds without spending new fetches. Missing entries use normal bounded inspection. The cache is passed explicitly only to author preparation; reviewer preparation retains `prepare_mandatory_evidence` and independent fetches.

- [x] **Step 1: Write RED counting tests.** `test_56_author_inputs_are_not_refetched_each_revision` asserts one author fetch per unchanged file, complete author evidence in every revision conversation, and one independent reviewer fetch per file per review. `test_cache_hash_identity_and_context_drift_hold` alters each identity/hash and exceeds the existing byte/context bounds. `test_cache_miss_uses_remaining_read_budget` proves missing content does not become a free inspected input.
- [x] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_scope_author_evidence.py`; reproduce repeated author fetches under the old implementation.
- [x] **Step 3: Implement author-only reuse.** Populate fresh author inspection inputs/source files only from verified cached bytes; keep per-phase byte accounting and complete-context admission. Preserve accumulated attempt counters and all retained hashes. Cache new author/investigation content after normal admission. A reviewer cannot receive or consult the author cache. Report actual inspections and repeated context delivery separately using existing metrics where possible.
- [x] **Step 4: Run GREEN.** Run the new tests with `test_353_scope_revision_budget.py`, `test_353_inspection.py`, `test_353_review_evidence.py` and `test_353_authoritative_provenance.py`. Update the old seven-read expectation to six (initial author 1, first reviewer 2, newly delivered author input 1, final reviewer 2), retaining its independent-review and insufficient-budget rejection assertions. Never increase its quota to conceal failure.
- [x] **Step 5: Commit:** `fix: reuse pinned author evidence across scope revisions`.

## Task 4: Persist investigation identity and recover without fresh budgets

**Files:** `environment_attempt.py`, `environment_repair.py`, `environment_scope_investigation.py`; new `tests/unit/test_353_scope_investigation_recovery.py`; existing `test_353_review_findings_journal.py`.

**Interfaces:** Journal version 8/source author protocol 3 add `scope_investigations` and `scope_investigation_results` using Task 1 models; `scope_phase` adds `investigation`. File content cache stays in memory. Recovery re-fetches missing content through normal bounded inspection; listings/absence are reconstructed only from matching pinned inventory and checked against recorded result digests. A persisted completion does not imply content is currently available to the author.

- [x] **Step 1: Write RED roundtrips.** `test_recovery_preserves_queue_findings_and_spent_budget` asserts unchanged start/expiry/read/repair counters and finding identities. `test_cancelled_fetch_cannot_become_completed` cancels during fetch, after result creation and before checkpoint. `test_unknown_or_rebound_queue_holds` corrupts protocol, originating review digest, request ID and source identity. `test_completed_binding_survives_later_scope_revision` verifies legitimate history remains available without rebinding it to the latest digest. `test_legacy_held_attempt_stays_terminal` asserts no restart. A binding matches the digest of its originating review sequence, not the latest revised scope or the finding's first appearance. Preserve completed historical bindings across legitimate later revisions; reject rebinding them to a different subject. Existing pending version-7 discovery holds for incompatible protocol; accepted legacy contracts retain their existing validation path, never an invented empty pending queue.
- [x] **Step 2: Run RED.** `uv run pytest -q tests/unit/test_353_scope_investigation_recovery.py`.
- [x] **Step 3: Implement versioned checkpoint validation.** Check all queue/result identities and aggregate metadata bounds before state publication. Resume the pending investigation transition before revision. Preserve all historical requests, results and findings; do not recreate spent coverage repairs. Missing in-memory cache requires charged reinspection or a hold. Cleanup failure cannot produce accepted/ready state.
- [x] **Step 4: Run GREEN.** Run the new tests with `test_353_review_findings_journal.py`, `test_353_execution_repair.py`, `test_353_runtime_cleanup_deadline.py` and scope flow tests. Assert original deadline and read-limit holds, not just successful recovery.
- [x] **Step 5: Commit:** `fix: preserve source investigation state across interruption`.

## Task 5: Replay the real failure, then prove Elm and shipping gates

**Files:** Create `scripts/replay_scope_investigation.py`; add `tests/unit/test_353_scope_investigation_replay.py`; modify `scripts/validate_environment_repositories.py` and its harness tests for separate source/executable review counts; update `validation-results.md` with actual results only.

**Interfaces:** Replay accepts `--transcripts PATH --output PATH`, does no network/model/project execution, and emits bounded JSON with replayed call count, requested investigation paths, source fetch counts, original held transition and corrected rejection. It must not label the old protocol's prose-only findings as structured requests or invent approvals. A separate deterministic fixture supplies the new structured requests and exercises the corrected end-to-end discovery flow.

- [ ] **Step 1: Write RED evidence tests.** `test_replay_reports_repeated_old_source_reads` uses synthetic transcript envelopes committed with the test to verify repeated reads do not become new investigation. The separate private replay must verify the real 23 saved responses contain only the old-file reread after authoring. `test_structured_request_blocks_premature_revision` exercises new wire admission without upgrading old responses. `test_completed_queue_cannot_waive_conflict_or_clean_failure` keeps a genuine conflict open, then separately rejects an accepted-manifest execution with a failed/unrun required check. Harness reporting distinguishes four source reviews from zero executable reviews in the saved run.
- [ ] **Step 2: Run RED, implement replay/reporting, then GREEN.** `uv run pytest -q tests/unit/test_353_scope_investigation_replay.py tests/unit/test_353_validation_harness.py tests/unit/test_353_scope_runtime_acceptance.py tests/unit/test_353_runtime_acceptance.py`. Run replay against `review-resolution-premerge-haiku55-elm60m-high64k/elm` and inspect its output; do not require private transcripts in ordinary CI.
- [ ] **Step 3: Verify and publish the implementation.** Run focused scope/investigation/journal/manifest regressions, lint and strict mypy. Obtain independent review of the actual delta and execute its claims. Commit, run ship-issue preflight/check and quality guard before push; preserve spent counters. Use T3 CI watching for the broad suite. The unrelated 50 ms CI failure remains an explicit blocker until diagnosed/resolved without weakening the assertion; no quota reset or unverified rerun.
- [ ] **Step 4: Run fresh pinned Elm.** Prepare a new labeled isolated profile from the exact tested source; verify the approved source pin, image/platform identities, resources and available storage. Retain model/runtime/budgets from Global Constraints, all earlier held evidence and known CI state. Inspect exact probe/manifest command scopes and hashes before authorized execution markers. Observe existing handles rather than restarting when an observation times out.
- [ ] **Step 5: Audit actual acceptance and cleanup.** Require accepted independent source coverage, accepted independent executable coverage, one unchanged manifest digest through installer and clean consumer, every required check and service lifecycle exit/status, pinned source/image/platform identity, offline clean operation and confirmed cleanup. Record stage-specific failures honestly. If Elm holds, retain evidence and diagnose; do not mark Task 5, issue or goal complete. Resume remaining repository acceptance only after genuine Elm success.
- [ ] **Step 6: Ship only through all existing gates.** Complete remaining sample acceptance, full-PR adversarial/Copilot findings, final-head required CI and existing issue/spec requirements. Merge via ship-issue/merge-pr only after their evidence audit; verify issue closure and unwatch before final user handoff. This plan does not redefine issue completion around its narrower correction.

## Self-Review and Handoff

Spec acceptance scenarios 1–3 map to Task 2; scenario 4 to Task 3; scenario 5 to Tasks 2/5; scenario 6 to Task 4; scenario 7 to Task 5; scenario 8 and all original shipping requirements to Task 5. Identity, bounds and protocol ownership are defined in Task 1 and consumed unchanged thereafter. All five Review Focus cases have owning regression tests.

Self-review follow-up: made request lists mandatory on the new wire protocol, identified explicit source-only fixture updates, and distinguished originating review identity from legitimate later scope changes.

Native implementation is the preserved execution method. The maintainer approved this plan and its native execution. Execute Tasks 1–5 in order with TDD and independent final review; remain on the existing isolated feature branch.
