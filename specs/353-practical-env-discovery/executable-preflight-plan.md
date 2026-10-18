# Bootstrap Evidence and Executable Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans task-by-task with the previously selected native execution method. Obtain one fresh independent whole-branch review after implementation. Written-plan review is required before execution.

**Goal:** Repair from complete evidence, discover actual image capabilities through bounded isolated execution, and verify required installation outputs before dependent work.

**Architecture:** Share mandatory evidence preparation between repair and independent review. A separate versioned probe contract travels through a trusted executor callback and durable attempt journal; probes inform manifest construction but cannot establish readiness. Installation assertions use existing required commands and dependency edges, preserving the environment manifest schema and digest rules.

**Tech Stack:** Existing Python, asyncio, pydantic, pytest, Docker/Kubernetes adapters; no new dependencies.

**Spec:** [executable-preflight-amendment.md](executable-preflight-amendment.md), approved by the user. Earlier amendments and failed validation results remain historical evidence.

## Global Constraints

- No subagents or new model routing in bootstrap. No ecosystem-specific production rules or edits to consumer source.
- Independent coverage acceptance AND deterministic successful execution of every required applicable check/service action remain mandatory. Probe results and install assertions cannot waive clean checks.
- Same accepted manifest digest for installer/consumer; immutable source inventory, image/platform and capability identities. No synthetic runtime evidence in manifest source inputs.
- Original attempt expiry; 600-second aggregate default, 120-second model-phase default, maximum 3600 seconds, 128 calls, 512 reads. Separate schema/coverage/execution limits remain default two, configurable zero through two.
- At most two probe batches per logical attempt, 16 unique commands per batch, 30 seconds per command, 120 seconds per batch, capped by remaining time. At most one executor active.
- Probe output: at most 16 KiB combined stdout/stderr per command and 256 KiB per batch. Truncation is explicit and cannot establish a positive capability.
- Probe runtime: pinned read-only source, empty read-only cache, disposable writable scratch, network disabled, no runtime secrets or host sockets. Unsupported isolation holds; no host execution fallback.
- Checkpoint reservations/ownership before external actions. Cancellation and uncertainty retain cleanup obligations. Never replenish consumption on restart, candidate change, or cache-generation change.
- Broad suite via CI, focused local TDD. Preserve private evidence, unrelated worktrees and current quality gates. Elm only until actual clean success.

## Review Focus

1. Two concurrent callbacks must not reserve a third batch or launch overlapping executors (Task 2/3 tests).
2. A command can fork or emit output indefinitely despite its declared purpose; bounds and cleanup must cover descendants and pipe backpressure (Task 3 tests).
3. A successful probe of the base image becomes stale when image/platform changes during dispatch (Task 4 tests).
4. Coverage revisions must not discard new probe-relevant source inputs or erase earlier required installation assertions (Task 4 tests).
5. An interrupted scope-approval wait must not approve another proposal, renew expiry, or strand owned resources (Task 5 tests).

## File responsibilities and interfaces

- `src/coordinare/services/environment_evidence.py` (new): shared independent mandatory-input preparation and serialized conversation admission. Avoid adding another source reader.
- `src/coordinare/services/environment_discovery.py`: discovery/review messages, probe envelope dispatch, install-assertion guidance, full obligation validation.
- `src/coordinare/services/environment_probes.py` (new): proposal/report types, identity validation, probe runner protocol, output admission. Keep separate from accepted manifest schema.
- `src/coordinare/services/environment_probe_executor.py` (new): disposable Docker execution and explicit unsupported-platform refusal; shared daemon/harness entrypoint.
- `src/coordinare/services/environment_attempt.py`, `environment_repair.py`: shared batch reservation, version-3 journal, probe audit/ownership/cleanup state.
- `src/coordinare/services/env_cache.py`: wire executor using actual runtime configuration; keep implementation in focused helpers.
- `packages/service_inference/src/coordinare_service_inference/environment/runner.py`: preserve existing install action/dependency execution; change only if a behavioral test demonstrates a gap.
- `scripts/validate_environment_repositories.py`, `scripts/environment_validation_image.py`: per-proposal scope approval, shared executor, safe actual-stage outcomes and owned cleanup.
- New `tests/unit/test_353_repair_evidence.py`, `test_353_environment_probes.py`, `test_353_probe_executor.py`, `test_353_probe_pipeline.py`: synthetic behavioral fixtures. Extend existing journal, snapshot, harness and runner tests.
- `docs/onboarding/06-env-cache.md`, feature `tasks.md`/`validation.md`: operator limits, migration, actual results. No timeout-default change.

