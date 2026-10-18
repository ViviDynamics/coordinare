# Complete Execution Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run bootstrap probes, installation and clean verification against the same complete pinned repository, preserving independent review and deterministic readiness.

**Architecture:** Keep bounded review evidence separate from a content-addressed binary execution-source bundle. A provider adapter obtains a complete pinned tree and archive; a shared validator/materializer validates it for Docker, Kubernetes and the private snapshot harness. Persist compact source identity alongside execution evidence, never archive contents in daemon snapshots or model conversations.

**Tech Stack:** Existing Python 3.12+, Pydantic v2, asyncio, httpx, pathlib, hashlib, tarfile and existing Docker/Kubernetes adapters. No new runtime dependency or language-specific adapter.

**Spec:** [execution-source-amendment.md](execution-source-amendment.md), approved by the user with “Excellent. Approved”. The user approved this plan with “Approve”. Preserve the existing native execution method: implement sequentially in this worktree; one fresh whole-diff adversarial review before merge.

## Global Constraints

- No daemon clone, git invocation, repository hooks or project command execution. Provider API reads and owned temporary files only. The private snapshot harness may read pinned Git objects.
- Review inputs, model context limits, accepted manifest digest, independent review and every required check remain mandatory. Execution data never enlarges model evidence or creates a readiness waiver.
- Source ceilings: 128 MiB compressed archive/transport, 512 MiB expanded file contents, 64 MiB per blob, 100,000 tracked entries, 256 fallback blob fetches, eight concurrent provider reads, 64 KiB transport chunks. Metadata and serialized Kubernetes objects have explicit bounds.
- Source work consumes existing deadlines; it cannot renew model/read/repair quotas. Existing production timeout defaults remain unchanged.
- No automatic scope approvals. No other repository tests until usable Elm activation and every required clean check pass. No merge until existing sample/Elm, CI and review gates are met.
- Preserve prior private artifacts, journal archives and counters, unrelated worktrees, installed skills, quality gates and constitutional text.

## Review Focus

1. A provider archive applies export-ignore/export-subst: complete missing blobs from pinned objects, refuse mismatched content instead of silently accepting it (Task 2).
2. A safe-looking symlink chain becomes cyclic, dangling or escaping: resolve admitted evidence/checkout accesses within the owned root and never write through links (Task 1).
3. A 6,616-file, 77 MB repository exceeds the text-evidence transport but fits execution limits: bounded streaming and chunked metadata work without bigger model context or oversized API objects (Tasks 1, 4).
4. Source acquisition is cancelled or a restart finds an owned resource: retain cleanup obligations and consumed budgets; no new commands before confirmed cleanup (Tasks 2, 3, 5).
5. Tool-version checks pass only via absolute paths while actual project commands cannot resolve tools: independent coverage review rejects incomplete activation, and real consumer tests exercise ordinary command names (Tasks 5, 6).

## File responsibilities and interfaces

Create these focused modules; do not move unrelated code out of existing large files:

- `packages/service_inference/src/coordinare_service_inference/environment/execution_source.py`: strict source identity/entry models, canonical hashes and streaming bundle validation.
- `packages/service_inference/src/coordinare_service_inference/environment/execution_source_restore.py`: safe file/mode/symlink restoration, projected payload reader and CLI.
- `src/coordinare/services/environment_source_provider.py`: provider contract, GitHub API acquisition, temporary artifact ownership and redacted acquisition diagnostics.
- `src/coordinare/services/environment_source_transport.py`: bounded binary stdin framing and Kubernetes source chunk/metadata generation. Keep the existing text `source_payload.py` for evidence-only callers; never treat it as a complete checkout.
- Modify `github.py`, `environment_probe_service.py`, `environment_probe_executor.py`, `environment_verification.py`, `kubernetes_verifier.py`, `daemon.py`, `env_cache.py`, `environment_acceptance.py`, `environment_repair.py`, `environment_probes.py`, `environment_discovery_prompts.py`, environment models, dispatcher contract wiring, and performer `environment_contract.py` only at source/provenance boundaries.
- Modify `scripts/validate_environment_repositories.py` and candidate-image code verification for the shared bundle, actual checkout identity and current-generation reports.
- New `tests/unit/test_353_execution_source.py`, `test_353_execution_source_provider.py`, `test_353_execution_source_pipeline.py`; extend existing source-transport, probe, acceptance, journal, performer-contract and harness tests.

