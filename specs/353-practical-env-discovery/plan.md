> Maintainer-approved source conflict correction: [source-conflict-amendment.md](source-conflict-amendment.md). Inline TDD plan: [source-conflict-plan.md](source-conflict-plan.md).

> Amendment: [repair-amendment.md](repair-amendment.md) supersedes proposal provenance ownership and extends the shared repair budget through executable failures. Implementation: [repair-plan.md](repair-plan.md). User explicitly authorized implementation and retesting.

# Practical Reviewed Environment Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the observed discovery and transport failures, then demonstrate real environment installation and clean verification on the user's repositories.

**Architecture:** Retain the existing manifest, independent review and deterministic runner. Add shared source-payload and execution-capability contracts; supply bounded evidence tools and one attempt budget to discovery/review. Extend runtime adapters without changing readiness semantics.

**Tech Stack:** Existing Python 3.12+, pydantic v2, asyncio, httpx, Kubernetes client, pytest; standard library hashing/serialization; no new dependencies.

**Spec:** [spec.md](spec.md), approved by the user on 2026-10-17. Issue #353.

## Global Constraints

- Project Ecosystem Neutrality: no language/framework catalogs or core install recipes.
- Fresh independent coverage approval and deterministic successful execution of every required check in a clean consumer are mandatory.
- Installer and consumer use the same accepted manifest digest, immutable image and platform identity.
- Default listing pages contain at most 200 entries and 32 KiB; batches contain at most 8 files.
- Default discovery raw text allowance: 4 MiB; existing per-file bound: 256 KiB.
- Source document maximum: 16 MiB serialized UTF-8 JSON; decoded chunks: at most 64 KiB; at most 256 chunks.
- Each Kubernetes API object remains below the existing 900 KiB safety bound.
- At most two repair rounds across malformed proposals, protocol violations and coverage revision requests per attempt.
- Default aggregate deadline: 600 seconds, configurable up to 3600 seconds; existing default per-phase timeout: 120 seconds.
- Default aggregate model-call limit: 128; aggregate file-read limit: 512; phase transitions never reset aggregate counters.
- Missing, failed, unknown, skipped or unrun required evidence holds; credentials stay named runtime references.
- Preserve original checkout changes, private baseline evidence and separate issue #349 work.
- Focused TDD locally; full regression suite through CI, per user direction.

## Review Focus

1. A timed-out/cancelled API create may still create resources: discover and remove only owned attempt resources before accepting verification. Task 4.
2. Candidate edits may drop an obligation to silence validation: retain repair history and require independent evidence-based coverage review of every revised manifest. Task 5.
3. Unicode/path/JSON expansion may exceed bounds without exceeding raw reads: share canonical byte admission and reject before approval. Tasks 1 and 3.
4. Current runtime capabilities may change while old saved evidence remains syntactically valid: compare current capability digest at dispatch and cache reuse. Task 2.
5. Model latency, adapter retries and fresh-review restarts can reset deadlines accidentally: enforce cancellation and aggregate budgets around the complete attempt including inventory acquisition. Tasks 5 and 6.

## Files and responsibilities

- New shared `packages/service_inference/src/coordinare_service_inference/environment/source_payload.py`: canonical bounded source document, chunk metadata, reconstruction and safe materialization.
- Shared `environment/models.py`: versioned capability model and optional migration fields in existing persisted contracts; explicit schema descriptions.
- New `src/coordinare/services/environment_capabilities.py`: resolve actual runtime identities and policies; no inferred ecosystem behavior.
- New `src/coordinare/services/environment_inspection.py`: generic inventory navigation, exact read ledger and phase-local tools.
- New `src/coordinare/services/environment_attempt.py`: aggregate counters, deadline and trusted diagnostics.
- Existing `environment_discovery.py`: orchestration, prompts, full candidate validation, bounded repair and independent review.
- Existing `environment_verification.py`, `env_cache.py`, `kubernetes_verifier.py`, `kubernetes_runtime.py`, `kubernetes_provenance.py`: integrate capabilities, shared source transport and resource lifecycle.
- Existing acceptance/dispatch/performer contract consumers: reject stale capabilities; retain all existing execution/provenance validation.
- `tests/unit/test_353_*.py`: focused regressions; update affected #351 tests to the new protocol without dropping their original safety assertions.
- `scripts/validate_environment_repositories.py`: explicit opt-in, reproducible private live validation; never a default CI task.

