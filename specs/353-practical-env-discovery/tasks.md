# Tasks: Practical reviewed environment discovery

Source: approved [plan.md](plan.md); all tasks run sequentially in the current isolated worktree.
Task 6 Step 1 is the pre-implementation analysis gate and is performed before Task 1.
Each implementation task follows red, green, focused verification and commit; full suite runs on CI.

## Foundation for US3 - Phase 1: Shared bounded source payload (FR-004, FR-005)

- [x] T001 [US3] **Step 1: Write regressions.** `test_large_unicode_bundle_roundtrip` uses multiple individually admitted files totaling above 315,664 serialized bytes and asserts exact byte/hash equality. `test_document_bounds` accepts exactly 16 MiB and rejects one byte over; `test_escaped_text_bound` tests raw/serialized divergence. `test_corrupt_missing_duplicate_extra_chunk` rejects each mutation. `test_source_paths_and_existing_symlink_refused` asserts no file outside destination changes. `test_exact_input_set_required` rejects extra/missing paths and mismatched hashes. See `specs/353-practical-env-discovery/plan.md`, Task 1, for exact files and interfaces.
- [x] T002 [US3] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_source_payload.py`; expect missing module/API failures before implementation. See `specs/353-practical-env-discovery/plan.md`, Task 1, for exact files and interfaces.
- [x] T003 [US3] **Step 3: Implement** the interfaces above. Keep individual file bounds and full document admission shared by discovery and both verifiers; preserve the `check_project_files(files)` compatibility function as a delegating wrapper. See `specs/353-practical-env-discovery/plan.md`, Task 1, for exact files and interfaces.
- [x] T004 [US3] **Step 4: Verify green.** Run the task test file plus affected source admission tests; all targeted cases pass with no weakened path/hash assertions. See `specs/353-practical-env-discovery/plan.md`, Task 1, for exact files and interfaces.
- [x] T005 [US3] **Step 5: Commit** only task files with `feat: share bounded verification source payloads`. See `specs/353-practical-env-discovery/plan.md`, Task 1, for exact files and interfaces.

## Foundation for US2 - Phase 2: Actual runtime capabilities and identity binding (FR-002, FR-006, FR-008)

- [x] T006 [US2] **Step 1: Write regressions.** `test_prompt_capabilities_match_actual_runtime` covers Docker and Kubernetes identities, roots and network policies. `test_capability_change_holds_saved_cache` changes each field independently. `test_legacy_missing_capability_binding_requires_rediscovery` parses legacy state but refuses readiness. `test_performer_rejects_capability_digest_mismatch` retains manifest/input/image validation. `test_schema_explains_check_id_references` checks descriptions for requirement/check/service dependencies and shell/root semantics. See `specs/353-practical-env-discovery/plan.md`, Task 2, for exact files and interfaces.
- [x] T007 [US2] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_capabilities.py`; expect absent fields/resolver and old acceptance behavior failures. See `specs/353-practical-env-discovery/plan.md`, Task 2, for exact files and interfaces.
- [x] T008 [US2] **Step 3: Implement** the exact contracts above and wire actual values at both bootstrap and dispatch. Keep additional digest fields covered by canonical manifest serialization and existing frozen provenance. Update fixtures to supply explicit capabilities; retain negative legacy tests. See `specs/353-practical-env-discovery/plan.md`, Task 2, for exact files and interfaces.
- [x] T009 [US2] **Step 4: Verify green.** Run the task file and existing #351 contract, acceptance, dispatch, consumer-image and performer-capability tests. Required-failure and all-scope acceptance guards continue passing. See `specs/353-practical-env-discovery/plan.md`, Task 2, for exact files and interfaces.
- [x] T010 [US2] **Step 5: Commit** with `feat: bind environment review to execution capabilities`. See `specs/353-practical-env-discovery/plan.md`, Task 2, for exact files and interfaces.

## US1 - Phase 3: Paginated inspection without inventory flooding (FR-001, FR-004)