The following interfaces are the shared decisions. Use runtime imports for Pydantic field types as repository conventions require.

- `SourceEntry`: strict `{path: str, mode: Literal['100644','100755','120000'], object_id: str, size: int}`. Validate immutable object identifiers of exactly 40 hex characters (SHA-1) or exactly 64 (SHA-256), with one format per tree. Directory entries are implicit; gitlinks and unsupported modes hold.
- `ExecutionSourceIdentity`: strict `{schema_version: Literal[1], source_commit: str, tree_digest: str, file_count: int, expanded_bytes: int}`; `digest() -> str` is SHA-256 of canonical version/commit/sorted entry metadata. Blob object IDs bind content. Recompression does not affect identity.
- `ExecutionSourceBundle`: host-only dataclass `{identity: ExecutionSourceIdentity, payload_path: Path, payload_sha256: str, payload_bytes: int}`. Its path and payload are never serialized into reports or model messages. The artifact lives only within its owned attempt directory.
- Bundle format: gzip-compressed tar with a fixed `metadata.json` and `files/` prefix. Metadata contains identity and sorted `SourceEntry` records. Transport metadata is bounded separately at 32 MiB expanded, counted within the 512 MiB ceiling; file contents retain the same ceiling. Archive total remains 128 MiB. Provider-specific archive prefixes are removed only during checked normalization, never by generic extraction.
- `validate_execution_bundle(path: Path, *, expected: ExecutionSourceIdentity | None = None, review_inputs: Mapping[str, str] | None = None) -> ExecutionSourceIdentity` in `execution_source.py`: stream headers/contents, check complete entry set/Git object hashes, byte bounds, metadata, link graph and optional review hashes before returning identity.
- `restore_execution_bundle(path: Path, destination: Path, *, expected: ExecutionSourceIdentity, review_inputs: Mapping[str, str]) -> None` in `execution_source_restore.py`: revalidate, create a fresh owned root and safely restore bytes/modes/links. Refuse an existing destination and incomplete source.
- `SourceProvider` protocol: `async prepare_execution_source(owner: str, repo: str, *, commit: str, destination: Path, deadline: float) -> ExecutionSourceBundle`. The caller owns `destination`; provider-created paths stay beneath it. Cleanup belongs to the caller on success, error and cancellation.
- `ensure_execution_source(state: Mapping[str, Any], name: str, cache: EnvCacheState, *, destination: Path, deadline: float) -> ExecutionSourceBundle` in `environment_source_provider.py`: select actual provider, enforce accepted source/inputs and current target, return validated artifact. No review-only fallback.

## Task 1: Verified binary source and safe restoration

**Interfaces:** Produces the shared types, validator and restorer defined above. Consumes no daemon state or provider credentials.

