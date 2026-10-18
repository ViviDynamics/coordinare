# Elm bootstrap completion implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this approved plan task-by-task, with independent final review.

**Goal:** Demonstrate complete source-backed Elm bootstrap and all required clean-consumer checks, then complete issue #353 through its existing shipping gates.

**Architecture:** First prove an optional private service guest usable by ordinary outer commands. Then make independently reviewed source scope an input to one executable-manifest construction, retaining final independent review and deterministic execution. Neither a runtime fixture nor an accepted manifest is readiness.

**Tech Stack:** Existing Python 3.12+, Pydantic v2, asyncio and manifest runner; optional QEMU software guest, Docker/Compose and SSH platform tools. No new agent framework or ecosystem catalog.

**Spec:** [coverage-first-amendment.md](coverage-first-amendment.md), [private-service-runtime-amendment.md](private-service-runtime-amendment.md), and existing [spec.md](spec.md).

**Status:** Approved by the user's "Yes" to the presented designs and plan. Native execution with independent review. Independent planning review is retained privately at `.agents/state/353-elm-fix-subagent-plan.md`. Its two corrections are incorporated: runtime proof comes first, and construction initially remains one response rather than fan-out.

## Global constraints

- Elm first at `bc224a78db6f669d56863599d653f14e7605de0b`; no broader runs until genuine Elm success.
- Mandatory independent coverage approval PLUS deterministic successful execution of every required check in a clean consumer using the same accepted manifest and immutable image identity.
- Failed, unknown, missing or unrun checks cannot be waived. Required service lifecycle and teardown failures hold. No operator-edited generated manifests or consumer source patches.
- No language/framework catalogs or project-specific core recipes. Private service runtime is explicitly configured, never automatically inferred from a repository.
- No host daemon socket, new outer privileges/devices, enabled clean egress, scope narrowing or global cleanup/pruning.
- One original deadline/call/read budget and two schema, two coverage and two execution repairs. No per-stage renewal. Retain the GLM low-effort, 32,768-token, canonical-JSON, 3,600-second Elm comparison profile.
- Same manifest/source/scope/runtime/image/platform identities across installer, verifier and ordinary downstream consumer. Actual cache read-only; fresh writable source/scratch.
- Full regression suite through CI; focused meaningful tests locally. Final full-diff adversarial review. Do not merge on CI green alone.

## Review focus

1. Verifier-only guest works while ordinary performer tools cannot reach it: pin downstream-shell behavior in Task 3.
2. A writable guest overlay conceals writable installation cache or retained secrets: pin both-side write denial, artifact hashing and credential absence in Tasks 1–3.
3. ARM carrier silently excludes an amd64 service or nested guest requirement: pin mixed-platform execution and measured resource limits in Tasks 1 and 7.
4. Reviewed scope misses a nested source obligation or a version check replaces operational use: source-first omissions and final semantic-review cases in Tasks 4–5.
5. Restart, partial cleanup or identity drift reuses stale readiness or fresh budgets: pin recovery, rejection and complete teardown in Task 6.

## File boundaries

New shared `environment/runtime.py` owns runtime/endpoint identity; `environment/runtime_supervisor.py` owns process, scratch, export and control lifecycle. Performer `environment_runtime.py` connects that facility to the existing job lifecycle. Discovery never constructs guest internals.

New shared `environment/scope.py` owns source criteria/contracts; daemon `environment_scope.py` orchestrates read-only author/reviewer stages using existing evidence tools and findings. Keep existing executable commands and `run_manifest` as the vocabulary, rather than adding per-framework executors.

Paths below rooted at `packages/service_inference/src/coordinare_service_inference/` use the prefix `shared/` for readability. This is a notation, not a new directory.

### Task 1: Prove the integrated runtime candidate before productizing it

**Files:** Create `scripts/probe_private_service_runtime.py`, `tests/integration/test_353_private_service_runtime.py`, and a synthetic source fixture under `tests/fixtures/private_service_runtime/`; update `runtime-feasibility.md` with measured results. Diagnostic artifacts remain private.

**Interfaces:** A diagnostic fixture accepts an immutable carrier image, pinned guest inputs, fresh source/cache/scratch roots, explicit CPU/memory/cache/scratch/free-reserve ceilings and an absolute deadline. It refuses missing resource bounds and persists them before starting a build. It emits a bounded JSON result with identity, per-case exit status, isolation inspections, duration/resource measurements and owned cleanup. It cannot issue readiness or modify a generated Elm manifest. Enable real fixture execution with `COORDINARE_RUNTIME_INTEGRATION=1`; absence means unavailable evidence, never a passing runtime gate.