## Execution prerequisites

- [ ] After written-plan approval, read both approved artifacts and create the native execution ledger in `.superpowers/sdd/executable-preflight-plan/`. Preserve sibling ledgers and historical budgets.
- [ ] Add amendment tasks to the existing feature tracking and run `speckit.analyze` consistency analysis before Task 1. Resolve blocking spec/plan/task inconsistencies before implementation.

## Task 1: Complete mandatory repair evidence

**Consumes:** Existing `SourceInspection.read_files`, `EnvironmentManifest.inputs`, `AttemptBudget` and configured context limit.

**Produces:** In `environment_evidence.py`, async `prepare_mandatory_evidence(inspection: SourceInspection, expected_inputs: Mapping[str, str], *, phase: str) -> list[dict[str, str]]` and `check_evidence_context(messages: list[dict[str, str]], *, max_bytes: int, phase: str) -> int`. Existing review entrypoints delegate to these helpers; no changed public discovery signature yet.

- [ ] Write `test_repair_receives_all_18_inputs_before_first_call`: previous manifest has 18 inputs, scripted repair requests no file reads; assert all 18 exact contents/hashes are delivered and independently fetched. Assert the subsequent reviewer fetches all 18 again.
- [ ] Write missing/hash-drift negatives, initial/growing context overflow, read-limit exhaustion, rejected checkpoint before fetch, and cancellation draining pending reads. Assert no next provider/executor call. Missing evidence and hash drift have distinct phase-specific codes.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_repair_evidence.py`; expect failures for absent mandatory repair delivery, not malformed test fixtures.
- [ ] Extract preparation/context admission from `environment_discovery.py` without changing review semantics. Await repair preparation before its initial model call; apply full-context admission to repair and all later tool results. Preserve final prior-input and review citation validators.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_repair_evidence.py tests/unit/test_353_review_evidence.py tests/unit/test_353_authoritative_provenance.py`; expect all pass. Commit `fix: deliver complete source evidence to environment repair`.

## Task 2: Probe contracts and durable admission

**Produces in `environment_probes.py`:**

- `ProbeRequest`: strict version-1 proposal, `commands: list[Command]` length 1..16, `citations: dict[str, list[str]]` and `questions: dict[str, str]` keyed by exact command IDs. IDs unique; required true, scope empty, dependencies empty, timeout at most 30. Validate citations against actual inspected inputs; questions nonempty and at most 512 characters. Entire proposal obeys existing model-output bounds.
- `ProbePlan`: trusted request plus commit, complete inventory digest, actual source-input hashes and execution capabilities; canonical `digest() -> str`. Models cannot provide trusted identity fields.
- `ProbeCommandResult`: command ID, status (`completed`, `timed_out`, `execution_error`), integer exit code only for completed execution, output, truncation flag.
- `ProbeReport`: version one, plan/source/image/platform identities, ordered results for exactly every requested ID, confirmed-cleanup boolean. Validate IDs/status/exit consistency, output byte bounds and target identity through `validate_probe_report(plan: ProbePlan, report: ProbeReport) -> None`.
- `ProbeExecutor = Callable[[ProbePlan], Awaitable[ProbeReport]]`. Callback is trusted runtime orchestration; model data never chooses transport.

**Persistence:** `AttemptBudget.reserve_probe_batch() -> None`, `probe_batches` consumed/max two in summaries. Version-3 `RepairJournal` stores consumed batches, bounded reports, current plan digest and ownership status. Add shared `reserve_probe(cache: EnvCacheState, plan: ProbePlan) -> None` and `finish_probe(cache: EnvCacheState, report: ProbeReport) -> None`; strict checkpoint remains caller-owned before dispatch. Reuse existing cleanup refusal and operator archival.