- [x] T011 [US1] **Step 1: Write regressions.** `test_100000_paths_prompt_bounded_and_all_pages_reachable` asserts root preview <=32 KiB/200 entries and no full inventory in initial messages. `test_nested_unknown_toolchain_discovery` follows synthetic arbitrary names. `test_batch_limit_and_duplicate_reads_count` enforces 8-file batches and counts repeated reads. `test_added_deleted_paths_invalidate_inventory` changes unread paths. `test_batch_snapshot_drift_holds` fails changed contents. `test_long_multibyte_names_respect_page_bytes` checks boundary behavior. See `specs/353-practical-env-discovery/plan.md`, Task 3, for exact files and interfaces.
- [x] T012 [US1] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_inspection.py`; expect old flooding/protocol behavior failures. See `specs/353-practical-env-discovery/plan.md`, Task 3, for exact files and interfaces.
- [x] T013 [US1] **Step 3: Implement** indexed navigation and ledgers above. Bind the full inventory identity without presenting every path to the model. Never prioritize files by a language/filename catalog. Reviewer tools use independent ledgers over the same snapshot. See `specs/353-practical-env-discovery/plan.md`, Task 3, for exact files and interfaces.
- [x] T014 [US1] **Step 4: Verify green.** Run the task file, #351 discovery/GitHub inventory tests and capability tests; verify reviewer extra reads still bind to assessment and all discovery reads bind to manifest. See `specs/353-practical-env-discovery/plan.md`, Task 3, for exact files and interfaces.
- [x] T015 [US1] **Step 5: Commit** with `feat: bound repository evidence navigation and batch reads`. See `specs/353-practical-env-discovery/plan.md`, Task 3, for exact files and interfaces.

## US3 - Phase 4: Source transport and resource lifecycle (FR-005, FR-006, FR-008)

- [x] T016 [US3] **Step 1: Write regressions.** `test_docker_large_payload_uses_stdin` executes trusted setup on >315,664 bytes and verifies no oversized argv/env. `test_kubernetes_source_payload_not_in_pod_env` runs init setup locally and verifies identical reconstruction. `test_chunk_secret_and_pod_size_bounds` checks exact serialized API objects. `test_partial_create_cancel_restart_cleanup` covers late API completion, 409 ownership conflicts, partial Secret creation, cancellation and restart orphan cleanup. `test_failed_cleanup_never_reports_success` holds even after passing checks. See `specs/353-practical-env-discovery/plan.md`, Task 4, for exact files and interfaces.
- [x] T017 [US3] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_source_transport.py tests/unit/test_353_payload_lifecycle.py`; expect old 256 KiB and missing resource lifecycle failures. See `specs/353-practical-env-discovery/plan.md`, Task 4, for exact files and interfaces.
- [x] T018 [US3] **Step 3: Implement** Task 1 payload use and resource APIs above. Retain cache read-only, clean network isolation, current immutable image validation, credential redaction, and independent full-scope execution admission. Adjust RBAC only for exercised missing operations and justify any policy diff. See `specs/353-practical-env-discovery/plan.md`, Task 4, for exact files and interfaces.
- [x] T019 [US3] **Step 4: Verify green.** Run task files plus #351 source/credential transfer, verification environment and Kubernetes credential tests, and #276 verifier followups. Exercise trusted setup as subprocesses and fake Kubernetes API lifecycle, not just manifest shape. See `specs/353-practical-env-discovery/plan.md`, Task 4, for exact files and interfaces.
- [x] T020 [US3] **Step 5: Commit** with `feat: transfer verified source payloads outside pod environment`. See `specs/353-practical-env-discovery/plan.md`, Task 4, for exact files and interfaces.

## US2 - Phase 5: One budget, sanitized feedback and fresh review (FR-003, FR-006, FR-007)