## Task 1: Shared bounded source payload (FR-004, FR-005)

**Files:** Create shared `environment/source_payload.py` and `tests/unit/test_353_source_payload.py`; modify `environment_verification.py` and `kubernetes_verifier.py` admission functions.

**Interfaces:**
- `SourcePayload`: frozen dataclass with `sha256: str`, `size_bytes: int`, `chunks: tuple[bytes, ...]`.
- `encode_sources(files: Mapping[str, str]) -> SourcePayload`.
- `decode_sources(payload: SourcePayload, *, expected_inputs: Mapping[str, str]) -> dict[str, str]`.
- `materialize_sources(files: Mapping[str, str], destination: Path) -> None` writes only into a newly created private directory, refusing preexisting entries/symlinks.
- Canonical encoding: sorted keys, UTF-8, `ensure_ascii=False`, compact separators, no duplicate JSON keys. Chunk count is derived from actual document size and checked against metadata during reconstruction.

- [x] **Step 1: Write regressions.** `test_large_unicode_bundle_roundtrip` uses multiple individually admitted files totaling above 315,664 serialized bytes and asserts exact byte/hash equality. `test_document_bounds` accepts exactly 16 MiB and rejects one byte over; `test_escaped_text_bound` tests raw/serialized divergence. `test_corrupt_missing_duplicate_extra_chunk` rejects each mutation. `test_source_paths_and_existing_symlink_refused` asserts no file outside destination changes. `test_exact_input_set_required` rejects extra/missing paths and mismatched hashes.
- [x] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_source_payload.py`; expect missing module/API failures before implementation.
- [x] **Step 3: Implement** the interfaces above. Keep individual file bounds and full document admission shared by discovery and both verifiers; preserve the `check_project_files(files)` compatibility function as a delegating wrapper.
- [x] **Step 4: Verify green.** Run the task test file plus affected source admission tests; all targeted cases pass with no weakened path/hash assertions.
- [x] **Step 5: Commit** only task files with `feat: share bounded verification source payloads`.

## Task 2: Actual runtime capabilities and identity binding (FR-002, FR-006, FR-008)

**Files:** Modify shared `environment/models.py`, `src/coordinare/config.py`, `models/env_cache.py`, `services/env_cache.py`, `services/environment_acceptance.py`, `services/env_provenance.py`, `services/kubernetes_provenance.py`, `graph/nodes/dispatch_performer.py`, `agent/performer/src/performer/environment_contract.py`; create `services/environment_capabilities.py`, `tests/unit/test_353_capabilities.py`.

**Interfaces:**
- `ExecutionCapabilities(StrictModel)`: `schema_version: Literal[1]`, `image_ref: str`, `platform: str`, `cache_mount_path: str`, `install_network: Literal['enabled','disabled']`, `verify_network: Literal['disabled']`; constant disposable-repo/writable-scratch/argv semantics in schema descriptions. `digest() -> str` hashes canonical JSON.
- `resolve_execution_capabilities(state: Mapping[str, Any], symphony_name: str) -> ExecutionCapabilities` is async and uses actual runtime image-resolution facilities. Missing immutable image/platform or unsupported policies raise a trusted configuration error.
- Add `capabilities_digest: str | None = None` to manifest and assessment models. New discovery requires matching non-null values; legacy persisted data can parse but cannot pass acceptance. Execution reports remain bound through the resulting manifest digest.
- Callers explicitly supply `capabilities: ExecutionCapabilities` to discovery; cache reuse and dispatch compare its digest against current resolved capabilities before relying on saved execution.

- [x] **Step 1: Write regressions.** `test_prompt_capabilities_match_actual_runtime` covers Docker and Kubernetes identities, roots and network policies. `test_capability_change_holds_saved_cache` changes each field independently. `test_legacy_missing_capability_binding_requires_rediscovery` parses legacy state but refuses readiness. `test_performer_rejects_capability_digest_mismatch` retains manifest/input/image validation. `test_schema_explains_check_id_references` checks descriptions for requirement/check/service dependencies and shell/root semantics.
- [x] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_capabilities.py`; expect absent fields/resolver and old acceptance behavior failures.
- [x] **Step 3: Implement** the exact contracts above and wire actual values at both bootstrap and dispatch. Keep additional digest fields covered by canonical manifest serialization and existing frozen provenance. Update fixtures to supply explicit capabilities; retain negative legacy tests.
- [x] **Step 4: Verify green.** Run the task file and existing #351 contract, acceptance, dispatch, consumer-image and performer-capability tests. Required-failure and all-scope acceptance guards continue passing.
- [x] **Step 5: Commit** with `feat: bind environment review to execution capabilities`.