- [ ] Write proposal validation tests: arbitrary executable accepted, ecosystem names irrelevant, 17 commands/duplicate IDs/unread citations/startup-hook env/nonfinite timeout rejected. Model-supplied identity rejected.
- [ ] Write report tests: exact IDs, missing/extra/duplicated results, wrong identity, completed without exit, nonzero observed exit, truncation and UTF-8 byte limits. Treat all output as untrusted text.
- [ ] Write journal tests for two reservations, third refused, no refund after failure/restart, one active ownership slot, snapshot roundtrip, failed checkpoint, and old v2 pending journals requiring explicit restart. Existing ready frozen v1/v2 contracts stay readable.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_environment_probes.py tests/unit/test_353_execution_repair.py tests/unit/test_daemon_snapshot_persistence.py`; watch intended failures, implement types/admission and nondecreasing version-3 state, then repeat to green.
- [ ] Commit `feat: add bounded durable environment probe contracts`.

## Task 3: Disposable bounded probe executor

**Produces:** `execute_environment_probe(plan: ProbePlan, *, state: Mapping[str, Any], source_files: Mapping[str, str], budget: AttemptBudget) -> ProbeReport` in `environment_probe_executor.py`. Docker implementation reuses source-payload reconstruction and existing runtime image/platform resolution. Adapter selection is explicit; unsupported platform isolation returns a typed hold before resource creation.

**Boundary:** Implement Docker isolation for Elm. Kubernetes uses the same protocol but holds with `probe_isolation_unsupported` unless its adapter can establish all specified restrictions; do not claim a plain verifier pod proves network isolation. This is an explicit supported-capability boundary from FR-003, not a Docker fallback for Kubernetes.

- [ ] Write command-construction tests for immutable image/platform, network none, no injected secrets/socket, read-only source and empty cache, writable scratch, shell-hook sanitization and explicit roots. Do not reuse a failed installation cache or execute probe text on the host.
- [ ] Write executor tests for missing executable yielding completed/nonzero observation, exact outputs, 30-second command/120-second batch limits capped by remaining budget, multibyte output bounds, truncation, and continuously writing/forking commands. Verify container removal kills all owned work, including cancellation and late resource creation.
- [ ] Write admission tests for concurrent requests, Kubernetes unsupported isolation, changed source during payload transfer, partial container creation failure, malformed report and unconfirmed cleanup. Subsequent external work must remain blocked when ownership is uncertain.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_probe_executor.py`; watch intended failures. Implement one bounded execution supervisor inside the disposable container, using explicit argv and bounded stream readers. Persist owner before create and retain it until confirmed removal.
- [ ] Repeat executor tests plus affected existing source-transfer/cleanup tests; expect pass. Run a disposable synthetic unfamiliar-command integration case on the actual available Docker backend and independently inspect resource absence.
- [ ] Commit `feat: execute environment probes in disposable consumers`.

## Task 4: Discovery, repair, review and install assertions

**Consumes:** Tasks 1-3 interfaces. Extend existing `discover_and_review` option keywords with `probe_executor: ProbeExecutor | None`; no executor means typed refusal if a probe is requested. Existing callers that never request probes remain compatible. Add async `_execute_probe_proposal(data: dict[str, Any], *, inspection: SourceInspection, options: _DiscoveryOptions) -> ProbeReport` in a focused helper if needed to avoid further expanding discovery.

**Produces:** Discovery supports `{"probe": <ProbeRequest>}` alongside existing read/list/final envelopes. The daemon binds `ProbePlan`, reserves/checkpoints its batch, executes the callback, validates its report and only then appends untrusted observations. Independent review receives validated reports/digests in its fresh context; no discoverer transcript. Repair receives complete prior inputs, failures and existing probe observations. No probe is a required check substitute.

