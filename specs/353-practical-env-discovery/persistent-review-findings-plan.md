# Persistent Coverage Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unresolved coverage objections disappearing across fresh reviews while preserving mandatory deterministic clean execution.

**Architecture:** Store structured findings and their adjudication history in the logical attempt. Discovery proposes responses; each fresh reviewer explicitly decides every finding against the current candidate and independently fetched evidence. Shared versioned review validation gates daemon, performer and saved readiness.

**Tech Stack:** Existing Python 3.12+, Pydantic v2, asyncio, canonical JSON/SHA-256 and current provider/runtime adapters. No new dependency.

**Spec:** [persistent-review-findings-amendment.md](persistent-review-findings-amendment.md), committed as `9f5d6d81` and approved by the user with “Approve”. Preserve the previously selected native execution method: sequential implementation in this worktree, then one fresh independent whole-diff review.

## Global constraints

- Manifest schema/digest remain unchanged; coverage assessment becomes version 2 and attempt journal version 5. Old assessments remain historically parseable but cannot establish current-policy readiness.
- Maximum 32 findings, 2 KiB UTF-8 per issue/rationale field and 128 KiB serialized ledger including history. Never silently truncate; keep existing 256 KiB assessment bound and context/read bounds.
- Two schema corrections, two coverage revisions and two executable repairs by default; original aggregate deadline, call/read counters and cleanup ownership remain authoritative.
- Existing required IDs/status/scopes and source/image/platform identity cannot weaken. No new privileges, host sockets, verification networking, automatic scope reduction or ecosystem-specific core recipes.
- Other repositories remain paused until actual Elm success. No merge before existing sample/Elm, final-head CI and review gates. Full suite uses CI; scope approvals inspect exact commands and digests.

## Review focus

1. A reviewer labels an earlier objection non-blocking without deciding its ID: schema-invalid, never acceptance (Task 3).
2. A candidate changes after a finding closed: require a fresh current-candidate decision before executing (Tasks 1, 3).
3. A crash occurs after review but before the next request: persist ledger/counters atomically and do not renew allowances (Task 2).
4. A valid false positive cannot be closed, or a genuine prerequisite is excluded for convenience: permit cited independent resolution while preserving required obligations and fixed scope (Task 3).
5. Legacy ready caches or performer metadata bypass the new policy: shared validation rejects reuse before project commands (Task 4).

## File responsibilities and shared interfaces

Create `packages/service_inference/src/coordinare_service_inference/environment/findings.py` for strict finding/evidence models and pure ledger validation. It imports Pydantic directly, not `environment.models`, avoiding a cycle. `environment.models` imports these types and retains ownership of manifest/assessment/proposal models.

Create `src/coordinare/services/environment_findings.py` for daemon-owned ID allocation, evidence binding and review transitions. Keep model calls/source I/O in existing discovery/inspection modules; do not further enlarge the discovery module with ledger algorithms.

Define these shared types in `findings.py`:

- `FindingProposal`: `category`, `issue`, `sources: list[str]`, `affected_ids: list[str]`; no daemon identity/provenance. Categories are the five approved spec values.
- `FindingResponse`: `finding_id`, `rationale`, `sources`, `affected_ids`.
- `FindingDecisionProposal`: `finding_id`, `status: Literal['open','resolved']`, `rationale`, `sources`, `duplicate_of: str | None`. Duplicate closure must reference a different retained finding and cannot form a cycle.
- `FindingRecord`: daemon `id` (`F0001` onward), original proposal, originating manifest digest, source commit and source hashes. Original record is immutable across transitions.
- `FindingReview`: daemon sequence, manifest digest, independently verified inputs, decisions and IDs of newly added findings. Each retained ID receives exactly one decision per completed review; newly added findings start open.
- `FindingLedger`: schema version 1, source commit, immutable records, review history; `digest() -> str` hashes canonical UTF-8 JSON. A current accepted snapshot requires a completed review for the exact candidate, every retained record explicitly resolved, and no invalid/missing references.
- `validate_finding_ledger(ledger: FindingLedger, *, manifest_digest: str, source_commit: str, inputs: Mapping[str, str]) -> None`: checks bounds, history/references, canonical source bindings and current closure. Raises `ValueError` without including source/model text.

Daemon helper interfaces in `environment_findings.py`:

