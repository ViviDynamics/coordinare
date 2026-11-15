# Complete Review Evidence and Separate Repair Budgets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans sequentially, retaining the previously selected native execution method. Obtain one fresh independent full-diff review after implementation. This plan requires user review before execution.

**Goal:** Deliver complete pinned evidence to each fresh coverage reviewer and permit bounded schema, coverage and executable correction without allowing one category to consume another.

**Architecture:** A shared attempt budget and version-2 journal hold category counters under the original aggregate limits. Review preparation uses a new independent SourceInspection to fetch and bind mandatory files before requesting judgment. Daemon and validation harness consume the same helpers; accepted manifests and readiness contracts remain unchanged.

**Tech Stack:** Existing Python, asyncio, pydantic, pytest and Docker/Kubernetes execution adapters; no new dependencies.

**Spec:** [review-evidence-amendment.md](review-evidence-amendment.md), approved by the user. Earlier [repair-plan.md](repair-plan.md) and [validation.md](validation.md) are historical context.

## Global Constraints

- Three attempt-wide categories: schema, coverage and execution, each default two, independently configurable zero through two. No counter renewal across candidates, reviewer contexts, generations or restarts.
- Preserve aggregate defaults: 600-second attempt, 120-second model phase, 128 model calls, 512 reads; absolute aggregate cap 3600 seconds. Existing configured lower bounds remain authoritative. Elm's 1800-second profile is separately labeled.
- Checkpoint reservations before external work. Source/image/platform drift, incomplete evidence, required-obligation weakening and cleanup uncertainty hold.
- Fresh independent coverage acceptance AND deterministic successful execution of every required applicable check/lifecycle command remain mandatory. Full manifest schema/digest unchanged.
- No ecosystem recipes, repository changes, automatic evidence partitioning, new model routing, suppressed checks or unrelated refactors.
- Preserve original results, pinned snapshots and unrelated worktrees. Full broad suite through CI; focused TDD locally; preflight and quality guard before every push.

## Review Focus

1. Reviewers can discover additional files absent from the first candidate: the revised candidate's next reviewer must get the complete expanded input set (Task 3 test).
2. A lower configuration limit after restart must hold if already consumed, never clamp consumption down or replenish it (Task 2 test).
3. A context-bound failure after additional tool reads must occur before another provider call and must not discard evidence to fit (Task 3 test).
4. Executable cancellation followed by new source/config must not bypass interrupted executor cleanup or legacy migration holds (Task 2/4 tests).
5. A well-formed independent acceptance cannot turn skipped service actions or failed clean checks into readiness (Task 4 test).

## File Responsibilities

- `src/coordinare/services/environment_attempt.py`: category reservation, shared deadline and safe diagnostic counters.
- `src/coordinare/services/environment_discovery.py`: limits, correction classification, evidence preparation and fresh-review conversation.
- `src/coordinare/services/environment_inspection.py`: existing bounded independent source reads and hash ledger; reuse rather than create another reader.
- `src/coordinare/services/environment_repair.py`: journal versioning, category roundtrip, legacy holds, execution-category admission.
- `src/coordinare/config.py`, `src/coordinare/services/env_cache.py`: expose and pass category/context limits, production diagnostics and restart behavior.
- `scripts/validate_environment_repositories.py`: matching opt-in harness limits and recorded outcomes; preserve per-digest scope approval.
- Existing `tests/unit/test_353_{discovery_repair,execution_repair,authoritative_provenance,inspection,bootstrap_pipeline,validation_harness}.py`, `tests/unit/test_daemon_snapshot_persistence.py`: extend existing behavioral tests. New `tests/unit/test_353_review_evidence.py`: deterministic review-delivery/context regressions.
- `docs/onboarding/06-env-cache.md`, `config.example.yaml`, and this feature's spec/tasks/validation documents: operator limits, migration and final observed results. Keep docs changes scoped to existing authoritative sections.

## Task 1: Category budgets and compatible configuration

**Interfaces:** Add `RepairKind = Literal['schema', 'coverage', 'execution']`. `AttemptBudget.reserve_repair(kind: RepairKind) -> None` increments only the selected category or raises its distinct exhaustion code. Add integer fields `schema_repairs`, `coverage_revisions`, `execution_repairs` and corresponding maxima `max_schema_repairs`, `max_coverage_revisions`, `max_execution_repairs`; each max defaults two and validates zero through two. Extend `DiscoveryLimits` with matching limits. `AttemptBudget.summary()` exposes category consumed/max values without source text.

**Configuration:** Add `manifest_max_schema_repairs`, `manifest_max_coverage_revisions`, `manifest_max_execution_repairs`. Retain old `manifest_max_repairs` only as a deprecated configuration alias supplying all three limits when category limits are absent; reject simultaneous old and new settings instead of silently choosing precedence. This configuration alias does not migrate journal consumption. Explicit old zero still disables every category. Do not retain an ambiguous production `reserve_repair()` default.