- [x] T021 [US2] **Step 1: Write regressions.** `test_invalid_refs_then_valid_revision_fresh_review` asserts trusted field/code feedback, all original read hashes and distinct review history. `test_install_only_dependency_corrected_not_waived` keeps obligation and requires executable clean evidence. `test_third_repair_held` shares budget across protocol/schema/review revision. `test_review_cannot_remove_obligation_without_evidence` supplies an omission and independent rejection. `test_identity_or_source_drift_never_repaired` stops immediately. `test_secret_validation_values_not_logged` injects source-bearing errors and checks diagnostics. `test_aggregate_deadline_cancels_inventory_and_backend_retries` uses controlled clock/events. `test_provider_metadata_optional_and_request_local` covers concurrent calls and absent metadata. See `specs/353-practical-env-discovery/plan.md`, Task 5, for exact files and interfaces.
- [x] T022 [US2] **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_discovery_repair.py`; expect current immediate validation exits and absent aggregate accounting. See `specs/353-practical-env-discovery/plan.md`, Task 5, for exact files and interfaces.
- [x] T023 [US2] **Step 3: Implement** the budget, feedback and chat contracts above. Every revision undergoes full schema/semantic/source admission before a new independent review; review must re-read all manifest inputs. Do not reuse an accepted assessment after any candidate change. Include actual capabilities and root/argv/check-reference guidance in both phase prompts. See `specs/353-practical-env-discovery/plan.md`, Task 5, for exact files and interfaces.
- [x] T024 [US2] **Step 4: Verify green.** Run task file and all #351 discovery/acceptance/contract tests, plus existing conducting tests. Ensure failed, not-run, timed-out, unknown and omitted results remain holds after successful repair/review. See `specs/353-practical-env-discovery/plan.md`, Task 5, for exact files and interfaces.
- [x] T025 [US2] **Step 5: Commit** with `feat: repair environment proposals within a shared attempt budget`. See `specs/353-practical-env-discovery/plan.md`, Task 5, for exact files and interfaces.

## Phase 6: Integration, documentation, analysis and CI (FR-001 through FR-008; SC-001, SC-002, SC-005)

- [x] T026 **Step 1: Before implementation**, generate complete `tasks.md` from this plan and run the read-only `.claude/commands/speckit.analyze.md` workflow against spec/plan/tasks and constitution. Resolve critical/high inconsistencies within approved scope and record analysis; no product code until that gate passes. See `specs/353-practical-env-discovery/plan.md`, Task 6, for exact files and interfaces.
- [x] T027 **Step 2: Write integration regressions.** `test_daemon_bootstrap_repair_clean_verify_freeze` drives service wiring with deterministic model replies, actual shared runner and fake transport/API only at external boundaries. `test_outer_retry_does_not_accept_partial_attempt` asserts attempts cannot accumulate partial approvals or stale digests. `test_required_failure_holds_both_transports` covers fail, timeout, missing and not-run outcomes. `test_capability_or_inventory_change_restarts_bootstrap` exercises dispatch state transitions, not helper-only comparisons. See `specs/353-practical-env-discovery/plan.md`, Task 6, for exact files and interfaces.
- [x] T028 **Step 3: Run red.** `.venv/bin/pytest -q tests/unit/test_353_bootstrap_pipeline.py`; expect incorrect wiring cases to fail before finishing integration. See `specs/353-practical-env-discovery/plan.md`, Task 6, for exact files and interfaces.
- [x] T029 **Step 4: Complete integration** and update operator/config documentation with exact limits, held codes, old-cache invalidation and Secret cleanup. Keep compatibility adapters small; do not bypass evidence validation in old test fixtures. See `specs/353-practical-env-discovery/plan.md`, Task 6, for exact files and interfaces.
- [x] T030 **Step 5: Verify and commit.** Run task file and all focused #353 tests, lint/type checks appropriate to modified code, then commit `test: cover reviewed bootstrap pipeline and document limits`. Honor the user's full-suite-on-CI instruction: do not run the broad local suite; run applicable focused preflight rows and record the user override for broad rows without falsifying preflight success. See `specs/353-practical-env-discovery/plan.md`, Task 6, for exact files and interfaces.
- [x] T031 **Step 6: Open PR** targeting main with `Closes #353`, link it with T3, run quality-guard, and use CI for the full suite. Read ci-safety/watch-ci before acting on failures; use T3 watching rather than a polling loop. Obtain adversarial full-diff review and Copilot review, verify claims, fix valid findings, rerun affected checks. Do not merge before Task 7 results are recorded. See `specs/353-practical-env-discovery/plan.md`, Task 6, for exact files and interfaces.