- `validate_finding_responses(responses: list[FindingResponse], *, ledger: FindingLedger, inspected: Mapping[str, str], manifest: EnvironmentManifest) -> None` requires exactly every currently open ID, unique known IDs, read citations and valid affected manifest IDs. Optional responses to closed findings are allowed only once per ID.
- `apply_finding_review(ledger: FindingLedger, *, manifest: EnvironmentManifest, proposal: CoverageAssessmentProposal, reviewer_inputs: Mapping[str, str]) -> FindingLedger` returns a new ledger without mutating the old one; assigns new IDs, binds hashes and validates complete adjudication. New findings are allowed only with `revise`/`indeterminate`. `accept` cannot contain open/new findings. `revise` needs at least one open/new finding. `indeterminate` always holds.

## Task 1: Shared structured findings and versioned review contracts

**Files:** Create shared `environment/findings.py`; modify `environment/models.py`, `environment/__init__.py`; create `tests/unit/test_353_review_findings_contract.py`.

**Interfaces:** Produces all shared types/validator above. Extend `CoverageAssessmentProposal` with required `findings` and `decisions` lists plus `capability_unavailable: bool`. A true flag requires at least one open/new capability finding with independently read citations and a non-accept verdict; it permits a typed held outcome, never acceptance. New findings use their proposal list order until the daemon allocates IDs, so the model need not invent IDs. Extend discovery's envelope with `finding_responses`, keeping them outside the executable manifest.