- [x] Write failing integration cases: ordinary Docker build/run and Compose multi-service health/down from real source; host-side client reaches service; repeat from relocated fresh source with cache RO and external network denied. Assert complete source hash/mode identity and offline build/image closure, not just version output.
- [x] Add negative cases: missing cached image/layer; guest-to-host/proxy/metadata egress; parent-path export; cache write from outer/guest; two consumers using identical project ports; cancellation during boot/build/health/stop. Every failure holds and owned cleanup is confirmed.
- [x] Measure backing-store free capacity before builds. Record explicit byte ceilings and a free-space reserve before the fixture runs. Stop on insufficient space; never prune unrelated resources. Current 57 GiB free is an observation, not a sizing promise.
- [x] Run the fixture and observe the missing integrated facility fail. Implement only the diagnostic guest/control/source-export experiment needed for these cases, using software TCG, identical source paths, strict SSH host verification and explicit loopback forwards. Restriction flags alone are insufficient: denial cases must actually execute.
- [x] Demonstrate required amd64 workloads on the actual arm64 carrier and measure nested-emulation requirements without deployment/hardware operations. Record cold/warm time, peak memory, cache/scratch growth and immutable before/after hashes.
- [x] Run `COORDINARE_RUNTIME_INTEGRATION=1 .venv/bin/pytest -q -m integration tests/integration/test_353_private_service_runtime.py` against the persisted bounded fixture configuration. Expected: every positive, denial, resource and cleanup case passes. A skipped fixture is not success. Commit the fixture/report.

**Stop gate:** If path sharing, access, offline closure, mixed architecture, policy enforcement or capacity fails, report the exact measurement and review the runtime design. Do not proceed to another full Elm model run or silently substitute a whole-performer VM.

### Task 2: Define explicit runtime identity and durable artifact contracts

**Files:** Create `shared/environment/runtime.py`; modify `shared/environment/models.py`, `src/coordinare/services/environment_capabilities.py`, `environment_repair.py`, `src/coordinare/models/env_cache.py`; test `tests/unit/test_353_runtime_identity.py`.

**Interfaces:** Define versioned `RuntimeIdentity` (carrier image/platform, configured platform guest-base digest/platform, emulator/config digest), `RuntimeEndpoint` (ID plus explicit guest/outer-loopback ports) and `RuntimeSpec` (identity, endpoints and measured resource ceilings). All use canonical digests. `RuntimeArtifactReceipt` binds installed runtime-state hashes to the accepted manifest/source/runtime identities after clean installer shutdown. This avoids making a manifest depend on hashes of its own future installation outputs. `ExecutionCapabilities` gains an explicit optional runtime contract in a new version; legacy native execution retains its meaning. Models never supply trusted identity hashes.

