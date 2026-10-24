# Bounded source-scope revisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove redundant complete-scope rewrites after coverage objections, then demonstrate genuine pinned Elm bootstrap success.

**Architecture:** Keep the complete initial declaration and canonical accepted contracts. A strict revision envelope proposes bounded changes against an exact retained digest; the daemon constructs the complete next definition atomically. A fresh independent reviewer judges the entire result before executable construction can proceed.

**Tech Stack:** Existing Python 3.12+, Pydantic v2, asyncio, pytest; no new dependencies.

**Spec:** [Approved scope-revision amendment](scope-revision-amendment.md).

**Status:** Written amendment approved by the user on 2026-10-19. Implementation plan approved by the user on 2026-10-19. Preserve the previously selected **Native / inline** execution method.

## Global Constraints

- Language, framework and library agnostic core; no project recipes or implicit constitution amendment.
- Preserve required criteria exactly, including IDs and rationale; no delete or rename operation.
- Only existing `unknown` or `not_applicable` criteria may be reassessed, once per revision.
- Maximum 16 combined additions/reassessments; retain 128 criteria and 256 KiB definition bounds.
- Retain inspected input hashes, complete inventory identity, pinned commit, original deadlines and counters. Listings do not count as inspected file content.
- Fresh independent whole-source review must close every finding; unknown applicability remains a hold. No reviewer can waive missing, failed, unrun or unknown executable checks.
- Elm source `bc224a78db6f669d56863599d653f14e7605de0b`; `spark/glm-5.3-flash`, low effort, canonical `json_object`, 32768 output tokens; 3600-second aggregate and original 2 schema / 2 coverage / 2 execution repair caps.
- Preserve proven private runtime policy, resource limits, image/source attestation, service lifecycle and confirmed teardown. No model changes, budget increases or privilege bypasses.
- Keep failed profiles and raw replies. No other repository acceptance runs or merge before Elm succeeds.
- Whole suite runs in CI; focused tests run locally. Never bypass failed/skipped required Build Success, including its actual workflow dependencies.

## Review Focus

1. A valid early change followed by an invalid late change must leave the complete base and saved responses untouched — Task 1 atomic-failure test.
2. Empty change arrays may defend a finding, but cannot close it without independent evidence review — Task 3 defense test.
3. Reassessing a nonrequired criterion changes its derived ID; responses naming the old ID must hold without aliasing — Tasks 1 and 3 stale-target tests.
4. A crash between accepted author output and reviewer dispatch must resume review, without applying the revision twice or renewing time — Task 4 checkpoint test.
5. Source-scope `F0001` must appear in held diagnostics when executable-manifest findings are empty, without exposing rationale/source text — Task 4 summary test.

## Files and responsibilities

Paths below are repository-relative. Shared package prefix: `packages/service_inference/src/coordinare_service_inference/environment/`.

| File | Responsibility |
| --- | --- |
| New shared `scope_revision.py` | Strict revision models and pure bounded atomic application |
| `src/coordinare/services/environment_scope.py` | Phase selection, inspected evidence, response validation, fresh whole review |
| `src/coordinare/services/environment_response_schema.py`, `environment_function_wire.py` | Equivalent canonical/native revision declarations and decoding |
| `src/coordinare/services/environment_admission.py`, `environment_discovery_prompts.py` | Trusted field locations and phase-specific repair guidance |
| `src/coordinare/services/environment_attempt.py`, `environment_repair.py` | Explicit protocol/phase/base checkpoints, migration and safe diagnostics |
| New `tests/unit/test_353_scope_revision.py` | Pure revision rules and boundary failures |
| Existing `test_353_scope_contract.py`, `test_353_scope_pipeline.py`, `test_353_structured_wire.py` | End-to-end phases, persistence, transport parity |
| `tests/unit/scope_chat_fixture.py` | Test-only scripted revision support where existing pipeline tests require it |

Canonical `scope.py`, manifest mapping, executor and runtime admission formats remain unchanged. Do not refactor unrelated orchestration.

## Task 1: Strict revision models and atomic application

**Files:** Create shared `scope_revision.py` and `tests/unit/test_353_scope_revision.py`.