- [ ] Before product changes, add amendment task IDs to `tasks.md`, read both approved artifacts into a new native execution ledger, and run repository `speckit.analyze` consistency analysis over spec/plan/tasks. Record the exact invocation/output and resolve blocking inconsistencies; preserve earlier ledgers and budgets.
- [ ] Write `test_complete_tree_preserves_unreviewed_binary_executable_and_link`: fixture has one reviewed build file, an unreviewed source directory, non-UTF-8 asset, executable script and relative symlink; restoration retains all bytes, executable bit and link target. Assert an extra/missing member and mismatched review hash raise before any command runs.
- [ ] Write parameterized rejects for traversal, absolute names, duplicate/file-directory conflicts, hard links/devices/FIFOs, escaping/cyclic/dangling link chains, bogus Git blob hashes and unsupported modes/LFS content. Include an unchanged external sentinel to prove restoration never writes through links. Permit normal relative symlinks into admitted tracked files.
- [ ] Write streaming bound tests with small injected limits: compressed overflow, expanded overflow including metadata, per-blob overflow and entry overflow fail at the first excess read. Test exact 40/64-character commit/object formats and stable identity across archive recompression/order changes.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_execution_source.py`; observe failure for missing behavior, then implement the exact interfaces and CLI `python -I -m coordinare_service_inference.environment.execution_source_restore <payload-directory> <destination>`.
- [ ] Restore regular files/directories using anchored descriptors/no-follow writes; create validated symlinks last. Normalize permissions to 0600/0700 plus executable bit, without owner/setid restoration. Do not call unrestricted `extractall`.
- [ ] Rerun the new tests and existing `test_353_source_payload.py`/`test_351_source_transfer_bounds.py`; commit the shared bundle/restorer.

## Task 2: Complete pinned provider acquisition

**Interfaces:** Implements `SourceProvider`, `ExecutionSourceBundle` acquisition and `ensure_execution_source`. Add `GitHubService.prepare_execution_source(...)` delegating to the provider helper, using its existing API base and current token. Extend relevant repository-service protocols, not only the concrete class.

- [ ] Write `test_archive_acquisition_uses_one_tree_and_one_archive_for_complete_source`: fake GitHub serves tree metadata and binary archive at a literal commit; assert no per-file API requests, no git subprocess, exact canonical identity and all original bytes/modes.
- [ ] Write `test_export_ignored_blobs_are_completed_from_pinned_objects` and substituted-content rejection: fallback only for genuinely missing tree blobs, at most 256, concurrency at most eight. An existing mismatched blob is corruption, not an omission to overwrite. Validate content/object identity for every fallback.
- [ ] Test truncated/duplicate trees, mutable ref, submodule/LFS, unsupported object format, archive redirects and authentication. Only the configured trusted GitHub API origin receives the token; signed external archive redirects receive no token and must use HTTPS. Restrict redirect count to three; reject credentials in URLs, downgrade and unexpected API-origin changes.
- [ ] Test 100,000-entry boundary, partial/oversized bodies, cancellation and expired deadlines with owned temporary paths; no bytes logged, no resource leaked and no successful partial result. Source-read limits are separate from model evidence reads, while the existing attempt deadline remains authoritative.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_execution_source_provider.py` RED; implement streaming acquisition/normalization and compact timing/count diagnostics with the task interfaces; rerun GREEN. Commit provider support.

## Task 3: Full source in Docker probes and clean verification

**Interfaces:** `execute_environment_probe(..., source_bundle: ExecutionSourceBundle, budget: AttemptBudget) -> ProbeReport` replaces its review-only `source_files` argument. `verification_execution_source(state, name, cache, *, destination: Path, deadline: float) -> ExecutionSourceBundle` in `environment_verification.py` delegates to Task 2. `source_setup` becomes binary source transport setup, not review-file materialization. Existing review fetching/independent hash checks remain.

- [ ] Write `test_probe_and_clean_verifier_see_unreviewed_application_directory`: review one build file, execute a directory/content assertion against unreviewed source. The existing review-only path must fail this test before the patch. Verify a corrupted review hash prevents Docker creation.
- [ ] Extend probe isolation tests: mount fully restored source read-only; preserve per-command container destruction, disabled network/cache writes and existing resource bounds. Source preparation happens once per batch, not once per command. No new model calls or hidden budget reset.
- [ ] Add binary stdin framing to `environment_source_transport.py`: a bounded trusted header containing lengths/digests/identity/review hashes, exact raw bundle bytes, then the trusted verifier script. Bootstrap with a fixed trusted interpreter; validate frame bounds before allocation and consume exact lengths. Never place source bytes in exec arguments or env; reject trailing/missing frames. Interpreter restores before credentials/project commands.
- [ ] Test source acquisition/restoration cancellation, malformed frames, corrupted/reordered/extra chunks, expired deadline and cleanup failure; keep the owned source directory until all commands/containers are drained, then remove it. The attempt cannot start another generation with outstanding source ownership.
- [ ] Run focused execution-source-pipeline/probe/source-transport tests RED; wire `run_owned_probe`, Docker clean verification and actual deadline/cleanup ownership to Task 2/Task 1. Run GREEN and commit.