- [ ] Write synthetic pipeline tests: nested arbitrary tool absent in a real-shaped probe report, evidence-backed install proposal, independent accepted review, required clean check still mandatory. Assert source inputs exclude runtime reports and review includes report digests.
- [ ] Test schema correction, coverage revision and executable repair share original deadline/counters/two-batch cap. Test image/platform change after observation holds; findings cannot be silently omitted from the review context.
- [ ] Add `test_revision_preserves_install_assertion_obligations`: required assertion IDs, status and scope cannot be dropped or narrowed after a schema correction, revision or execution repair. Extend existing obligation ledger to include required install assertions, without locking malformed declarations into validity.
- [ ] Add producer/assertion/consumer runner regression: producer exits zero but expected artifact absent; assertion fails, dependent consumer is not_run, clean readiness false. Portable repaired generation must rerun every required action. Use existing install `depends_on`; do not add an artifact filename registry or manifest schema.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_probe_pipeline.py tests/unit/test_353_bootstrap_pipeline.py tests/unit/test_353_executable_pipeline.py` and the existing shared runner tests discovered under `agent/performer/tests`/`packages`; watch intended failures before implementation.
- [ ] Implement model guidance requiring executable postconditions before consuming persisted artifacts, with fresh coverage review responsible for evidence-based completeness. Wire actual capabilities and the shared executor from `env_cache.py`; preserve final acceptance checks.
- [ ] Repeat focused tests, strict mypy and lint with unchanged baselines; expected pass. Commit `feat: integrate capability observations and install postconditions`.

## Task 5: Harness, scope approval and accurate diagnostics

**Produces:** `probe_authorized(path: Path, plan: ProbePlan) -> bool` in the validation harness, using a separate `probe-approved.json` marker bound to exact plan/source/image/platform identity. Record `probe-proposal.json` and bounded report artifacts in private per-batch directories. Existing per-manifest execution approval remains distinct.

- [ ] Add tests for no probe execution before explicit scope approval, wrong digest/source/image/platform marker refusal, interrupted approval wait, unchanged original expiry and cleanup after timeout. Existing manifest markers cannot authorize probes, or vice versa.
- [ ] Add final-stage regression: installer failure followed by repair evidence failure reports `repair_preparation`/its code, rather than stale `installer`. Expose consumed/max batches, required/delivered inputs and actual elapsed stage through safe diagnostics; redact source, credentials, output and rationale in operator summaries.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_validation_harness.py tests/unit/test_353_probe_pipeline.py tests/unit/test_141_server_wording.py tests/unit/test_203_dashboard_redaction.py tests/unit/test_436_dashboard_decomposition.py`; watch new cases fail, implement shared executor wiring and private artifacts, repeat to green.
- [ ] Update operator docs for probes, unchanged defaults, adapter holds and explicit legacy restart. Keep amendment task completion and the native execution ledger synchronized with the implemented behavior.
- [ ] Commit `feat: expose scoped probe validation and precise bootstrap outcomes`.

## Task 6: Review, CI and Elm-first live validation

**Consumes:** All earlier tasks. Keep the existing isolated branch, issue assignment and PR #354; preserve historical review/budget outcomes rather than resetting counters.

- [ ] Self-review the full diff, then obtain one fresh independent full-branch adversarial review through native execution's required reviewer. Address Important/Critical findings in one red/green fix pass. Do not treat scripted coverage acceptance as a real repository judgment.
- [ ] Before every push: commit, focused preflight, unchanged quality guard, preflight `--check 353`, then push. Broad suite through CI; inspect failures under ci-safety. No bypasses or old-head success claims.
- [ ] Start one clearly labeled fresh Elm-only profile with pinned original source, actual image/platform, `spark/glm-5.3-flash`, low reasoning, output 32768, and phase/aggregate/provider/installer allowances 3600 seconds. Preserve previous attempts and do not modify a running attempt's expiry.
- [ ] Inspect each probe plan before its scope marker and every new accepted manifest before its separate execution marker. Run installation, independent clean verification, required checks and lifecycle actions. Record all actual stages and bounded repairs; if held, investigate before another attempt.
- [ ] Independently verify unchanged source hashes and absence of owned containers, ephemeral image refs, registry storage and volumes. Preserve failed reports/transcripts privately; update `validation.md` and PR body with exact results and comparison limits.
- [ ] Resume other repositories only after Elm's mandatory independent review and every required clean check pass. Sample-plus-Elm live-success criteria still gate merge. If held, leave PR open, explain the exact blocker and unwatch before handback. Do not expand the matrix automatically on a successful probe alone.
- [ ] Commit final observed validation documentation; repeat necessary preflight/CI for that head and complete the existing ship-issue gates only if actual merge criteria are met.

## Self-review and execution handoff

Spec coverage: FR-001 -> Task 1; FR-002/006 -> Task 2; FR-003 -> Task 3;
FR-004/005 -> Task 4; FR-007 -> Task 5; all success/acceptance scenarios -> Tasks 1-6.
Each Review Focus condition has a named owning test. Probe plan/report identities stay
outside manifest file hashes. Types and batch limits are shared; daemon and harness
use one executor. Existing command dependency semantics supply installation assertions.

The prior native implementation method is preserved. Implementation follows written
plan approval and consistency analysis. A successful implementation test suite cannot
replace the outstanding live Elm/sample acceptance gate.