- [ ] Write identity tests that reject unknown versions, mutable/missing guest hashes, carrier/guest platform confusion, endpoint collisions/unsafe binds and unbounded resource declarations. Assert any identity/config change invalidates cache/report reuse; do not alias guest platform to outer platform.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_runtime_identity.py` and observe red. Implement the models and capability/journal migration, binding resolved actual artifacts rather than model claims.
- [ ] Rerun the identity tests plus existing capability/journal regressions. Expected: native legacy records remain readable; runtime-required records lacking identity hold. Commit.

### Task 3: Supply the runtime to both verification and ordinary consumers

**Files:** Create `shared/environment/runtime_supervisor.py`, `agent/performer/src/performer/environment_runtime.py`, an optional runtime image under `agent/performer/runtime/`; modify `agent/performer/src/performer/environment_contract.py`, `src/coordinare/services/env_manifest.py`, `env_cache.py`, `scripts/validate_environment_repositories.py`, `scripts/environment_validation_image.py`. Integrate existing `workspace.py` / `server/job_runner.py` lifetime only where necessary. Test `tests/integration/test_353_runtime_consumer.py` and `agent/performer/tests/test_353_environment_runtime.py`.

**Interfaces:** `open_runtime(spec: RuntimeSpec, *, artifacts: RuntimeArtifactReceipt | None, repo: Path, cache: Path, scratch: Path, install: bool, deadline: float) -> RuntimeLease` is async; clean startup requires a verified artifact receipt. `RuntimeLease` exposes `identity`, bounded trusted `activation_env: dict[str, str]`, `owner_id`, and async `close(deadline: float) -> RuntimeCleanupReport`. Async `seal_runtime(lease: RuntimeLease, *, manifest_digest: str, source_digest: str, deadline: float) -> RuntimeArtifactReceipt` cleanly stops the installer guest and hashes its closed state before publication. The cleanup report records owned resource absence; incomplete cleanup cannot report success. The lease lives for the consumer job, not merely the initial check subprocess.

- [ ] Write red tests that a normal performer shell uses the same runtime through ordinary Docker/Compose commands after activation. Verify exact source bind paths, host reachability, cache write denial, isolated listeners and fresh keys; an empty/mismatched runtime must refuse dispatch.
- [ ] Implement the already proven Task 1 mechanism as the optional supervisor and lifecycle adapter. Guest preparation runs only during installation; clean startup uses sealed artifacts and fresh overlay. Package pinned platform tools without consumer recipes. Preserve ordinary outer tool execution.
- [ ] Wire startup/cleanup into both the real installer/verifier path and `prepare_environment` / `close_environment`. Keep runtime ready through downstream tool use; close on success, failure, cancellation and process shutdown. No global Docker context or environment mutation.
- [ ] Run `.venv/bin/pytest -q agent/performer/tests/test_353_environment_runtime.py` and `COORDINARE_RUNTIME_INTEGRATION=1 .venv/bin/pytest -q -m integration tests/integration/test_353_runtime_consumer.py`. Expected: real downstream command succeeds using the proved runtime; failed teardown, inaccessible service, stale artifact, wrong source and unknown identity hold. Rehash immutable cache/guest artifacts.
- [ ] Exercise Kubernetes non-root/read-only-root constraints before claiming that runtime configuration supported there. If not proven, refuse that optional configuration explicitly; retain existing adapter regressions. Commit only after the claimed configurations pass.

### Task 4: Review source-defined obligations before command generation

**Files:** Create `shared/environment/scope.py`, `src/coordinare/services/environment_scope.py`; modify `environment_discovery.py`, `environment_discovery_prompts.py`, `environment_response_schema.py`, `environment_inspection.py`, `environment_findings.py` and `environment_repair.py`; test `tests/unit/test_353_scope_contract.py` and `test_353_scope_pipeline.py`.

**Interfaces:** `ScopeCriterion` contains daemon ID, source citations, source-backed applicability and observable usability. `ScopeContract` binds the criteria to commit, full inventory digest and all inspected hashes, and provides `digest()`. Async `discover_scope(*, paths: Sequence[str], fetch: Fetch, chat: ChatJson, commit: str, limits: DiscoveryLimits, budget: AttemptBudget) -> ScopeContract` reuses existing generic fetch/chat aliases and fresh independent review, consuming the passed budget rather than constructing one. Wire it into existing `discover_and_review`, without changing the public caller's ownership of that budget.

- [ ] Write red source-first tests for unfamiliar nested entry points and service requirements. A CLI-only candidate disclaimer, image limitation or omitted nested area cannot exclude source obligations. Unknown applicability, source conflict or unresolved finding holds.
- [ ] Implement author then fresh reviewer without the executable candidate or generator conversation. Daemon assigns stable IDs before review; union independently read evidence and retain complete findings and identity. No file/language catalog is added.
- [ ] Assert schema/coverage corrections, navigation and all calls consume the existing budget/journal. Newly read sources or obligations reopen review before execution; history cannot silently forget earlier requirements.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_scope_contract.py tests/unit/test_353_scope_pipeline.py` plus existing review-evidence/findings regressions. Expected: admitted contracts are source-bound and no blocker is dropped. Commit.

### Task 5: Construct and correct one complete executable manifest

**Files:** Modify `shared/environment/models.py`, `src/coordinare/services/environment_discovery.py`, `environment_discovery_prompts.py`, `environment_response_schema.py`, `environment_proposal_patch.py`, `environment_obligations.py`; test `tests/unit/test_353_scope_mapping.py` and existing `test_353_proposal_patch.py` / `test_353_function_transport.py`.

**Interfaces:** Add `scope_contract_digest` and criterion-to-execution mapping to the versioned canonical manifest. `validate_scope_mapping(scope: ScopeContract, manifest: EnvironmentManifest) -> None` rejects missing/forged IDs, dropped applicability and required-to-optional links. One constructor response receives admitted scope and actual runtime capabilities. The final semantic reviewer still judges whether commands exercise the criteria.