## Task 3: Paginated inspection without inventory flooding (FR-001, FR-004)

**Files:** Create `services/environment_inspection.py`, `tests/unit/test_353_inspection.py`; modify `environment_discovery.py`, its #351 tests and discovery fixtures.

**Interfaces:**
- `SourceInspection(paths: Sequence[str], fetch: Fetch, limits: DiscoveryLimits)` owns pinned inventory, directory index, read ledger and source bytes. Task 5 extends this with aggregate accounting; this task retains existing per-phase limits.
- `list_directory(path: str = '', cursor: int = 0) -> dict[str, Any]`: immediate children only, sorted directories/files, bounded entries and serialized bytes, opaque-in-prompt integer continuation. Reject traversal, unknown directories, invalid offsets, and an entry too large for a page.
- `read_files(paths: Sequence[str]) -> dict[str, Any]`: 1-8 distinct valid paths, exact content/hashes, deterministic request ordering. Fetch concurrently within the batch; reserve phase read/byte admission before accepting results. Any failed read holds the phase, with no partially accepted candidate.
- `inventory_digest: str` hashes the complete sorted inventory. Add nullable `inventory_digest` fields to manifest and assessment as in Task 2; new acceptance requires matching non-null values and live inventory checks invalidate additions/deletions.
- Tool envelopes: `{'tool':'list_files','path':'','cursor':0}`, `{'tool':'read_files','paths':[...]}`. Keep `read_file` as a bounded single-read compatibility alias; legacy `list_files` means root page, never full inventory.

