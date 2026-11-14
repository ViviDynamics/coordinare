# Tasks: Nare bootstrap runtime replacement

Input: spec.md, approved nare-bootstrap-design.md and plan.md in this directory.
Execute each plan task's red/green/verification/commit cycle; these IDs provide
requirements traceability. No product changes before plan review and analysis.

## Setup and foundation

- [x] T001 Resolve prerequisite #355/main lineage and record baseline in specs/376-nare-discovery-spike/validation.md; carry approved specs onto isolated376-nare-bootstrap branch created from main, locally integrate dependencies, test baseline (plan Task1 prerequisite).
- [x] T002 Write failing runtime/config tests in tests/unit/test_376_agent_runtime.py and tests/unit/test_376_agent_configuration.py (FR01/FR06).
- [x] T003 Implement models/configuration/nare_runtime under src/coordinare/services/agent_runtime/ and verify exact nare2026.11.104 pin in pyproject.toml and uv.lock; run tests and normal build imports, commit (plan Task1).

## US1: Reliable evidence-backed review (P1)

Independent test: full page bytes cross real nare scripted transport; early answers
refuse; final assessment passes the unchanged domain validator with separate role.

- [x] T004 [US1] Write evidence red tests in tests/unit/test_376_agent_evidence.py including Unicode/escaping/partial response/tampered source/compaction (FR02/FR03).
- [x] T005 [US1] Implement EvidenceStore.read in src/coordinare/services/agent_runtime/evidence.py, verify exact transport bytes and rereads, commit (plan Task2).

## US3: Restart and terminal outcomes (P1 foundation for live review)

Independent test: restore identity-bound sessions without renewed allowances;
refuse stale/tampered receipts; no replay of uncertain external effects.

- [x] T006 [US3] Write checkpoint integrity/resume/effect receipt red tests in tests/unit/test_376_agent_checkpoint.py (FR04/FR08).
- [x] T007 [US3] Implement checkpoint.py plus versioned references in environment_repair.py and state_store.py; test atomic interruption and real nare save/load, commit (plan Task3).
- [x] T008 [US3] Write accounting/cancellation/stream-retry/dedup red tests in tests/unit/test_376_agent_accounting.py (FR05/FR07).
- [x] T009 [US3] Implement accounting.py and bridge environment_attempt.py, conductor_calls.py and bootstrap_status.py; verify exact budget ownership and terminal mapping, commit (plan Task4).

## US1 completion: Production reviewer

- [x] T010 [US1] Write native reviewer contract/independence/early-refusal red tests in tests/unit/test_376_environment_review.py (FR01/FR02).
- [x] T011 [US1] Implement environment_agent.py and native reviewer wiring in environment_discovery.py and environment_discovery_prompts.py; run deterministic tests and bounded live replay, record validation.md and commit (plan Task5).

## US2: Complete bootstrap through nare (P1)

Independent test: scope/author/reviewer/repair produce the same domain decisions
under scripted proposals; real acceptance includes installer and clean consumer.

- [ ] T012 [US2] Write phase/repair/probe/trial parity red tests in tests/unit/test_376_bootstrap_phases.py (FR01/FR02/FR05/FR08).
- [ ] T013 [US2] Migrate phase bodies in environment_scope.py, environment_shard_scope.py, environment_discovery.py and environment_repair.py to environment_agent.py; preserve domain scheduling and full repair; run affected353 suites and commit (plan Task6).

## US3 completion: Daemon lifecycle

- [ ] T014 [US3] Write old-state/restart/cancel/one-active-bootstrap red tests in tests/integration/test_376_bootstrap_lifecycle.py (FR04/FR07/FR09).
- [ ] T015 [US3] Wire env_cache.py and state_store.py migration policy to runtime and checkpoint references; verify no domain budget reset and commit (plan Task7).

## US4: Acceptance and retirement (P2)

Independent test: final bootstrap cannot instantiate a legacy runtime; accepted
artifact passes independent clean-consumer/provenance checks after deletion.

- [ ] T016 [US4] Write missing-legacy-transport and provenance refusal tests in tests/unit/test_376_no_legacy_bootstrap.py and tests/integration/test_376_bootstrap_acceptance.py (FR09/FR10).
- [ ] T017 [US4] Run authorized pinned Dettmore and eligible non-Rails production-entrypoint acceptance, including failure/repair and daemon lifecycle, record source/image/manifest/cleanup evidence in validation.md (plan Task8).
- [ ] T018 [US4] Delete old loop-only code and legacy JSON tool protocol in the three phase modules after T017 passes; retain shared non-bootstrap utilities; verify no fallback and commit (FR10).
- [ ] T019 [US4] Implement tests/benchmarks/test_376_agent_runtime.py and measure paging/memory/startup against spec budgets; wire benchmark lane; rerun affected tests and full configured checks on final deletion head (FR05/FR09/FR10).
- [ ] T020 [US4] Update docs/operators/reviewed-environment-manifest.md and validation.md; inventory service inference/config assistant/observer followups in followups.md without migrating them (FR06/FR07/FR11).
- [ ] T021 [US4] Obtain adversarial final-diff review, resolve findings and reverify; use normal PR review/check gates with final validation.md evidence (FR09/FR10).

## Dependencies and execution

T001->T002->T003->T004->T005->T006->T007->T008->T009->T010->T011->T012->T013->T014->T015->T016->T017->T018->T019->T020->T021.
Evidence and checkpoint test authoring can be parallel after shared contracts are
stable; do not parallelize mutations to shared phase files. Native execution is
recommended because budgeting, receipts and domain integration are tightly coupled.
First demonstrable slice is US1 after its US3 foundations; it is not the final
single-runtime delivery. No merge before the final deletion and acceptance gates.
