# Nare Bootstrap Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all bootstrap conversation loops with nare, preserve Coordinare's
trust and execution rules, and delete the retired loops after final acceptance.

**Architecture:** A narrow typed runtime adapter owns nare sessions and transports.
Coordinare supplies immutable evidence, domain tools, validation callbacks and
outer-budget accounting. Scope, authoring, review and repair remain independent
roles under existing orchestration.

**Tech Stack:** Python >=3.12, asyncio, pydantic v2, nare 2026.11.104 candidate pin,
existing Coordinare config/transcript/attempt machinery and pytest.

**Spec:** [spec.md](spec.md), [approved design](nare-bootstrap-design.md).

## Global Constraints

- No permanent selectable legacy chat runtime and no automatic fallback to it.
- Nare does not gain arbitrary shell, filesystem write, network, Docker, Git, or publication capabilities.
- No new daemon-side repository clone is introduced.
- Never reuse an author's conversation as the independent reviewer's context.
- The outer Coordinare attempt deadline and repair rules remain authoritative.
- Legacy sessions must never be reinterpreted as nare evidence.
- No private source/transcripts committed; preserve experiment artifacts.
- No timeout, budget exhaustion, missing receipt, or cleanup uncertainty becomes readiness.
- Preserve delta-to-full domain repair, trial restrictions and separate revision budgets.
- Run speckit.analyze before implementation; no critical findings may remain.

## Baseline and dependency decisions

Read-back 2026-11-14: PR #355 OPEN, head `353-sharded-discovery` at
`31d54cd23d0da39e727ae0855b0ac9424cbea09b`, base `353-practical-env-discovery`.
The frozen validation checkout has this same head. The documentation spike is an
older branch and must not be used as the production baseline.

At execution start, re-read PR state and create/use an isolated implementation
branch `376-nare-bootstrap` from fresh main, as the constitution requires. Bring
in the exact required #355 head through an explicit local dependency merge if it
is not already on main, resolve conflicts and verify the affected baseline before
feature edits. This preserves the prerequisite implementation without using the
old spike as a base. Keep the frozen validation checkout untouched. Until #355
merges, use a temporary stacked review target to isolate this feature's diff;
retarget and rebase onto main before final merge. Do not overwrite or merge #355
on GitHub as part of this task. Carry these spec artifacts to the implementation
branch; record the resulting baseline SHA and test result.
Nare local tag `2026.11.104` points to tested SHA
`21fd8e7fc010137dd948aef717f16eabb7f359f9`. Task 1 verifies the published artifact
matches and resolves in normal builds before adding an exact pin. If release
provenance/distribution fails, report the concrete packaging blocker; do not
silently choose another version or use local source paths. Its declared deps are
anthropic>=1.6, mcp>=2,<3, pillow>=13,<14; check the existing lock and image builds.

Initial transport matrix: configured OpenAI native or OpenAI-compatible API through
nare OpenAI transport; configured Anthropic native API through nare Anthropic
transport. Preserve resolved model and parameters. CLI-only and unsupported
non-single orchestration combinations produce a named preflight refusal, never
silently map to another endpoint or single-mode behavior. Test and document the
actual bootstrap selection path before wiring it; other performer backends remain
unchanged.

## File structure and shared interfaces

New package `src/coordinare/services/agent_runtime/`:
- `models.py`: immutable request/identity/result types and protocols.
- `nare_runtime.py`: only nare run/session/transport integration.
- `evidence.py`: bounded immutable evidence paging and delivery receipts.
- `checkpoint.py`: atomic checkpoint/receipt storage and identity verification.
- `accounting.py`: budgeted transport wrapper and event/usage bridge.
- `configuration.py`: bind resolved existing model config to supported transport.

New bootstrap integration `src/coordinare/services/environment_agent.py` supplies
phase-specific tools and validators. Existing environment_scope, environment_discovery,
environment_shard_scope, environment_repair, environment_admission and env_cache
retain domain behavior. Avoid unrelated reformatting of these large modules.

Use `JsonObject = dict[str, Any]` only at JSON/provider boundaries. Define:
- `AgentIdentity`: attempt_id, phase, role, source_commit, domain_digests,
  policy_digest, prompt_digest, schema_digest, model_config_digest.
- `AgentRequest`: identity, instructions, task, answer_schema, evidence_catalog,
  allowed_tools, check, budget, checkpoint_key.
- `AgentCheck = Callable[[JsonObject], Awaitable[list[AdmissionIssue]]]`;
  `AdmissionIssue` has code, location, message, guidance. Adapt sync validators
  explicitly; do not change their validation semantics.
- `AgentResult`: status (`accepted|held|cancelled|error`), answer, hold_code,
  usage, checkpoint_key, evidence_receipt. Accepted requires check completion.