- [x] **Step 1: Write regressions.** `test_100000_paths_prompt_bounded_and_all_pages_reachable` asserts root preview <=32 KiB/200 entries and no full inventory in initial messages. `test_nested_unknown_toolchain_discovery` follows synthetic arbitrary names. `test_batch_limit_and_duplicate_reads_count` enforces 8-file batches and counts repeated reads. `test_added_deleted_paths_invalidate_inventory` changes unread paths. `test_batch_snapshot_drift_holds` fails changed contents. `test_long_multibyte_names_respect_page_bytes` checks boundary behavior.
- [x] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_inspection.py`; expect old flooding/protocol behavior failures.
- [x] **Step 3: Implement** indexed navigation and ledgers above. Bind the full inventory identity without presenting every path to the model. Never prioritize files by a language/filename catalog. Reviewer tools use independent ledgers over the same snapshot.
- [x] **Step 4: Verify green.** Run the task file, #351 discovery/GitHub inventory tests and capability tests; verify reviewer extra reads still bind to assessment and all discovery reads bind to manifest.
- [x] **Step 5: Commit** with `feat: bound repository evidence navigation and batch reads`.

## Task 4: Source transport and resource lifecycle (FR-005, FR-006, FR-008)

**Files:** Modify `services/environment_verification.py`, `services/env_cache.py`, `services/kubernetes_verifier.py`, `services/kubernetes_runtime.py`, `services/kubernetes_provenance.py`, `deploy/helm/coordinare/templates/rbac.yaml` only if existing Secret permissions prove insufficient; create `tests/unit/test_353_source_transport.py`, `tests/unit/test_353_payload_lifecycle.py`; update #351 source-transfer tests and affected Kubernetes tests.

**Interfaces:**
- Use `source_setup(files: dict[str, str], *, expected_inputs: Mapping[str, str]) -> str`; update all callers and fixtures to provide actual expected hashes; feed the encoded payload through existing Docker `bash -s` stdin, not argv/environment.
- `build_verifier_source_secrets(spec: VerifierSpec, *, name: str, namespace: str, owner: str, pod_uid: str) -> list[dict[str, Any]]` builds immutable, opaque-name, owner-bound source Secrets with ordered chunks and trusted metadata. Each Secret contains up to 8 chunks and must satisfy the API-object bound after base64/JSON serialization.
- Add `source_inputs: Mapping[str, str]` to `VerifierSpec`; real verification requires exact equality with accepted inputs. Walk-only mode has no source payload.
- Pod init container alone mounts source Secrets, validates metadata/digest/hash set using the shared package, and writes the reconstructed repo into private scratch. The verification container mounts reconstructed scratch without Secret volumes. Keep existing credential transfer separate.
- Extend `_VerifierAttempt` resource accounting to all source Secrets; use Pod owner references. Cleanup confirms Pod removal then Secret absence; cancellation shields cleanup. Restart orphan reconciliation uses ownership labels and current Pod ownership, never deletes another attempt's resources.

- [x] **Step 1: Write regressions.** `test_docker_large_payload_uses_stdin` executes trusted setup on >315,664 bytes and verifies no oversized argv/env. `test_kubernetes_source_payload_not_in_pod_env` runs init setup locally and verifies identical reconstruction. `test_chunk_secret_and_pod_size_bounds` checks exact serialized API objects. `test_partial_create_cancel_restart_cleanup` covers late API completion, 409 ownership conflicts, partial Secret creation, cancellation and restart orphan cleanup. `test_failed_cleanup_never_reports_success` holds even after passing checks.
- [x] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_source_transport.py tests/unit/test_353_payload_lifecycle.py`; expect old 256 KiB and missing resource lifecycle failures.
- [x] **Step 3: Implement** Task 1 payload use and resource APIs above. Retain cache read-only, clean network isolation, current immutable image validation, credential redaction, and independent full-scope execution admission. Adjust RBAC only for exercised missing operations and justify any policy diff.
- [x] **Step 4: Verify green.** Run task files plus #351 source/credential transfer, verification environment and Kubernetes credential tests, and #276 verifier followups. Exercise trusted setup as subprocesses and fake Kubernetes API lifecycle, not just manifest shape.
- [x] **Step 5: Commit** with `feat: transfer verified source payloads outside pod environment`.

## Task 5: One budget, sanitized feedback and fresh review (FR-003, FR-006, FR-007)

**Files:** Create `services/environment_attempt.py`, `tests/unit/test_353_discovery_repair.py`; modify `environment_discovery.py`, `environment_inspection.py`, `services/env_cache.py`, `config.py`, `services/conducting.py` and discovery tests.

**Interfaces:**
- Move `EnvironmentDiscoveryError` to `environment_attempt.py` and re-export it from `environment_discovery.py` to avoid circular imports.
- `AttemptBudget`: monotonic deadline, counters and `reserve_call() -> None`, `reserve_reads(count: int) -> None`, `reserve_repair() -> None`; exhaustion raises `EnvironmentDiscoveryError` with a fixed code. Defaults: 600s, 128 calls, 512 reads, 2 repairs.
- Extend `SourceInspection` with `budget: AttemptBudget | None = None`; production callers always pass the attempt budget, and every batch reserves aggregate reads before issuing fetches. Repair within a phase never resets that phase deadline.
- Add env-cache config `manifest_attempt_timeout_seconds` (600, >0 and <=3600), `manifest_max_model_calls` (128), `manifest_max_total_reads` (512), `manifest_max_repairs` (2). Aggregate budgets encompass inventory acquisition; apply the smaller configured outer bootstrap budget when positive.
- `discover_and_review(..., capabilities: ExecutionCapabilities, ...)` keeps the tuple return type. Candidate validation executes within the discovery conversation; repair returns fixed validation codes and schema field locations, with a strict allowlist excluding input values/exception strings.
- `DiscoveryChatReply`: frozen dataclass `text: str`, `usage: Mapping[str, int] | None`, `finish_reason: str | None`. Discovery accepts existing string callbacks or this result. Add `OpenAiApiBackend.chat_json_with_metadata(messages) -> DiscoveryChatReply` without changing existing `chat_json` callers; share response parsing, no parallel mutable last-response property.
- Persist only trusted attempt summaries. Raw model rationale/objections remain ephemeral untrusted evidence in repair; bound and redact them before any feedback. `revise` consumes one shared repair; existing `indeterminate` remains terminal. Do not add a new verdict to existing schemas.