## Task 4: Kubernetes binary source transport

**Interfaces:** Extend `VerifierSpec` with `execution_source: ExecutionSourceBundle | None` and compact expected source identity. Retain `walk_only` behavior; a real source-dependent verification requires a bundle. `build_verifier_source_secrets` and `_mount_source_payload` use Task 3 transport helpers and Task 1 restoration CLI. `daemon.py`'s verifier factory supplies the actual pinned bundle through its async preparation boundary.

- [ ] Write `test_kubernetes_restores_same_full_tree_identity_as_docker`: reconstruct projected binary payload and metadata through the actual shared CLI, assert identical bytes/modes/links/identity. An evidence-only mapping cannot satisfy this path.
- [ ] Write large-tree tests representing Elm's 6,616 entries/77,426,246 bytes, including a 1,172,711-byte binary file: metadata is chunked as well as archive content; no source in Pod command/env; every generated Secret/Pod is under the existing serialized object ceiling. Preserve 64 KiB chunks and bounded grouping.
- [ ] Test partial Secret creation, wrong Pod UID, admission refusal, missing/projected extra chunk, corrupt metadata and cancellation. Confirm each owned object is removed or retain a cleanup hold. Do not copy private payloads to logs.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_source_transport.py tests/unit/test_353_execution_source_pipeline.py` RED, implement the binary projected-volume extension and factory wiring, then rerun GREEN with existing Kubernetes verifier tests. Commit.

## Task 5: Source provenance, restart safety and usable activation

**Interfaces:** Add compact `environment_execution_source: dict[str, Any] | None` to live/persisted cache state and matching dispatch metadata. Add `execution_source_digest: str | None = None` to `VerificationReport`, `ProbePlan` and `ProbeReport`; absence remains parseable for diagnosis but cannot satisfy new readiness or saved probe admission. The source digest is parent-owned and included in the probe plan digest. `execution_report`, `saved_execution_accepted` and performer contract execution compare expected commit/source digest, manifest digest, image/platform and every required result.

- [ ] Write positives with exact source identity and negatives for missing source identity, old report replay, changed tree/commit/image, forged model-owned identity and mismatched installer/consumer. Assert neither copied parent metadata nor an arbitrary syntactically valid digest can replace actual validated source/checkout evidence.
- [ ] Extend performer `execute_contract` preflight: derive actual tracked checkout identity before install/project commands using Git object reads in the performer, with global/system config disabled and no hooks/filters; compare with the daemon descriptor and review hashes. Match symlink evidence semantics. A dirty tracked source or wrong checkout holds. Do not add git subprocesses to daemon modules.
- [ ] Version the repair journal to 4 with compact source identity and durable owned-source cleanup marker. Preserve all existing counters/expiry/rejected reports. Old pending v3 attempts require explicit restart after confirmed cleanup; old frozen reports require re-verification/rebuild. Test roundtrip, concurrent ownership, source change, cancellation and reset refusal. Do not serialize 6,616 entries or payloads into the journal.
- [ ] Add `test_independent_review_receives_activation_usability_requirement` to discovery/review prompt coverage, plus an actual synthetic clean consumer command that invokes an installed tool by its ordinary name using declared activation. Missing activation directory stays a failure; do not waive it or remove required links/checks. The agent may repair installation to create a valid activation path.
- [ ] Run the related acceptance, performer-contract, journal, source and probe tests RED/GREEN. Run strict mypy for touched modules and commit provenance/migration changes without weakening gates.

## Task 6: Production-path harness and truthful current-generation results

**Interfaces:** `Snapshot.prepare_execution_source(owner, repo, *, commit, destination, deadline) -> ExecutionSourceBundle` reads the exact pinned objects and uses the shared bundle contract. `_execute` always records `installer_evidence` for the current attempt, on pass or fail, and never carries previous clean evidence into a new attempt. Final summary includes that attempt's source identity/digest.

- [ ] Write `test_snapshot_bundle_ignores_mutable_checkout_and_preserves_modes`: use pinned objects containing binary/executable/symlink content and a hostile host git config; extraction gets the commit's bytes, not local worktree changes or export filters. Snapshot mutation detection remains active.
- [ ] Write `test_current_installer_report_replaces_previous_failed_generation`: two failed generations then a passing installer produce only current-digest installer evidence in the final summary. Prior artifact files remain byte-identical; required checks and repair counters stay intact.
- [ ] Write an end-to-end synthetic harness scenario with one review file, an unreviewed source directory, cached installed tool and ordinary-name project build check. Installer modifications must not appear in the fresh consumer. Assert full source/image/manifest identities match and no scope marker can waive a failed check.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_validation_harness.py tests/unit/test_353_execution_source_pipeline.py` RED; implement the snapshot adapter/current-attempt summary, extend candidate image verification for new shared modules; rerun GREEN. Commit.