**Interfaces:** Import existing `Digest`, `FindingEvidence`, `FindingResponse`, `ScopeCriterionProposal`, `ScopeDefinition`, `ScopeCriterion`, `criterion_id`. Produce:

- `ScopeReassessment(FindingEvidence)`: required `criterion_id: str` with `^S[0-9a-f]{16}$`, `criterion: ScopeCriterionProposal`.
- `ScopeRevision(FindingEvidence)`: required `base_digest: Digest`, `add: list[ScopeCriterionProposal]`, `reassess: list[ScopeReassessment]`; each list max16 and combined length max16.
- `ScopeRevisionEnvelope(FindingEvidence)`: required `scope_revision: ScopeRevision`, `finding_responses: list[FindingResponse]` max32. No extra fields/defaulted envelope fields.
- `apply_scope_revision(base: ScopeDefinition, revision: ScopeRevision, *, inspected_inputs: dict[str, str]) -> ScopeDefinition`. Pure; returns a newly validated complete definition or raises `PydanticCustomError` with trusted codes/locations; never mutates either argument.

- [x] Write parameterized tests with exact assertions: add one obligation => `len(result.criteria) == len(base.criteria)+1`, retained required objects equal their original `model_dump()`, `result.source_commit == base.source_commit`, `result.inventory_digest == base.inventory_digest`, and old input hashes remain equal. Unknown and not-applicable reassessments receive `criterion_id(replacement)`; unaffected order is retained and additions append.
- [x] Add rejection cases: stale digest, unknown/repeated target, required target, duplicate additions (including rationale-only differences), collision with retained/replacement IDs, 17 combined changes, 129 final criteria, >256 KiB final definition, missing/extra fields, unread citations, changed/missing retained hashes. For every case assert `base.model_dump() == before`; include valid first addition followed by invalid last reassessment. Exactly16 changes and empty arrays must pass when final bounds permit.
- [x] Run `.venv/bin/pytest tests/unit/test_353_scope_revision.py -q`; expect RED for missing revision API.
- [x] Implement models and pure merge. Preserve required full objects, recompute replacement/addition IDs using existing identity rules, keep source/inventory/requested-scope fields, and validate against the complete inspected-input map. Use `ScopeDefinition` validation for canonical final bounds.
- [x] Run the same command; expect all PASS. Commit only this task's two files as `feat: add bounded atomic source-scope revisions`.

## Task 2: Phase-specific transport and correction contract

**Files:** Modify response schema, function wire, admission and discovery prompts above; extend `test_353_structured_wire.py` and `test_353_scope_revision.py`.

**Interfaces:** Existing `response_schema(phase: str, *, allow_patch: bool = False) -> dict[str, Any]` gains `scope_revision`. Existing `functions(schema)` exposes `submit_environment_scope_revision`; `decode_function(raw, schema) -> str` reconstructs exactly the same canonical revision envelope. Existing `correction_instruction(envelope: str) -> str` gains explicit revision guidance. No provider-specific acceptance logic.

- [x] Write tests asserting canonical schema requires exactly `scope_revision` and `finding_responses`, nested required fields and `additionalProperties == False`; add/reassess arrays advertise max16 and local model validation enforces combined16. Initial `scope` schema remains unchanged.
- [x] Write native round-trip assertions: decoded envelope equals the canonical submitted object including `finding_responses`; wrong function name, duplicate keys, extra metadata and a complete `scope` submission in revision phase are rejected. Both transports feed the same local validator and produce equivalent held codes.
- [x] Add diagnostic assertions: a bad revision citation identifies `['scope_revision','add',0,'sources',0]`; an arbitrary extra key/value is redacted, never echoed. Correction guidance says `scope_revision`, exact retained base digest, no full declaration/no commands; schema allowance is unchanged.
- [x] Run `.venv/bin/pytest tests/unit/test_353_structured_wire.py tests/unit/test_353_scope_revision.py -q`; expect new cases RED.
- [x] Implement schema dispatch, native flatten/reconstruction alongside existing scope/manifest handling, trusted field allowlist and revision-specific correction guidance. Do not relax duplicate-key or strict object validation.
- [x] Repeat command; expect all PASS. Commit listed task files as `feat: expose strict scope revision transport`.