## Phase 7: Repeat live pinned-repository validation (SC-003, SC-004, SC-006)

- [x] T032 **Step 1: Write harness regressions.** `test_hold_never_launches_installer`, `test_missing_clean_evidence_never_ready`, `test_scope_refuses_deployment_command`, `test_profiles_and_snapshots_do_not_overwrite_baseline`, and `test_stage_results_do_not_claim_daemon_coverage` assert those exact behaviors. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [x] T033 **Step 2: Run red.** `.venv/bin/pytest -q tests/unit/test_353_validation_harness.py`; expect missing CLI/wiring failures. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [x] T034 **Step 3: Implement** the harness by extracting the previous private validation approach into an explicit opt-in script. Require command-scope inspection before project execution; ambiguous destructive operations hold. Share no model credentials with project commands. Prepare immutable test images containing the candidate runner, and record platform, image, source and accepted manifest identities. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [x] T035 **Step 4: Verify and commit.** Run harness tests, commit `test: add opt-in repository bootstrap validation`, and rerun relevant CI for the resulting head. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [x] T036 **Step 5: Run small sample**, then Elm at baseline pinned commits with the baseline GLM profile/settings. Record defaults first; any changed model/deadline profile is separate. Require real fresh review and installation/clean-consumer results. A hold requires investigation and honest reporting, never a handcrafted manifest to manufacture success. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [x] T037 **Step 6: Run the remaining baseline eligible repositories** at their pinned commits, excluding the same four Rails-primary and three archived repos. Test all 29 baseline targets exactly once per named profile; preserve reruns separately. Record external prerequisite holds rather than deploying services outside validation scope. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [x] T038 **Step 7: Reconcile results and cleanup.** Verify source snapshots unchanged, resource deletion confirmed and no private payloads in reports. Distinguish direct-entrypoint live results from Task 6 daemon integration tests; disclose no full live daemon test if none ran. Small sample and Elm must demonstrate passing required checks before claiming bootstrap fixed. If either fails, diagnose remaining in-scope defects and repeat affected stages within recorded budgets. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.
- [ ] T039 **Step 8: Ship through gates.** Attach sanitized evidence, complete review/CI on final head, merge via merge-pr, verify closure, and unwatch PR before final handoff. If validation still holds, report the exact blockers and leave the PR unmerged rather than claiming success. See `specs/353-practical-env-discovery/plan.md`, Task 7, for exact files and interfaces.

## Dependencies

Phases 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7. Phase 6 analysis step runs first.
No parallel implementation: shared manifest, runtime and acceptance interfaces require sequential ownership.
See each plan task for exact files, signatures, test names and requirement mappings.

## Approved executable repair amendment

- [x] T040 Declaration-only proposals, authoritative provenance and portable-root guidance; focused red/green coverage per repair-plan Task 1.
- [x] T041 Durable repair journal, finite shared budget, rejected evidence and fresh cache generations; focused red/green coverage per Task 2.
- [x] T042 Production daemon and live harness orchestration, restart/cleanup/transport integration; focused red/green coverage per Task 3.
- [x] T043 Documentation, independent review, focused preflight, CI and repeated pinned real-repository validation per Task 4; original live-success merge gate remains.

## Validation outcome

Implementation and the repeated validation attempt are recorded in [validation.md](validation.md).
T039 remains open: SC-004 is unmet, so PR #354 is unmerged and issue #353 stays open.
Copilot did not deliver a review; the shipping workflow fallback used independent adversarial review and author verification of its fixes. No Copilot approval or real bootstrap success is claimed.