- [ ] Write `test_category_reservations_are_independent` with all maxima two: reserve schema twice, coverage twice, execution twice; assert each equals two; third reservation of each raises its own code and changes no other counter. Parameterize zero limits and invalid bounds.
- [ ] Write config regressions for defaults, explicit zero, legacy alias and mixed-setting rejection. Watch failures for missing category behavior, not import errors.
- [ ] Implement the interfaces/config validation; route `_repair` to schema, a valid `revise` to coverage, `record_execution_failure` to execution. Update fixtures explicitly; do not weaken source drift, required-obligation or acceptance negatives.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_discovery_repair.py tests/unit/test_353_execution_repair.py tests/unit/test_353_authoritative_provenance.py` plus the affected existing configuration tests selected by `rg`. Expected: all selected tests pass with independently exhausted categories.
- [ ] Commit this cohesive budget change; runtime persistence is completed in Task 2 before pushing or live execution.

## Task 2: Versioned durable journal and migration holds

**Interfaces:** `RepairJournal.version` becomes literal two with each category's consumed/max fields from Task 1. Preserve `begin_attempt`, `save_attempt`, `journal_for`, `record_execution_failure` signatures. `journal_for` must distinguish legacy version one from malformed/unknown versions and issue `legacy_attempt_requires_restart` for pending/held legacy attempts, without rewriting them into unused version-two budgets. Existing accepted frozen manifest/assessment loading does not depend on resuming an old attempt. A ready legacy cache remains readable; an explicitly requested rebuild starts version two only through the existing operator regeneration path after cleanup confirmation.

- [ ] Add tests `test_category_counters_survive_restart`, `test_lowered_limit_does_not_replenish_consumption`, `test_legacy_pending_requires_explicit_restart`, `test_legacy_frozen_contract_remains_usable`, `test_unknown_journal_version_holds`, and `test_interrupted_legacy_executor_still_requires_cleanup`.
- [ ] Assert original UTC expiry/call/read consumption and category consumption survive snapshot roundtrip; changed source/config must not bypass a prior cleanup hold. Counters above lowered maxima hold with the affected category identified.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_execution_repair.py tests/unit/test_daemon_snapshot_persistence.py`; watch new cases fail for the intended state behavior.
- [ ] Implement parsing, validated persistence, nondecreasing save/restore and explicit-regeneration integration. Preserve three rejected entries/generations and all report/redaction bounds. Never infer legacy category history from an aggregate number.
- [ ] Repeat the command; expected all pass. Commit journal/migration changes.

## Task 3: Independent evidence preparation and context bounds

**Interfaces:** Add async `_prepare_review_evidence(reviewer: _Inspection, manifest: EnvironmentManifest) -> list[dict[str, str]]` in `environment_discovery.py`. It batches sorted mandatory paths through `reviewer.read_files`, verifies their actual hashes and returns path/content/hash records for the fresh reviewer conversation. It never copies the discoverer's source cache or ledger. Existing reviewer tool reads remain supported.

Add `DiscoveryLimits.max_review_context_bytes` and configuration `manifest_max_review_context_bytes`, proposed default 1 MiB, valid 1 through 16 MiB. Add `_check_review_context(messages: list[dict[str, str]], limits: DiscoveryLimits) -> int`: measure the full serialized UTF-8 conversation, fail with `review_context_limit` on overflow, return measured bytes for diagnostics. Include schema, manifest, evidence, tool results and model messages. Check immediately before every review provider call; never truncate history/evidence. Apply the same configured output reservation and explicit model input-capacity checks as the existing backend supports. Do not introduce a guessed bytes-to-token conversion. If input capacity is unavailable, retain the byte cap and classify provider context rejection as a hold; record that limitation.