## Task 3: Revision authoring followed by complete independent review

**Files:** Modify `environment_scope.py`, `environment_attempt.py`; extend `test_353_scope_contract.py`; update only affected test-only scripted fixture cases.

**Interfaces:** Extend `AttemptBudget.expected_envelope` literal with `scope_revision`. Add `_validate_revision(data: dict[str, Any], *, author: SourceInspection, commit: str, budget: AttemptBudget) -> ScopeDefinition`, consuming Task 1 API. Keep existing `_phase`, `_messages`, `_validate_author`, `preserve_scope` signatures. Factor `_validate_responses(responses: list[FindingResponse], *, ledger: FindingLedger, inputs: dict[str, str], definition: ScopeDefinition) -> list[dict[str, Any]]` for identical initial/revision finding checks.

- [x] Convert the existing nested-omission scenario to initial full scope, independent `F0001`, small revision, fresh independent resolution. Assert original required object exact, final two required criteria, `coverage_revisions == 1`, two review records, no open findings, and reviewer messages contain the entire canonical new definition but no author conversation.
- [x] Add source-backed unknown reassessment and empty-delta defense scenarios. Assert empty delta alone leaves `F0001` open until a fresh reviewer resolves it; unknown still holds on acceptance. Test newly discovered omission outside the delta creates another blocking finding.
- [x] Add missing/duplicate/unknown finding responses, stale replaced affected ID, unread response citations, complete-scope revision reply, stale base and malformed late-change cases. Assert saved responses/base unchanged on failure, no commands dispatched, original schema/coverage caps and absolute deadline still govern. Empty affected IDs are accepted as references pending daemon assignment; no IDs are fabricated.
- [x] Run `.venv/bin/pytest tests/unit/test_353_scope_contract.py tests/unit/test_353_scope_revision.py -q`; expect new cases RED.
- [x] Select initial versus revision envelope from retained protocol phase (Task 4 supplies persistence). Include retained definition digest in revision messages. Validate inspected citations and responses before publishing any change; run `preserve_scope` independently. Route `_phase` repair feedback to the exact exposed phase. Fresh reviewer always receives complete result plus retained ledger/responses/evidence. Keep every read/call charged to the original budget.
- [x] Repeat command; expect all PASS. Commit affected files as `feat: review complete scope after bounded revisions`.

## Task 4: Durable revision state and accurate held diagnostics

**Files:** Modify `environment_attempt.py`, `environment_repair.py`, `environment_scope.py`; extend `test_353_scope_pipeline.py` and `test_353_scope_revision.py`.

**Interfaces:** Add matching budget/journal fields `scope_author_protocol: Literal[2] = 2`, `scope_phase: Literal['initial','revision','review','accepted'] = 'initial'`, `scope_revision_base_digest: str | None = None` (journal validates Digest when present). Advance `RepairJournal.version` to7. Keep `journal_for`, `_budget`, `save_attempt`, `begin_attempt` public signatures. `AttemptBudget.summary()` keeps existing manifest summary fields and adds prefixed source fields `scope_finding_count`, `scope_open_finding_ids` (via existing trusted finding-summary logic).

- [x] Write crash/resume tests at reserved revision call, post-valid-revision/pre-review and post-review/pre-next-author checkpoints. Assert persisted protocol2/version7, exact base digest when phase=`revision`, canonical definition/responses/ledger equality, unchanged `expires_at` and nondecreasing calls/reads/repair counters. Post-application phase=`review` must not reapply or request another author revision. Before-review flush must precede the next chat invocation.
- [x] Test mismatched phase/base, missing protocol and altered evidence reject. Version6 in-progress source work holds `legacy_attempt_requires_restart`, preserving raw journal unchanged. Version6 with a valid accepted canonical scope may be normalized to version7/phase accepted only after all existing identity/ledger/readiness checks; assert contract digest and expiry/counters identical. Versions1–5 retain their existing explicit-restart policy. Exercise explicit restart/archive path with original failed consumption retained; no automatic new allowance.
- [x] Test summary with source `F0001` and empty manifest ledger: `scope_finding_count == 1`, `scope_open_finding_ids == ['F0001']`, manifest count remains0, actual stage/counters/remaining time agree with budget; serialized diagnostics exclude issue/rationale/source content. Persisted held diagnostics retain these fields.
- [x] Run `.venv/bin/pytest tests/unit/test_353_scope_pipeline.py tests/unit/test_353_scope_revision.py -q`; expect new cases RED.
- [x] Implement checkpoint state transitions: initial author -> review; review revise -> revision with current digest; valid revision -> review with base cleared; accepted review -> accepted. Flush consumed work and complete state before each external call. Restore by phase, never infer pending protocol from model output. Implement bounded safe migration and distinct source diagnostics.
- [x] Repeat command; expect all PASS. Commit listed files as `fix: preserve scope revision state and held findings`.