- [ ] Add amendment task IDs to `tasks.md` and a new native execution ledger. Read both approved artifacts; run the repository's `.claude/commands/speckit.analyze.md` analysis over spec/plan/tasks before implementation and record findings. Resolve blocking inconsistencies without altering the approved scope.
- [ ] Write `test_review_ledger_requires_current_complete_closure`: assert a valid fully resolved ledger passes; removing a decision or changing candidate/source fails; reordering canonical object keys preserves digest while editing history changes it. Add bound tests at 32/33 findings, 2048/2049 UTF-8 bytes and 131072/131073 serialized bytes, including multibyte strings.
- [ ] Write `test_finding_identity_and_duplicate_history_cannot_be_forged`: reject overwritten original records, unknown/duplicate IDs, duplicate cycles, malformed paths/hashes and credential literals. Test roundtrip and a valid duplicate linked to a surviving finding, which must itself resolve before acceptance.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_review_findings_contract.py` and observe missing-contract failures. Implement the strict shared models/validator with existing path/credential utilities where cycle-safe; otherwise move only the minimal common validator into the focused shared module.
- [ ] Extend `CoverageAssessment` parsing to support historical version 1/policy `351-v1` and current version 2/policy `353-findings-v1`; current form requires `finding_ledger` and `finding_ledger_digest`. Enforce matching canonical digest and full current closure for accepted v2. Legacy forms cannot carry invented v2 fields. Shared manifest remains version 1/policy `351-v1`.
- [ ] Run the new contract tests and `tests/unit/test_351_environment_contract.py`; keep historical parse positives and add current v2 positives. Commit `feat(env): add versioned coverage finding contracts`.

## Task 2: Durable ledger ownership, checkpointing and migration

**Files:** Modify `environment_attempt.py`, `environment_repair.py`; create `tests/unit/test_353_review_findings_journal.py`; extend `test_353_category_budgets.py` and `test_353_execution_repair.py` where relevant.

**Interfaces:** Add `finding_ledger: FindingLedger | None` to `AttemptBudget` and version-5 `RepairJournal`. `None` is permitted only before first ledger initialization, never as an accepted review. Existing `begin_attempt`, `_budget`, `save_attempt`, `restart_environment_attempt` carry/reconcile ledger state without changing signatures.

- [ ] Write `test_restart_preserves_findings_expiry_and_consumed_budgets`: save a two-review ledger, rehydrate, assert exact ledger digest, original expiry and all consumed counters. Repeat through executable generation repair. Fail closed on source mismatch, malformed history, reduced limits and checkpoint error.
- [ ] Write `test_legacy_attempt_requires_explicit_cleanup_restart`: versions 1 through 4 never resume as empty-ledger v5; explicit reset preserves historical evidence and requires existing ownership cleanup. Historical ready records remain inspectable without being admitted as current. Unknown version, existing source owner and cleanup uncertainty stay held.
- [ ] Run new journal tests RED. Implement version 5 parsing/publishing and separate bounded legacy parsing for diagnosis/reset; do not normalize legacy active state into a current-policy journal. Copy ledger state by value, bind commit, preserve monotonic ID/history and enforce the existing journal/archive ceilings.
- [ ] Persist ledger decisions and exhausted-budget diagnostics before returning a hold. Before a revised candidate/model/source request, existing `budget.flush()` must save the current ledger plus already reserved counters. Test crash/checkpoint refusal yields zero subsequent requests.
- [ ] Run journal/category/executable-repair tests GREEN. Commit `feat(env): persist coverage findings across attempt restarts`.

## Task 3: Discovery responses and fresh independent adjudication

**Files:** Create daemon `environment_findings.py`; modify `environment_discovery.py`, `environment_discovery_prompts.py`; create `tests/unit/test_353_review_findings_flow.py`; extend `test_353_review_evidence.py`, `test_353_review_citations.py`, `test_353_discovery_repair.py` fixtures for explicit empty/new finding envelopes.

**Interfaces:** Produces daemon helpers above. `discover_and_review(...)` retains its public manifest/assessment return pair. `_validated_assessment` binds v2 evidence and ledger. `_prepare_review_evidence` includes the union of manifest and retained finding/response citation paths, fetched independently at the pinned commit; reviewer inputs include all hashes.

- [ ] Write `test_elm_objection_survives_nonblocking_review`: scripted replies open Docker/ShellCheck/promtool findings; revision fixes two; next review says “non-blocking” but omits the Docker ID. Assert no execution/acceptance and existing schema counter consumed. A corrected review must explicitly keep it open or resolve it with independently read evidence. Preserve two coverage revisions and the original deadline.
- [ ] Write `test_false_positive_closes_only_with_cited_independent_decision`, `test_changed_candidate_rechecks_closed_findings` and `test_unavailable_required_service_holds_without_privilege_changes`. Assert source-backed closure succeeds, required-ID/scope weakening still fails and unsupported required capability returns `required_capability_unavailable` with no executor calls. Test that the unavailable-capability flag cannot accompany acceptance or a review without an open/new cited capability finding.
- [ ] Write input/adversarial tests: unknown/missing/duplicate IDs, response citation not read, decisions citing invented paths, model-supplied provenance, finding/context overflow and source drift. Validate that malformed output consumes schema allowance, valid revise consumes coverage allowance, indeterminate/identity drift remain holds, and no free retry category appears.
- [ ] Run flow tests RED. Implement response envelope validation and pure transitions. Keep response prose and historical feedback explicitly untrusted; do not replay discovery assistant turns into review. Assign new IDs only after proposal/evidence validation, then checkpoint before the next action. A failed validation must not partially append records.
- [ ] Update reviewer prompt to independently reassess every retained finding, including closed ones, against the current candidate and full source evidence. Explain fixed project scope, generic capabilities and service usability; do not hard-code Docker/Ruby or any ecosystem in core prompts. Source-cited out-of-scope/false-positive resolution cannot weaken existing obligations.
- [ ] Return structured ledger/responses in revision feedback. Emit v2 accepted assessment only after complete closure; preserve fixed safe accepted-rationale summary. Exercise first candidate with zero findings and accepted review with empty ledger. Run new flow/evidence/citation regressions GREEN; commit `feat(env): require explicit resolution of coverage findings`.

## Task 4: Enforce current review evidence at every consumer boundary

**Files:** Modify shared `environment/models.py`, daemon `environment_acceptance.py`, `env_cache.py`, performer `environment_contract.py`, verification/runtime callers as required, and `scripts/environment_validation_image.py`; extend `test_351_environment_acceptance_matrix.py`, `test_353_authoritative_provenance.py`, performer `test_351_environment_contract.py` and root `workflows/test_env_bootstrap.py` fixture builders.

**Interfaces:** Add shared `validate_current_coverage(manifest: EnvironmentManifest, assessment: CoverageAssessment) -> None` in `environment/models.py`, using Task 1 validator. It requires schema 2/current policy, existing source/inventory/capabilities/input bindings, canonical ledger digest and complete current closure. Every readiness/installer admission uses this helper rather than duplicating policy logic.

- [ ] Write `test_old_ready_contract_cannot_bypass_findings_policy` and `test_performer_rejects_tampered_or_open_findings_before_commands`: cover old v1 accepted proof, changed ledger, wrong candidate/source/capabilities, omitted findings and valid v2 metadata. Keep actual pinned checkout binding in fixtures; no guard mocks.
- [ ] Write `test_accepted_findings_never_waive_failed_or_unrun_checks`: closed independent review followed by nonzero required check, missing result or failed service lifecycle stays unready. Verify saved-cache/Kubernetes and dispatch admission use the same current policy; do not change image/source/runtime identity comparisons.
- [ ] Run affected tests RED; implement the shared gate in daemon/performer and ensure installer-to-clean-consumer metadata preserves the exact accepted assessment/ledger digest and manifest digest. Include the new shared module in candidate-image code identity verification and any explicit trusted module allowlists. No source bodies or secrets enter operator state.
- [ ] Migrate synthetic accepted contract factories to honest v2 empty/resolved ledger proofs. Preserve explicit legacy rejection tests and all tampering/cancellation/timeout assertions. Run performer and root pytest suites in separate processes because both use a `tests` package.
- [ ] Run focused daemon acceptance/source/cache/dispatch tests, performer contract tests and root workflow tests GREEN. Commit `fix(env): require current finding evidence before environment reuse`.

## Task 5: Diagnostics, final verification and Elm simulation

**Files:** Modify `environment_attempt.py` summary, `scripts/validate_environment_repositories.py`, `docs/onboarding/06-env-cache.md`, `specs/353-practical-env-discovery/validation.md` and `tasks.md`; extend `test_353_validation_harness.py` and new finding tests.

**Interfaces:** Existing result/summary dictionaries gain safe finding count/open-ID/category fields. Fixed codes: `review_findings_unresolved`, `review_findings_invalid`, `review_findings_limit`, `required_capability_unavailable`, `legacy_review_requires_refresh`; retain existing schema/coverage exhaustion, context and source drift codes where authoritative. The private evidence artifacts retain redacted full ledger/history separately.

- [ ] Write `test_finding_diagnostics_do_not_expose_model_rationale_or_credentials`: assert summaries contain IDs/counts/fixed codes only; private evidence remains bounded/redacted. Test held review produces no stale installer/clean success and cleanup reporting remains truthful.
- [ ] Run harness/diagnostic tests RED, implement diagnostics and document cache invalidation/explicit restart without deleting unrelated caches. Rerun GREEN, then commit documentation with actual implementation results, never anticipated Elm success.
- [ ] Run affected 351/353, source/transport, journal/review/acceptance suites, lint, strict typing and unchanged debt ratchets. Run ship-issue preflight and quality guard for each pushed head. Preserve all existing justified annotations and retry/rebase budgets; broad suite runs through CI. Push PR #354 and use T3 CI watch, never polling scripts.
- [ ] Request one fresh whole-diff adversarial review through the existing native execution review workflow. Execute actionable reproductions, fix valid findings and rerun affected checks. Record exact reviewed head and resulting corrections; do not merge on old-head CI.
- [ ] Create a new Elm-only private profile using pinned commit `bc224a78db6f669d56863599d653f14e7605de0b`, `onprem/glm-5.3-flash`, low reasoning, 32768 maximum output tokens and 3600-second experimental phase/aggregate/provider/installer limits. Preserve model/settings, prior profiles and production defaults; bind candidate image to the pushed implementation.
- [ ] Inspect each exact probe and accepted-manifest command scope, source/input hashes, image/platform and ledger identity before writing a digest-bound approval. Use a one-shot artifact notifier without automatic approval. Never edit a proposed manifest or renew exhausted limits.
- [ ] On completion, record real review/install/every-required-clean-check outcomes, matching identities, timings and findings history. Confirm pinned snapshot unchanged and all owned source directories, containers, images/registry and volumes absent. A held run remains unvalidated even when the limitation is accurately explained. Other repositories stay paused until actual Elm success; PR #354 stays open until sample/Elm, final CI and review gates pass.

## Self-review and handoff

Task 1 covers strict finding identity/bounds/versioned evidence; Task 2 persistence/migration/budgets; Task 3 fresh judgments, source evidence and fixed capability scope; Task 4 producer/consumer policy parity and non-waivable execution; Task 5 safe diagnostics, quality/review gates and real Elm validation. Each review-focus condition has a named test in its owning task. No architecture change, runtime code or new dependencies are included in this planning commit. Written-plan approval is the remaining gate; native execution remains selected.
