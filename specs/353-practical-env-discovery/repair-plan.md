# Authoritative provenance and executable repair implementation plan

> **For agentic workers:** Use superpowers:executing-plans sequentially. User explicitly requested implementation and retesting; no additional approval pause.

**Goal:** Remove model-owned cryptographic bookkeeping and repair rejected executable environments within one durable finite budget.
**Architecture:** Declaration-only proposal models build unchanged full manifests/assessments from daemon ledgers. A persisted repair journal links rejected execution evidence to a fresh cache generation, next discovery and independent review. Daemon and live harness share admission and budget helpers.
**Tech Stack:** Existing Python, pydantic, asyncio, Docker/Kubernetes adapters.
**Spec:** [repair-amendment.md](repair-amendment.md), extending [spec.md](spec.md).

## Global constraints

Existing full manifest/digest and readiness rules remain authoritative. Maximum two repairs, 128 calls, 512 reads, 3600-second cap; defaults 600-second aggregate/120-second model phase. No framework recipes. Full suite via CI. Preserve private snapshots and other worktrees.

## Review focus

- Required service lifecycle IDs and scope cannot disappear during executable repair.
- Restart and cancellation cannot renew a deadline or mount an old rejected generation.
- Forged reports, secret-bearing output and cleanup uncertainty never enter trusted readiness.
- Cache generation changes alter mount bindings without accepting a different source/image/platform.
- Legacy persisted contracts remain readable; model proposals must exclude provenance fields.

## Task 1: Daemon-authored manifest and assessment

Files: shared environment/models.py; services/environment_discovery.py; existing discovery fixtures; new tests/unit/test_353_authoritative_provenance.py.
Interfaces: declaration-only EnvironmentManifestProposal and CoverageAssessmentProposal; discovery builds EnvironmentManifest and CoverageAssessment from proposal plus actual ledger/identity. Optional previous_manifest and execution_feedback options seed required obligations and untrusted feedback for a new discovery conversation.
- [x] Write regressions: provenance-free proposal accepted with exact daemon hashes; supplied provenance rejected; independent missing/changed reads rejected; previous required IDs/scopes/lifecycle preserved; portable-root guidance present.
- [x] Run targeted tests red before implementation.
- [x] Implement proposal binding and fresh-review validation, keeping full persisted models unchanged. Add explicit generic source-root portability rules.
- [x] Adapt old model-reply fixtures explicitly; retain independent source-drift and extra-field negative controls. Run focused discovery/contract/pipeline tests green.
- [x] Commit task files.

## Task 2: Durable repair journal and cache generations

Files: new services/environment_repair.py; models/env_cache.py; state_store.py; daemon snapshot/rehydration; tests/unit/test_353_execution_repair.py.
Interfaces: begin_attempt(cache, limits, outer_seconds) -> AttemptBudget; save_attempt(cache,budget); record_execution_failure(cache, report, cleanup_confirmed) -> bool; prepare_repair(cache) -> prior manifest/feedback and fresh generation. Helpers preserve counters/UTC expiry and validate stored identity, full-scope report, actual platform and digest. Model feedback capped to 16 KiB, output 512 chars/check; full sanitized rejected reports capped to 256 KiB each, journal capped to 4 MiB and at most three rejected entries. Generation names contain only validated opaque suffixes.
- [x] Write red tests for shared third-repair refusal, finite expiry, snapshot roundtrip, failed report forgery, secret redaction, cleanup uncertainty, required lifecycle preservation and different cache generation.
- [x] Implement and wire optional persisted environment_attempt state, restoring generation only through validated journal data. Keep legacy snapshots safe.
- [x] Run focused repair and snapshot tests green; commit.

## Task 3: Production and live repair orchestration

Files: services/env_cache.py, daemon.py, scripts/validate_environment_repositories.py; pipeline and harness tests.
Interfaces: next discovery resumes the journal budget and previous required contract; completion records only validated failed execution after confirmed quiescence; repaired dispatch uses a new generation. Daemon poll and harness installer/clean verifier are bounded by the journal's remaining deadline. Harness repeats scope inspection per new digest, archives each result and cleans each runtime before repair.
- [x] Reproduce nonportable synthetic installation, failing required check and missing cleanup paths red.
- [x] Wire production failure recording and rediscovery without resetting counters; preserve ordinary bootstrap circuit breaker. Bound poll/verification and cancellation cleanup.
- [x] Drive real runner through a failing then portable repaired contract, fresh review and final freeze; assert failed evidence stays rejected and all required checks rerun. Test both transport dispatch generations and restart.
- [x] Run focused integration/harness/daemon tests green; commit.

## Task 4: Documentation, review, CI and live retest

Files: operator documentation and private validation reports; amend current PR #354.
- [ ] Update proposal ownership, overall deadline, repair evidence and cache generation documentation; self-review full diff and run spec/plan/task consistency analysis before code implementation.
- [ ] Run focused preflight and unchanged quality guard before every push. Full suite via CI; classify actual failures before acting.
- [ ] Obtain the required fresh independent full-diff review for the amendment, preserving earlier review history and explicitly recording this user-authorized new implementation stage.
- [ ] Run pinned small sample and Elm, then all 29 baseline targets with unchanged baseline and separately labeled alternative settings; inspect every new digest's executable scope. No handcrafted success manifests.
- [ ] Confirm snapshot/resource cleanup, record exact revision/stages/checks, update PR. Merge only after actual sample/Elm clean success, final CI and review gates; otherwise report precise holds.

Self-review: all amendment requirements map to Tasks 1-4. Source identity and same-image/platform guards are preserved; cache mount changes are daemon-owned generation changes. Journal keeps failure evidence separate from readiness. No unrelated refactor or new dependency is planned.