- `AgentRuntime.run(request: AgentRequest) -> AgentResult` (async protocol).
- `EvidenceStore.read(request: EvidenceRead) -> EvidencePageBatch` (async);
  batch contains whole pages plus next_cursor and remaining_required.
- `CheckpointStore.save(checkpoint: AgentCheckpoint) -> str` and
  `load_verified(key: str, expected: AgentIdentity) -> AgentCheckpoint` (async).
- `BudgetBridge` wraps the existing AttemptBudget; before every physical provider
  request call reserve_call + flush, before domain reads/probes use their existing
  reservation functions. Schema corrections are charged once via the existing
  domain authority, not again by both callback and event handler.

These are internal Python contracts, not new performer wire fields. Extend state
serialization only with a versioned checkpoint reference/identity receipt and
migration tests; no raw nare objects in persisted daemon state.

## Performance and cache acceptance budgets

Implementation choices for review: bound paging to100ms median for a4MiB synthetic
catalog on the designated CI runner, peak incremental paging allocations to20MiB,
and cold-start median regression to <=10% plus100ms versus the recorded baseline.
Measure offline with repeated samples, no network/provider latency in these budgets;
add the benchmark to the existing benchmark lane. Existing configured phase/attempt
wall deadlines and model/read/correction limits remain hard domain bounds. Never
start another request after exhaustion; provider token usage already in flight may
only be reported, not retroactively prevented. Record that overshoot explicitly.
Cache only immutable evidence by pinned content identity; invalidate checkpoint
eligibility on identity changes. No new inference-cache assumptions.

## Review Focus

1. Unicode/escaped evidence exceeds a serialized cap: exact pages must survive (Task 2).
2. Crash after external execution but before receipt: never replay uncertain effects (Task 3/6).
3. Disconnect after partial streamed output: count/hold correctly, never duplicate tools (Task 4).
4. Resume under changed scope/model/policy: stale session cannot inherit evidence (Task 3).
5. Old daemon state survives deployment: reconcile external work and hold/restart safely (Task 7).

## Task 1: Runtime and normal dependency binding

**Files:** create agent_runtime/models.py, configuration.py, nare_runtime.py;
modify pyproject.toml, uv.lock, Dockerfile.daemon only where dependency installation
requires it; tests/unit/test_376_agent_runtime.py and test_376_agent_configuration.py.
**Consumes:** resolved existing config, injected fake transport/check.
**Produces:** AgentRuntime.run, request/result contracts and pinned dependency.

- [ ] Write failing tests `test_accepted_requires_domain_check`,
  `test_failed_check_never_returns_accepted`, `test_unsupported_endpoint_fails_before_request`,
  `test_selected_model_parameters_are_preserved`; assert no provider call on preflight refusal.
- [ ] Run `.venv/bin/pytest tests/unit/test_376_agent_runtime.py tests/unit/test_376_agent_configuration.py -q`; confirm intended failures.
- [ ] Verify/pin nare2026.11.104, resolve lock normally; implement typed adapter using
  nare.run/check/native tools. Carry exact final schema, never legacy JSON-tool unions.
  Bind normal config without globals, private paths or changed model selection.
- [ ] Re-run tests; install/build wheel and daemon image through existing build targets,
  confirming imports/version without PYTHONPATH. Document tested transport matrix.
- [ ] Commit `feat(376): add configured nare agent runtime`.

## Task 2: Immutable evidence tools

**Files:** create agent_runtime/evidence.py; tests/unit/test_376_agent_evidence.py.
**Consumes:** pinned source fetch and domain-required evidence set.
**Produces:** EvidenceStore.read, stable evidence catalog and delivery receipts.

- [ ] Write failing tests `test_whole_pages_survive_actual_transport_boundary`,
  `test_unicode_and_escaping_respect_serialized_cap`, `test_requested_but_unreturned_page_is_unread`,
  `test_changed_source_hash_refused`, `test_elision_does_not_erase_required_coverage`.
  Assert returned envelope length <= nare.tools.MAX_TOOL_OUTPUT, exact page hashes,
  and zero admission while required evidence remains unread.
- [ ] Run `.venv/bin/pytest tests/unit/test_376_agent_evidence.py -q` and confirm failures.
- [ ] Implement stable paging with actual serialization before delivery, bounded
  cursors, explicit continuation and source-hash validation. Oversize page repaginates
  before being marked read. Preserve existing read budgets and scoped review semantics.
- [ ] Re-run transport-boundary tests through real nare with a scripted transport;
  test compaction and reread of old evidence without double-charging coverage.
- [ ] Commit `feat(376): deliver immutable bootstrap evidence through native tools`.

## Task 3: Checkpoints, resume and external-effect receipts