## Task 5: Verify the correction, publish, and rerun pinned Elm

**Files:** Existing focused tests and private validation harness; update this plan's execution evidence and PR body with actual outcomes. No project-specific production recipes.

**Interfaces:** Existing `.agents/state/353-run-focused-preflight.py`, ship-issue `preflight --check 353`, quality guard, native PR watcher and repository simulation runner. Preserve current production validation entry points and runtime policy.

- [ ] Run focused tests for revision, scope contract/pipeline/mapping/runtime acceptance and structured wire/output, then Ruff, strict mypy ratchet, debt ratchet and whitespace checks using the established focused preflight. Require every internal row PASS, not merely controller exit0.
- [ ] Replay retained chat011 duplicate-key and chat012 extra-field failures offline through current strict decoding: assert held, original candidate unchanged, no model/installation/approval. Add their minimized raw shapes to transport regressions; never synthesize production review acceptance.
- [ ] Self-review the diff against all six amendment acceptance scenarios; verify shared interfaces and budgets. Commit only owned changes/evidence, run the established preflight/quality guard before pushing, then watch PR354 through native T3. Require actual required gates including Build Success success; investigate failures, never waive or poll around them.
- [ ] Once required CI passes, launch one newly labeled pinned Elm attempt using current code/image attestation, approved model/low effort and original 3600-second aggregate/repair caps. Do not reuse an exhausted journal. Verify original pinned source hashes and use production source/coverage/execution flow; test fixtures and offline replies cannot supply live approvals.
- [ ] Record source review, manifest review, exact manifest digest, installer and clean-consumer results, all required check/service exit codes, consumed counters/deadline and independent owned-resource cleanup. Success requires every required executable check ran and passed with confirmed teardown. If held, retain exact stage/code/evidence and diagnose that failure; do not claim bootstrap success, widen scope, alter budgets or merge.

## Self-review and execution handoff

Spec coverage: protocol/bounds/atomicity -> Task1; wire/diagnostics -> Task2; whole fresh review/evidence/finding closure -> Task3; provenance/recovery/budgets/held reporting -> Task4; malformed replays/CI/genuine Elm -> Task5. All five Review Focus cases have explicit owning tests. Existing canonical scope/manifest/runtime interfaces stay unchanged; new shared signatures are defined before use. No additional runtime subsystem or model experiment is included.

Written spec and plan approvals are recorded; Native execution is in progress. Tasks1–4 are implemented with RED-to-GREEN tests. Task5 remains pending required CI and genuine Elm execution. The latest actual Elm run remains held before installation; this document is not validation evidence.


## Execution evidence (2026-10-19)

- Tasks1–4: strict atomic models, matching canonical/native declarations, complete independent review and journal7/protocol2 recovery implemented. Tests covered16-change bounds, required retention, invalid response references, independent closure and source diagnostics.
- Offline replay of the actual retained chat011/chat012 remains held; retained base unchanged, zero model/execution calls, no approval produced.
- Fresh GPT-6-Astra whole-branch review independently ran224focused tests and found one Important integration regression: new executable-phase evidence cleared the scope contract without updating its persisted phase. A production-checkpoint regression failed before the fix. Reopening now enters review before flushing; additional author/reviewer interruption tests preserve coverage consumption, source hashes and expiry.
- Local preflight initially rejected complexity-ratchet growth. Extracted the author-phase helper and named journal-version constants; the unchanged debt baseline now passes. No thresholds, suppressions or CI requirements changed.
- Current-head preflight, required CI, immutable-image proof and fresh Elm acceptance must still be recorded. Earlier synthetic runtime fixture025 is retained as runtime evidence, not Elm acceptance.