- [ ] Reproduce the exact latest Elm required Rails criterion linked to optional `rails-container-runtime-check`; also test missing verdict and clean checks depending on install-only commands. Assert admission rejects with bounded actionable paths/codes and never executes project commands.
- [ ] Implement source-owned requiredness and complete mapping. Reject invalid model output; do not auto-promote an optional check or fabricate commands. Preserve complete-declaration correction and existing 16-change patch limit. Expose constraints through both native-function and strict-wire schemas.
- [ ] Keep final fresh whole-source omissions and semantic coverage review. A version check cannot automatically satisfy operational service use; resolve findings independently. New source evidence changes identity and requires a correctly bound final assessment.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_scope_mapping.py tests/unit/test_353_proposal_patch.py tests/unit/test_353_function_transport.py` with existing structured-wire and obligation suites. Replay saved bad responses as diagnostics only; no synthetic approval or operator-edited manifest counts as acceptance. Commit.

### Task 6: Bind every acceptance/consumer seam and prove restart safety

**Files:** Modify `shared/environment/models.py`, `src/coordinare/services/environment_acceptance.py`, `environment_repair.py`, `environment_verification.py`, `env_cache.py`, `environment_capabilities.py`, `graph/nodes/dispatch_performer.py`, `agent/performer/src/performer/environment_contract.py`, `src/coordinare/services/kubernetes_verifier.py`; test `tests/unit/test_353_scope_runtime_acceptance.py` plus existing runner, provenance and performer contract tests.

**Interfaces:** Existing `execution_report`, `saved_execution_accepted`, `accepted_manifest` and dispatch gates validate the new manifest/scope/runtime versions and identities. Reuse the full binary execution-source provider/transport; inspected text is not sufficient execution source. Reports retain every required result and consistent exit code.

- [ ] Write red tests for missing/failed/unrun/unknown checks or service actions, failed stop, wrong scope/runtime/image/platform/source and missing closure. Old accepted records lacking new scope/runtime policy cannot become ready through defaults.
- [ ] Test roundtrip restart at scope review, runtime setup and project execution: same original deadline/counters; cleanup uncertainty holds. After execution, all existing required commands, including unrun ones, remain frozen across repairs.
- [ ] Implement bindings/migration without unrelated refactors. Retain full-source identity, executable bits and admitted symlinks through guest exports. All model/runtime credential values use existing reference and redaction policy.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_scope_runtime_acceptance.py agent/performer/tests/test_351_environment_contract.py` and the relevant existing provenance/repair/runner suites. Expected: every negative holds and complete synthetic installer/consumer identity roundtrip passes. Commit, preflight and obtain independent adversarial review before live Elm.

### Task 7: Prove Elm, then finish the requested repository validation and shipping

**Files:** Use `scripts/validate_environment_repositories.py`; update `validation-results.md`, `validation.md` and `tasks.md`. Private transcripts/manifests remain under `.agents/state/353-org-validation/`.

- [ ] Verify runtime capacity and immutable image/code identity. Start one fresh explicitly labeled Elm profile with the original pinned source/model/effort/deadlines/caps. Retain old failed attempts and invalidated acceptance; changing runtime identity is not a pure algorithm comparison.
- [ ] Obtain source-first and final independent coverage review. Check the source audit's documented environment areas, including services, nested apps, validators, architecture and ordinary activation. Source-backed environment-usability checks may be smaller than full application suites, but existing required checks cannot be dropped. No broad test matrix starts yet.
- [ ] Execute the exact accepted manifest's installation and every required installer/clean check and service lifecycle. Record all exit codes, same digest identities, actual read-only cache and no egress. Verify unchanged pinned snapshot, artifact hashes and complete owned cleanup.
- [ ] Report each stage separately. A capability hold or fixture success is not Elm completion. If the real workload exceeds resource/time limits, report the measured blocker; do not expand limits automatically or call partial coverage success.
- [ ] Only after genuine Elm readiness, rerun the small sample/current head and requested remaining pinned repositories. Audit successful coverage judgments and report held/not-started cases explicitly.
- [ ] Run final preflight/quality guard, full CI and full-diff adversarial review on the final head. Use ship-issue/merge-pr only when SC-001 through SC-006 and existing gates actually pass. Verify merge and issue closure; unwatch before user handoff.

## Stop rules and approval handoff

The runtime integration gate is deliberately first. If it fails, do not spend another
opaque hour generating a manifest for a consumer that still cannot supply services.
If scope/mapping replay fails, fix that deterministic regression before live Elm.
If real Elm fails after those gates, report the exact first failed stage and command
with remaining budgets; do not restart automatically to conceal exhausted attempts.

This plan was self-reviewed for source coverage, ordinary consumer equivalence,
artifact portability, identity, resource limits, no-waiver acceptance, cleanup and
legacy/restart behavior. The user approved the presented designs and plan with
"Yes". Execution is native with independent final review. Task 1 starts with a
diagnostic runtime integration proof; its result cannot substitute for Elm acceptance.