## Approved complete-review-evidence amendment

- [x] T044 Independent schema/coverage/execution budgets and compatible configuration (review-evidence-plan Task 1).
- [x] T045 Version-two durable journal, no budget renewal, explicit legacy migration holds (Task 2).
- [x] T046 Deterministic fresh mandatory-input delivery and bounded review context (Task 3).
- [x] T047 Pipeline integration, safe diagnostics and complete Elm-sequence replay (Task 4).
- [x] T048 Independent full-diff review, CI and the same 29 pinned repositories, with separate Elm1800 profile (Task 5). T039 still requires actual clean-success gates.

The complete-evidence amendment retest also held for all 29 default repositories and the separately labeled Elm1800 experiment. T048 records completed implementation/review/CI/validation attempts; T039 and SC-004 remain unmet. See validation.md for exact revisions and outcomes.

## Executable preflight amendment (approved spec and plan)

- [x] T049 Complete mandatory repair evidence and context admission (FR-001).
- [x] T050 Versioned probe contracts, durable reservations and journal migration (FR-002/006).
- [x] T051 Disposable bounded probe executor and confirmed cleanup (FR-003).
- [x] T052 Discovery/review integration and required install assertions (FR-004/005).
- [x] T053 Separate probe scope approval and accurate diagnostics (FR-007).
- [ ] T054 Independent review, CI and Elm-first 3600-second live validation (acceptance 1-9).

### Correction following the first executable-preflight Elm run

- [x] T055 Require actual capability preflight in production/harness before accepting a manifest; no budget renewal or readiness waiver.
- [x] T056 Normalize only unambiguous schema-valid bare review objects; retain citation/provenance validation and reject extra fields.
- [ ] T057 Run focused/preflight checks, CI, and one fresh pinned Elm-only 3600-second profile; preserve prior failed evidence and explicit scope approvals.

- [x] T058 Correct stage-specific source/probe/manifest guidance and schema feedback after the 39.78-second hold; regression covers both malformed envelopes without increasing the two-correction limit.

- [x] T059 Express existing probe restrictions in field schemas and return safe typed correction codes after the 167.5-second Elm hold; four regressions observed red before correction.

- [x] T060 Correct Linux teardown signal delivery after CI reproduced the asyncio pidfd status race; deterministic regressions preserve both exit 0 and exit 7, with all 43 runner tests passing in Linux.

- [x] T061 Admit the runner's strictly bound activation failure as rejected repair evidence; retain unknown/indeterminate holds and all required unrun checks. Exact Elm report replay and nine regression cases verify the boundary.

- [x] T062 Repair harness cleanup of owned read-only directories without following symlinks; preserve rejection when cleanup-container removal is unconfirmed, and verify actual Elm leftovers before retesting.

- [x] T063 Retain the redacted terminal output tail within the unchanged repair excerpt bound; preserve statuses and original-report digest, verified against Elm's actual linker failure.

- [x] T064 Advertise the complete closed assessment envelope and explicitly prohibit mixed/null tool fields; retain rejection of mixed replies and all nested required assessment fields.

- [x] T065 Retain the parent-validated executed probe plan in durable and fresh-context evidence, reject missing/substituted saved provenance, and explicitly communicate required coverage links without relaxing the obligation guard.

## Approved complete execution-source correction

Spec: execution-source-amendment.md; plan: execution-source-plan.md. User approved both.

- [x] T066 Verified binary bundle identity and safe full-tree restoration (plan task1).
- [x] T067 Complete pinned provider acquisition and bounded archive/blob reads (task2).
- [x] T068 Full source for Docker probes and clean verification (task3).
- [x] T069 Kubernetes binary source transport parity and cleanup (task4).
- [x] T070 Source provenance/migration, restart ownership and usable activation (task5).
- [x] T071 Pinned snapshot source adapter and current-generation installer reports (task6).
- [ ] T072 Focused tests, CI, final adversarial review and Elm-only60m simulation (task7). No live-success claim until actual required clean checks and usable activation pass.