- [ ] Write `test_reviewer_receives_all_mandatory_contents_before_first_call`: discovery reads eight files; the review model immediately returns a verdict without tool calls. Assert a separate fetch of all eight inputs, exact contents/hashes in review messages and complete independent assessment binding.
- [ ] Add negative tests for missing/read-failed/changed mandatory files, checkpoint rejection before fetch, cancellation draining pending reads, and context overflow before judgment. Assert no review provider call or executor dispatch on these paths.
- [ ] Add `test_additional_review_reads_extend_evidence_without_bypassing_context_cap` and `test_revised_candidate_prefetches_expanded_input_set`: first reviewer discovers a ninth file, discovery incorporates it, next independent review gets all nine freshly fetched files. Growing-context overflow holds before another provider call.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_review_evidence.py tests/unit/test_353_inspection.py tests/unit/test_353_authoritative_provenance.py`; watch intended failures.
- [ ] Implement preparation before `_phase` review, keeping the final full-input/citation validator. Keep preparation within the original deadline and shared limits, count it as review preparation, and provide typed error codes. Provider errors may be mapped from structured error types/status, not arbitrary untrusted output text.
- [ ] Repeat tests; expected all pass. Commit evidence delivery and context bounds.

## Task 4: Pipeline integration, diagnostics and Elm replay

**Interfaces:** Production configuration builds the same `DiscoveryLimits` used by the private harness. Persist reservations through strict existing checkpoint callbacks. Result summaries retain the actual stage (`review_preparation`, `review`, `installation`, `verification` as applicable), safe failure code, per-category consumed/max values, elapsed/remaining time, calls, reads, measured context bytes and delivered/required mandatory-input counts. Accepted operator summaries never expose source bodies or model rationale.

- [ ] Add deterministic `test_elm_sequence_schema_then_two_coverage_revisions`: scripted responses fail activation schema once, omit two required lanes, correct those, conflict with generic repository version evidence, correct that, then receive fresh acceptance. Assert schema one, coverage two, execution zero; each reviewer gets complete input evidence and existing required IDs/scopes remain. Use small synthetic project files modeling the failure classes, not private Elm source in committed fixtures.
- [ ] Add pipeline tests where complete coverage acceptance is followed by a failed required check or skipped required service action; assert unready. Then test valid failed execution/confirmed cleanup/new generation/fresh review/all required checks rerun, preserving schema/coverage counters. Unknown/missing reports must never reserve an executable repair.
- [ ] Add harness/daemon tests for matching limits, legacy migration reason, per-digest scope markers, no model retry on context/source drift, safe diagnostics and cleanup after cancellation. Confirm stage reporting does not collapse review failure into discovery.
- [ ] Run `.venv/bin/pytest -q tests/unit/test_353_review_evidence.py tests/unit/test_353_bootstrap_pipeline.py tests/unit/test_353_validation_harness.py tests/unit/test_353_execution_repair.py tests/unit/test_daemon_snapshot_persistence.py`; watch new cases fail, implement minimal integration, then repeat to green.
- [ ] Run relevant operator wording/redaction guards and strict mypy/lint without suppression changes. Update operator docs and config examples for categories, alias, context bound and migration action. Commit integration/docs.

## Task 5: Independent review, CI and actual repository retests

**Interfaces:** Preserve PR #354 and issue #353 ownership. Keep all earlier validation directories immutable; new output directories record exact code/source/image/platform/settings and actual executed stages. Review fixes and validation outcomes stay distinct from coverage acceptance.

- [ ] Run spec/plan/task consistency analysis before implementation begins; add tasks for this amendment under existing feature tracking without marking earlier failures as successes. Self-review full final diff and obtain one fresh independent full-diff adversarial review using native execution's required final reviewer. Address Important/Critical findings in one failing-then-passing fix pass, keeping historical review budgets/outcomes explicit.
- [ ] Commit then run focused preflight and unchanged quality guard before every push. Run the broad suite through CI as requested; classify failures with ci-safety, never waive checks or call old-head results current.
- [ ] Run the pinned sample under defaults and Elm under the separately labeled 1800-second phase/aggregate/private-provider profile used in the last experiment. Keep the model/reasoning/output settings unchanged to isolate this change. New attempts require explicit clean start under version-two journals; do not recycle legacy pending state or alter source snapshots.
- [ ] For every accepted digest, inspect proposed executable scope before granting the harness's digest-specific approval. Execute installation and every required clean check. Confirm all owned containers, image refs, registry storage and caches are cleaned; preserve rejected reports and private transcripts.
- [ ] Update `validation.md` and PR body with exact outcomes, stage/counters, source unchanged status and cleanup proof. If sample or Elm is held, report the precise hold; do not substitute a scripted replay for live success or initiate unbounded retries.
- [ ] Merge only after actual sample AND Elm mandatory coverage and clean-execution success, current-head required CI and completed review fixes. Otherwise leave PR/issue open, preserve worktree, and unwatch before handing results back.

## Self-review and handoff

Spec sections 1/context -> Task 3; section 2/category limits -> Task 1; section 3/migration -> Task 2; section 4/conflicts -> Tasks 3/4; section 5/readiness -> Tasks 4/5; diagnostics -> Task 4; all acceptance scenarios -> Tasks 1-5. Shared interfaces use one category vocabulary and one context limit. Suggested 1 MiB context default is an implementation decision exposed for plan review, not an assurance that any repository fits any provider. No auto-partitioning or model change is introduced.

The prior native execution choice is retained. Written-plan approval is required before implementation. This plan does not authorize a merge while the real repository acceptance gate remains unmet.