- [x] **Step 1: Write regressions.** `test_invalid_refs_then_valid_revision_fresh_review` asserts trusted field/code feedback, all original read hashes and distinct review history. `test_install_only_dependency_corrected_not_waived` keeps obligation and requires executable clean evidence. `test_third_repair_held` shares budget across protocol/schema/review revision. `test_review_cannot_remove_obligation_without_evidence` supplies an omission and independent rejection. `test_identity_or_source_drift_never_repaired` stops immediately. `test_secret_validation_values_not_logged` injects source-bearing errors and checks diagnostics. `test_aggregate_deadline_cancels_inventory_and_backend_retries` uses controlled clock/events. `test_provider_metadata_optional_and_request_local` covers concurrent calls and absent metadata.
- [x] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_discovery_repair.py`; expect current immediate validation exits and absent aggregate accounting.
- [x] **Step 3: Implement** the budget, feedback and chat contracts above. Every revision undergoes full schema/semantic/source admission before a new independent review; review must re-read all manifest inputs. Do not reuse an accepted assessment after any candidate change. Include actual capabilities and root/argv/check-reference guidance in both phase prompts.
- [x] **Step 4: Verify green.** Run task file and all #351 discovery/acceptance/contract tests, plus existing conducting tests. Ensure failed, not-run, timed-out, unknown and omitted results remain holds after successful repair/review.
- [x] **Step 5: Commit** with `feat: repair environment proposals within a shared attempt budget`.

## Task 6: Integration, documentation, analysis and CI (FR-001 through FR-008; SC-001, SC-002, SC-005)

**Files:** Create `tests/unit/test_353_bootstrap_pipeline.py`; modify `docs/operators/reviewed-environment-manifest.md`, `docs/onboarding/06-env-cache.md`, relevant example configurations; derive `specs/353-practical-env-discovery/tasks.md` from this plan before implementation begins.

**Interfaces:** Consume Tasks 1-5 contracts through real `EnvCacheService._build_manifest_artifacts`, dispatch, installer runner, clean verification and provenance freeze. No alternate test-only readiness path.

- [x] **Step 1: Before implementation**, generate complete `tasks.md` from this plan and run the read-only `.claude/commands/speckit.analyze.md` workflow against spec/plan/tasks and constitution. Resolve critical/high inconsistencies within approved scope and record analysis; no product code until that gate passes.
- [x] **Step 2: Write integration regressions.** `test_daemon_bootstrap_repair_clean_verify_freeze` drives service wiring with deterministic model replies, actual shared runner and fake transport/API only at external boundaries. `test_outer_retry_does_not_accept_partial_attempt` asserts attempts cannot accumulate partial approvals or stale digests. `test_required_failure_holds_both_transports` covers fail, timeout, missing and not-run outcomes. `test_capability_or_inventory_change_restarts_bootstrap` exercises dispatch state transitions, not helper-only comparisons.
- [x] **Step 3: Run red.** `.venv/bin/pytest -q tests/unit/test_353_bootstrap_pipeline.py`; expect incorrect wiring cases to fail before finishing integration.
- [x] **Step 4: Complete integration** and update operator/config documentation with exact limits, held codes, old-cache invalidation and Secret cleanup. Keep compatibility adapters small; do not bypass evidence validation in old test fixtures.
- [x] **Step 5: Verify and commit.** Run task file and all focused #353 tests, lint/type checks appropriate to modified code, then commit `test: cover reviewed bootstrap pipeline and document limits`. Honor the user's full-suite-on-CI instruction: do not run the broad local suite; run applicable focused preflight rows and record the user override for broad rows without falsifying preflight success.
- [ ] **Step 6: Open PR** targeting main with `Closes #353`, link it with T3, run quality-guard, and use CI for the full suite. Read ci-safety/watch-ci before acting on failures; use T3 watching rather than a polling loop. Obtain adversarial full-diff review and Copilot review, verify claims, fix valid findings, rerun affected checks. Do not merge before Task 7 results are recorded.