**Files:** create agent_runtime/checkpoint.py; modify environment_repair.py and
state_store.py for versioned references; tests/unit/test_376_agent_checkpoint.py.
**Consumes:** AgentIdentity, delivery receipts, existing repair/probe journal.
**Produces:** atomic verified session persistence and explicit resume/hold decision.

- [ ] Write failing tests for checkpoint tampering, changed source/scope/capability/
  policy/model identities, preserved tokens/turns/corrections, partial atomic writes,
  and pending probe/trial with missing completion receipt. Assert no tool replay.
- [ ] Run `.venv/bin/pytest tests/unit/test_376_agent_checkpoint.py -q`; confirm failures.
- [ ] Implement envelope version1, hash bindings and comparison against trusted
  current domain state. Persist reservations before external effects and receipts
  after completion. Delegate reconciliation to existing pending-probe/cleanup logic.
  Never infer completed effects from transcript prose.
- [ ] Re-run tests, including interrupted serialization and a real nare scripted
  save/load continuation. Preserve accounting and check provenance from pinned source.
- [ ] Commit `feat(376): persist verified agent checkpoints and receipts`.

## Task 4: Budgets, telemetry and terminal semantics

**Files:** create agent_runtime/accounting.py; modify environment_attempt.py,
conductor_calls.py and bootstrap_status.py only at adapter boundaries;
tests/unit/test_376_agent_accounting.py.
**Consumes:** AttemptBudget, existing transcript/activity/usage sinks.
**Produces:** BudgetBridge and replay-safe event sink, typed timeout/hold outcomes.

- [ ] Write failing tests for physical retry reservation, cancellation during stream,
  timeout with internal working session, duplicate replayed usage, missing usage,
  and schema correction counted once. Assert unchanged outer deadline and no extra call
  after exhaustion; preserve separate scope/executable/execution counters.
- [ ] Run `.venv/bin/pytest tests/unit/test_376_agent_accounting.py -q`; confirm failures.
- [ ] Wrap nare transport calls/streams to reserve/flush before inference; relay
  per-turn events with stable dedupe keys. Bridge domain corrections directly to
  the shared budget. Clamp nare allowance to remaining domain allowance; map hold codes.
  Close streams/flush state on cancellation. Do not reset global usage on resume.
- [ ] Re-run tests and existing transcript/accounting regressions; assert logs omit
  secrets/source bodies and progress reaches existing bootstrap activity surfaces.
- [ ] Commit `feat(376): preserve bootstrap budgets and telemetry under nare`.

## Task 5: Production reviewer integration

**Files:** create environment_agent.py; modify environment_discovery.py,
environment_discovery_prompts.py; tests/unit/test_376_environment_review.py.
**Consumes:** Tasks1-4 plus existing _url_checked_assessment and reviewer preparation.
**Produces:** `run_environment_phase(request: EnvironmentPhaseRequest) -> PhaseResult`
(async), adapting domain envelope, inputs, role and budget to AgentRuntime.

- [ ] Write failing tests that keep exact assessment constraints, forbid author-session
  reuse, reject early/incomplete evidence and accept/revise/reject only after domain
  validation. Assert scoped review does not expand its requirement set to all files.
- [ ] Run `.venv/bin/pytest tests/unit/test_376_environment_review.py -q`; confirm failures.
- [ ] Extract transport-independent prompt builders and bind reviewer to native
  evidence tools/check callback. Preserve input digest and ledger admission.
  Do not copy spike string replacement or assume accept means bootstrap ready.
- [ ] Re-run tests and one bounded live saved-packet replay with normal dependency/config.
  Preserve report, verdict, usage, evidence and checkpoint provenance separately.
- [ ] Commit `feat(376): run bootstrap reviewers on nare`.

## Task 6: Scope, authoring, sharded roles and repair

**Files:** modify environment_agent.py, environment_scope.py,
environment_shard_scope.py, environment_discovery.py, environment_repair.py,
environment_discovery_prompts.py; tests/unit/test_376_bootstrap_phases.py;
retain existing test_353_* domain suites.
**Consumes:** run_environment_phase and unchanged domain admission/patch/probe/trial functions.
**Produces:** all bootstrap roles on nare with independent sessions and domain scheduling.

- [ ] Write failing parity tests for initial/sharded scope, global/group authoring,
  declaration correction, failed-execution patch repair, scoped review and full-repair
  escalation. Assert obligations/frozen records survive, correct role identities,
  no duplicate probe/trial after restart, and trials never supply runtime artifacts.
- [ ] Run `.venv/bin/pytest tests/unit/test_376_bootstrap_phases.py -q`; confirm failures.
- [ ] Replace phase loop bodies with the common runtime; expose approved probe/trial
  actions as native domain tools. Preserve group scheduling, prerequisite checks,
  journal semantics, static guards and existing domain escalation. Route full repair
  through nare too. Do not introduce runtime fallback flags.