## Task 7: Verification, final review and Elm simulation

- [ ] Update `tasks.md`, onboarding env-cache docs and `validation.md` with source bounds, old-cache restart/rebuild behavior and source-specific holds. Recheck consistency against the final implementation; never mark live success in advance.
- [ ] Run the focused source/provider/probe/acceptance/harness suites, lint and strict type checks. Execute the ship-issue preflight and quality guard on every pushed commit, preserving existing justified annotations and all retry/rebase budgets. Broad suite runs through CI; T3 owns CI watching, no custom CI polls.
- [ ] Run a native end-to-end Docker smoke with a small synthetic repository, binary asset, symlink, executable tool and full-source build check. Reproduce missing-directory failure with the old behavior and demonstrate correction using the real shared adapter; verify network disabled and read-only cache in the consumer.
- [ ] Commit/push the tested correction to PR #354. Obtain the required fresh full-diff adversarial review, execute every actionable reproduction, fix valid findings and rerun affected checks. Track the review round and final exact head; no claim based on old-head green CI.
- [ ] Create a new private Elm-only profile, preserving all previous profiles. Use commit `bc224a78db6f669d56863599d653f14e7605de0b`, `spark/glm-5.3-flash`, low effort, 32,768 max output tokens and 3,600-second phase/aggregate/provider/installer limits. Bind the candidate image to the pushed implementation head. Production defaults stay unchanged.
- [ ] Independently inspect exact probe/manifest argv/env/paths, verify source/review hashes, and write only digest-bound scope approvals. Use the one-shot artifact notifier, not automatic approvals. Report source transfer timings/counts separately from model/installer/check time, and preserve finite schema/coverage/execution budgets.
- [ ] On completion, confirm snapshot unchanged, all owned containers/volumes/images/source objects removed, installation and every required clean check passed with matching identities, and installed tools usable through declared activation. Record truthful evidence in `validation.md` and the checkpoint. On failure retain the actual code/output and hold readiness; do not edit the manifest by hand, reset the attempt or expand quotas to manufacture success.
- [ ] Resume other repositories only after Elm passes. Existing sample/Elm SC-004, final-head CI and review gates still control merging; retain PR #354 until all are met, then use ship-issue/merge-pr and unwatch before actual handback.

## Plan self-review and handoff

The seven tasks cover all approved spec sections: binary completeness/identity and
safe restoration (1), provider integrity/limits (2), Docker isolation and lifecycle
(3), Kubernetes parity/object bounds (4), provenance/migration/activation (5),
snapshot harness and result attribution (6), verification/review/Elm (7). The five
review-focus conditions each have named tests in their owning tasks. No runtime
code is part of this planning commit. Written-plan review is the remaining gate;
native execution stays selected. No additional timeout or model decision is needed.