## User-directed probe timeout correction

On 2026-10-19 the user instructed: “Increase the timeout.” The private-service
probe batch allowance is now 1800 seconds (30 minutes), capped by the existing
attempt expiry. This supersedes the prior 120-second batch allowance for fresh
private guests. Outer-tool batches retain 120 seconds; individual command and
guest bounds, isolation, mandatory review/checks and the overall 3600-second Elm
attempt remain unchanged. New service/executor deadline regressions cover the
larger batch, shorter remaining attempts, explicit tighter/looser deadlines, and
eleven isolated 90-second observations sharing one absolute expiry. A fresh Elm
run follows successful preflight and required CI; the exhausted run is retained.


## Follow-up review diagnostics

The fresh `probe-batch30m-elm60m-low` run stopped after945.12seconds before
probes. Typed revision succeeded (nine added criteria, eight finding responses),
but the subsequent reviewer omitted decisions for all eight retained findings;
its correction added forbidden `decisions2`. The two schema slots were exhausted
with2654.965seconds remaining. The30-minute probe change was not exercised.

Complete retained-finding adjudication remains mandatory. Review input now lists
the exact daemon-owned required decision IDs separately; incomplete/duplicate
adjudication identifies `scope_assessment.decisions`, and repair feedback repeats
those trusted IDs. No decision, verdict, source citation or approval is filled in
by the daemon. Saved malformed replies remain rejected and ledgers unchanged.
Regression cases cover both one and eight retained findings. No budgets or
readiness conditions change in this correction.


### Executable-preflight follow-up: align the advertised mapping schema

The fresh `decision-feedback-elm60m-low` run on `46a31cc7` completed six
isolated read-only capability probes with confirmed cleanup. Independent source
review accepted 19 criteria; executable manifest review was never reached.
The run held after 1767.68 seconds with 1832.4 seconds remaining: a manifest
omitted `criterion_checks`, and its correction supplied malformed nested JSON.
The original two schema repairs were exhausted; no installer or clean consumer
ran. The user-directed probe allowance was exercised without a batch timeout.

Local acceptance already requires the exact required-criterion mapping, but the
provider schema advertised that field as optional. Bind that existing requirement
to each manifest request's reviewed scope: require the mapping and every required
criterion ID, with nonempty check lists and no invented criterion keys. Continue
using normal local admission, independent executable coverage review and every
required clean check. Never fill a mapping, salvage invalid JSON, alter budgets,
or change the source obligations. Unscoped legacy schemas and other phases keep
their existing contracts. Verify the real production request boundary plus both
structured transport representations, then preflight and CI before a fresh Elm
attempt. This aligns representation with the approved contract; it does not
establish that the model will comply or that Elm bootstraps successfully.


### Canonical transport follow-up: deliver the actual schema and safe guard feedback

The `mapping-schema-elm60m-low` run on `74587664` held after 1493.84 seconds,
with 2106.245 seconds remaining. Source review accepted 22 criteria and all six
outer-image observations completed. The initial source declaration repeated a
citation; the later manifest contained literal credential URLs in seven check
environments. Its correction repeated a JSON key. Original schema repair limits
held; neither executable coverage review nor installer/clean execution ran.

The previous schema correction missed the actual canonical HTTP boundary:
`json_object` mode discarded schema metadata, while the prompt still contained
the old generic schema. Deliver the exact current logical schema in the final
canonical transport instruction, preserving json_object wire syntax and all
local validation. A regression must inspect the real outbound HTTP payload,
not only the injected chat callback or recorded request metadata.

Credential validators keep rejecting the same literals. Give this known guard
a trusted machine code and safe corrective guidance: use named runtime references,
never move credentials into argv/activation/another field, and never invent
missing runtime values. Do not echo candidate keys, values or exception context.
Test both environment forms and command arguments through real discovery repair
and independent assessment. Duplicate-key replies remain rejected unchanged.
These corrections supply no links, credentials, coverage judgments or approvals,
and do not guarantee model compliance. Preflight and exact-head CI precede the
next fresh labeled Elm attempt with the approved model and original limits.