- [ ] Re-run phase and affected test_353 scope/repair/obligation/pending-probe suites;
  scripted bad proposals must receive the same named refusals before execution.
- [ ] Commit `feat(376): migrate bootstrap authoring and repair to nare`.

## Task 7: Daemon wiring and pre-nare state migration

**Files:** modify env_cache.py, state_store.py, bootstrap_status.py and relevant
bootstrap configuration model; tests/integration/test_376_bootstrap_lifecycle.py.
**Consumes:** migrated domain phase functions and verified checkpoint store.
**Produces:** normal daemon path with one runtime and explicit legacy-state policy.

- [ ] Write failing integration tests loading old states: ready verified manifests
  remain valid under existing rules; incomplete legacy conversations cannot become
  nare checkpoints; uncertain external work holds; confirmed-clean interrupted work
  begins a fresh native phase within the same remaining attempt budget.
- [ ] Run `.venv/bin/pytest tests/integration/test_376_bootstrap_lifecycle.py -q`;
  confirm failures, including service-entrypoint-only stubs cannot satisfy daemon tests.
- [ ] Wire config/runtime into actual env-cache entrypoints. Preserve ready artifacts
  and domain ledgers; retain rejected legacy transcripts as diagnostics. Reset only
  incompatible conversation state after reconciliation, never budgets or obligations.
- [ ] Re-run restart/cancel/status/cleanup tests with one active bootstrap per identity.
  Verify terminal hold states and actionable operator diagnostics.
- [ ] Commit `feat(376): wire resumable nare bootstrap into daemon lifecycle`.

## Task 8: End-to-end acceptance, deletion and follow-up inventory

**Files:** tests/integration/test_376_bootstrap_acceptance.py,
tests/unit/test_376_no_legacy_bootstrap.py;
create tests/benchmarks/test_376_agent_runtime.py; delete retired loop-only helpers in the
three phase modules; modify docs/operators/reviewed-environment-manifest.md and
this spec's validation.md; create followups.md in this spec directory.
**Consumes:** complete production path from Tasks1-7.
**Produces:** final single-runtime diff and independently auditable acceptance evidence.

- [ ] Write failing architecture guard exercising normal bootstrap with the legacy
  chat transport made unavailable; all phases must complete through nare. Add
  deterministic source/provenance tampering and clean-consumer refusal cases.
- [ ] Run targeted tests; inspect all caller references before deleting anything.
- [ ] Run the real authorized installer/repair/consumer path through production entrypoints
  on pinned Dettmore and an eligible non-Rails project, preserving existing scope,
  image identity, approvals, network/isolation and cleanup rules. Do not run preflight
  concurrently with e2e. Record held runs honestly; request concrete extra budgets
  when required rather than inheriting unlimited experimental permission.
- [ ] Once replacement acceptance passes, delete obsolete bootstrap model loops,
  manual JSON-tool instructions, loop-only context trimming and correction helpers.
  Keep shared helpers used by unrelated callers; no runtime selection fallback.
- [ ] Implement and run the offline paging/memory/startup benchmarks against the
  numeric budgets above, record baseline and final measurements, wire the existing
  benchmark lane. Exercise arbitrary tool names/nested source layouts.
- [ ] Re-run targeted acceptance after deletion, then `make lint`, `make typecheck`,
  `make test-all`, `make ci` as repository policy requires. Validate clean consumer,
  provenance and absence of leaked owned resources on the final head.
- [ ] Write measured validation results and supported configuration documentation.
  Survey service inference first, configuration assistant and observer second;
  followups.md records duplicate logic, benefit, risk and acceptance per candidate.
  No later-task product migrations in this branch.
- [ ] Commit `refactor(376): remove legacy bootstrap loops after nare acceptance`.
  Obtain adversarial full-diff review, address findings, reverify changed claims,
  then use normal PR checks/review/merge workflow. No automatic merge authorization
  is inferred merely from approval of this plan.

## Task ordering and self-review

Tasks1-4 establish shared contracts; Task5 then Task6 migrate roles; Task7 wires
lifecycle; Task8 validates and deletes. Evidence/persistence tests may be authored
independently after Task1, but implementation interfaces are coupled. Native
execution is recommended to preserve that context, with fresh final-diff review.

Coverage: FR01 Task1/5/6; FR02 Task2/5/6; FR03 Task2; FR04 Task3/7;
FR05 Task3/4/6; FR06 Task1/7; FR07 Task4/7; FR08 Task3/6;
FR09 Task7/8; FR10 Task8; FR11 Task8. Review-focus cases have named tests above.
All new interfaces are defined in the shared contract or their producing task.
No checkpoint or runtime result changes performer wire payloads.