## Task 7: Repeat live pinned-repository validation (SC-003, SC-004, SC-006)

**Files:** Create `scripts/validate_environment_repositories.py`, `tests/unit/test_353_validation_harness.py`; save private reports under ignored `.agents/state/353-org-validation/`; publish only a sanitized results summary in the PR.

**Interfaces:** CLI accepts explicit repository/commit selections, model configuration by references, private output directory and per-attempt budgets. Calls production discovery/review, actual installer, clean verifier and freeze; captures each stage distinctly. No source snapshots, raw transcripts or secrets enter tracked artifacts.

- [x] **Step 1: Write harness regressions.** `test_hold_never_launches_installer`, `test_missing_clean_evidence_never_ready`, `test_scope_refuses_deployment_command`, `test_profiles_and_snapshots_do_not_overwrite_baseline`, and `test_stage_results_do_not_claim_daemon_coverage` assert those exact behaviors.
- [x] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_validation_harness.py`; expect missing CLI/wiring failures.
- [x] **Step 3: Implement** the harness by extracting the previous private validation approach into an explicit opt-in script. Require command-scope inspection before project execution; ambiguous destructive operations hold. Share no model credentials with project commands. Prepare immutable test images containing the candidate runner, and record platform, image, source and accepted manifest identities.
- [ ] **Step 4: Verify and commit.** Run harness tests, commit `test: add opt-in repository bootstrap validation`, and rerun relevant CI for the resulting head.
- [ ] **Step 5: Run small sample**, then Elm at baseline pinned commits with the baseline GLM profile/settings. Record defaults first; any changed model/deadline profile is separate. Require real fresh review and installation/clean-consumer results. A hold requires investigation and honest reporting, never a handcrafted manifest to manufacture success.
- [ ] **Step 6: Run the remaining baseline eligible repositories** at their pinned commits, excluding the same four Rails-primary and three archived repos. Test all 29 baseline targets exactly once per named profile; preserve reruns separately. Record external prerequisite holds rather than deploying services outside validation scope.
- [ ] **Step 7: Reconcile results and cleanup.** Verify source snapshots unchanged, resource deletion confirmed and no private payloads in reports. Distinguish direct-entrypoint live results from Task 6 daemon integration tests; disclose no full live daemon test if none ran. Small sample and Elm must demonstrate passing required checks before claiming bootstrap fixed. If either fails, diagnose remaining in-scope defects and repeat affected stages within recorded budgets.
- [ ] **Step 8: Ship through gates.** Attach sanitized evidence, complete review/CI on final head, merge via merge-pr, verify closure, and unwatch PR before final handoff. If validation still holds, report the exact blockers and leave the PR unmerged rather than claiming success.

## Self-review and execution handoff

Coverage: FR-001 -> Task 3; FR-002 -> Tasks 2/5; FR-003 -> Task 5;
FR-004 -> Tasks 1/3; FR-005 -> Tasks 1/4; FR-006 -> Tasks 2/4/5/6;
FR-007 -> Task 5; FR-008 -> Tasks 2/4/6. SC-001/002 -> Tasks 1-6;
SC-003/004/006 -> Task 7; SC-005 -> Task 6. Review Focus cases have explicit tests.

Recommended execution: native implementation in this session, followed by the required
fresh adversarial full-diff review and Copilot gate. The capability, inventory and
acceptance interfaces are coupled; sequential ownership avoids competing edits while
keeping independent review before merge. No implementation begins until plan review.
