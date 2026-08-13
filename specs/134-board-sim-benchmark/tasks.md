# Tasks: Board-Simulation Benchmark — Phase 1 (evaluation substrate)

**Input**: Design documents from `/specs/134-board-sim-benchmark/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: INCLUDED — Constitution II (Testing Discipline) is non-negotiable and the
spec defines test acceptance (SC-003/004/006). Contract + unit + one integration.

**Organization**: Grouped by user story. NOTE (see Dependencies): this is a *layered
substrate* — US1 (end-to-end) integrates US2 (fake), US3 (approver), and US4
(fixtures). Story labels are for traceability; the real build order is Foundational →
US2 → US3/US4 → US1.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: parallelizable (different files, no dependency on an incomplete task)
- Paths are repo-root relative (single-project layout).

---

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 Create the bench package skeleton `src/coordinare/bench/__init__.py` (empty package marker; docstring naming it Phase-1 benchmark substrate, not imported by production paths)

---

## Phase 2: Foundational (Blocking Prerequisites) ⚠️ blocks ALL stories

**Purpose**: the contract every later piece depends on. Per research.md D3 and
`contracts/github-service-protocol.md`.

- [ ] T002 Expand `GitHubServiceProtocol` in `src/coordinare/graph/state.py` to the full node-called surface (all methods in `contracts/github-service-protocol.md`), including the private `_current_token`; keep it `@runtime_checkable` and async-accurate
- [ ] T003 [P] Contract test `tests/unit/test_134_protocol_conformance.py`: assert the real `GitHubService` structurally satisfies the expanded Protocol (proves FR-002 / SC-003 — no behavior change)
- [ ] T004 Run `make test` and confirm the existing suite is still green after T002 (checkpoint: no regression from the Protocol expansion)

**Checkpoint**: Protocol is the single documented contract; real service satisfies it.

---

## Phase 3: User Story 2 - Faithful in-process board simulation (Priority: P1)

**Goal**: `FakeGitHubService` models board/branches/PRs/reviews/CI/comments/merges
in-process, returning the shapes coordinare reads, backed by real local git + real
pytest. Prerequisite for US1.

**Independent Test**: exercise the fake directly (no daemon) — create branch/PR, run
CI, approve, merge — and confirm shapes match the real service and a merge is
reflected in later reads (SC-004).

### Tests for User Story 2 ⚠️ write first, watch them fail

- [ ] T005 [P] [US2] Unit test `tests/unit/test_134_fake_github.py::test_poll_board_returns_real_snapshot_shape` — board seed → `poll_board()` snapshot shape + `move_card` column mutation
- [ ] T006 [P] [US2] Unit tests (same file) for git-reading methods against a `file://` bare repo: `get_pr_diff`, `compare_changed_files`, `get_file_content`, `get_file_blob_sha`, `branch_exists`
- [ ] T007 [P] [US2] Unit tests (same file) for `check_mergeability` (6-key dict, `mergeable` = MERGEABLE AND APPROVED), `get_pr_reviews`/`get_pr_review_context` shapes, `squash_merge` real local merge → truthful later reads
- [ ] T008 [P] [US2] Unit test (same file) for CI: real pytest against PR head → `CheckRollup` via the injected fake `PrChecksService` on `_pr_checks_service_cache`; `get_required_status_checks` → `{"pytest"}`
- [ ] T009 [P] [US2] Unit test (same file) `test_fake_never_raises_on_unknown_or_degraded_input` — every method degrades to a safe default (research.md D1 invariant)

### Implementation for User Story 2

- [ ] T010 [US2] Implement `FakeGitHubService` core in `src/coordinare/services/fake_github.py`: plain mutable object; board model (cards dict), `poll_board`/`move_card`/issue methods, in-memory event log; `initialize`/`aclose`/`current_token`/`_current_token`/`org`/`project_name`/`_org`/`_project_name`/`project_id`
- [ ] T011 [US2] Add git-backed reads in `fake_github.py`: run real `git` against the fixture's `file://` bare repo for `get_pr_diff`, `compare_changed_files`, `get_file_content`, `get_file_blob_sha`, `branch_exists`, `branch_has_open_pr`, `list_prs_by_branch_prefix`, `delete_branch`, `get_repository_id`
- [ ] T012 [US2] Add PR/review model in `fake_github.py`: `find_pr_for_issue`, `count_closed_prs_for_issue`, `get_pr_reviews`, `get_pr_review_context`, `request_reviews`, `request_reviewers`, `check_mergeability` (6-key), `add_comment`, `post_comment` (records+no-ops per D6), label methods
- [ ] T013 [US2] Add CI in `fake_github.py`: a fake `PrChecksService` returning a `CheckRollup` (from `pr_checks_service.py`) built by running real pytest against a clean PR-head checkout, cached per `head_sha`; pre-populate `_pr_checks_service_cache`; implement `get_required_status_checks`, `fetch_failed_job_log`
- [ ] T014 [US2] Add merge in `fake_github.py`: `squash_merge` performs a real local git merge into the bare repo's default branch and records it; head_sha read live from git
- [ ] T015 [US2] Extend `test_134_protocol_conformance.py` to assert `FakeGitHubService` also satisfies `GitHubServiceProtocol`

**Checkpoint**: `test_134_fake_github.py` green; fake satisfies the Protocol.

---

## Phase 4: User Story 3 - Pluggable approval, default gates_green (Priority: P2)

**Goal**: a PR reaches approved/mergeable without a human via one injected callable.

**Independent Test**: drive a PR to green gates + green CI; policy withholds until
both hold, then approves and an APPROVED review from a `human_reviewers` login
appears (US3 acceptance).

### Tests for User Story 3 ⚠️

- [ ] T016 [P] [US3] Unit test `tests/unit/test_134_approver.py`: `gates_green` withholds when gates/CI incomplete, approves when both green; on approval the fake's `check_mergeability` reports `review_decision == "APPROVED"`

### Implementation for User Story 3

- [ ] T017 [US3] Implement `gates_green(pr_state) -> bool` in `src/coordinare/bench/approver.py` (callable, not a class hierarchy — research.md D7)
- [ ] T018 [US3] Wire `approver` into `FakeGitHubService` (`fake_github.py`): when the policy fires, insert an APPROVED review authored by a `human_reviewers` login into the PR's review list

**Checkpoint**: a fake PR can reach `review_decision == APPROVED`.

---

## Phase 5: User Story 4 - Fixtures + board seeding (Priority: P3)

**Goal**: seed fresh whole-lifecycle cards from a manifest into the fake's backlog,
each backed by a materialized `file://` bare repo.

**Independent Test**: load the manifest, seed, and confirm cards appear in the
backlog column via `poll_board`.

### Tests for User Story 4 ⚠️

- [ ] T019 [P] [US4] Unit test `tests/unit/test_134_fixtures.py`: manifest load + bare-repo materialize + seeding → seeded cards present in backlog via `poll_board`

### Implementation for User Story 4

- [ ] T020 [US4] Implement `src/coordinare/bench/fixtures.py`: manifest loader (fixture id, task body, ground-truth marker, repo seed) + `file://` bare-repo materializer + card seeding into `FakeGitHubService`
- [ ] T021 [US4] Author the tiny built-in integration fixture (a pure function + its pytest) under the bench fixtures location, cheap enough for stubbed-performer CI

**Checkpoint**: seeding produces a board the fake reports via `poll_board`.

---

## Phase 6: User Story 1 - Run end-to-end and emit a validated artifact (Priority: P1) 🎯 MVP capstone

**Goal**: inject the fake, drive the real daemon to terminal under budget, emit and
validate one artifact, tear down. Integrates US2/US3/US4.

**Independent Test**: seed one tiny fixture, stubbed performer, run to terminal,
assert loop closes and `run.json` validates (SC-001/002/006).

### Tests for User Story 1 ⚠️

- [ ] T022 [P] [US1] Unit test `tests/unit/test_134_artifact_schema.py`: `RunArtifact` + submodels validate + round-trip; `final_state` ∈ {merged,blocked,abandoned,error}; `cost_estimated` True (FR-011/012)
- [ ] T023 [US1] Integration test `tests/integration/test_134_end_to_end.py`: one tiny fixture, performer STUBBED via `node_overrides`, drive daemon to terminal, assert loop closes + artifact validates (deterministic, no model cost — SC-006)

### Implementation for User Story 1

- [ ] T024 [P] [US1] Implement artifact models in `src/coordinare/bench/artifact.py`: `RunArtifact`/`CardOutcome`/`PersonaDispatch`/`GateDecision`/`CIResult`/`RunTotals`, `schema_version`, `cost_estimated`, `validate()` (per data-model.md §A + `contracts/run-artifact.md`)
- [ ] T025 [P] [US1] Implement `src/coordinare/bench/recording_performer.py`: thin wrapper delegating to the real performer service, recording per-dispatch `job_id/session_id/container_id/status/tokens_processed/timing` + raw summary
- [ ] T026 [US1] Implement `run_board()` in `src/coordinare/bench/runner.py`: load config (`ProjectConfiguration.from_yaml`), materialize fixtures, build graph (`CoordinareGraphBuilder().build()`, `node_overrides` stub in test mode), construct `CoordinareDaemon(max_cycles, sleep_func=<no-op>, state_store=None)`, inject fake + recording performer + no-op notifier, keep `symphony_configs` empty
- [ ] T027 [US1] Add drive-to-terminal loop + hard budget in `runner.py`: run `daemon.start()`; stop on max_cycles / wall-clock / all cards DONE|BLOCKED; assign every card a terminal `final_state` incl. `abandoned`/`error` (FR-009)
- [ ] T028 [US1] Add artifact writer in `runner.py`: build `RunArtifact` from the fake event log + recording wrapper + token×rate cost estimate; write `run.json` + `raw/`; call `validate()` before declaring complete (FR-011)
- [ ] T029 [US1] Add teardown in `runner.py`: stop daemon, `aclose` fake, remove ephemeral performer containers, delete temp repos/scratch (FR-013)
- [ ] T030 [US1] Implement `scripts/board_bench.py` CLI over `run_board` (`--config`, `--fixtures`, `--run-dir`, `--max-cycles`, `--max-wall-clock`) with actionable errors on misconfig

**Checkpoint**: `test_134_end_to_end.py` green; a real run (opt-in) produces a valid artifact.

---

## Phase 7: Polish & Cross-Cutting (incl. companion repo)

- [ ] T031 [P] conductor-bench companion (separate repo `ViviDynamics/conductor-bench`): author whole-lifecycle fixture cards + `BENCH.md` rows for the new cards and the two undocumented planted branches (`feat/impl-failing-test` → implementer, `feat/closer-ready` → closer) (FR-015 / SC-007)
- [ ] T032 [P] Validate `quickstart.md` end-to-end (stubbed path) and fix any drift
- [ ] T033 Run `make test-all`; confirm all new tests + existing suite green; no lint/type regressions

---

## Dependencies & Execution Order

### Phase order (real, dependency-driven)

- **Setup (P1 tasks T001)** → **Foundational (T002–T004, blocks all)** → **US2 fake
  (T005–T015)** → **US3 approver (T016–T018)** + **US4 fixtures (T019–T021)** in
  parallel → **US1 capstone (T022–T030)** → **Polish (T031–T033)**.

### Story dependencies (this is a layered substrate — not independent stories)

- **US2 (fake)** depends only on Foundational. It is a P1 prerequisite for US1.
- **US3 (approver)** depends on US2 (wires into the fake's review model).
- **US4 (fixtures)** depends on US2 (seeds into the fake). Parallel with US3.
- **US1 (end-to-end)** depends on US2 + US3 + US4 — it is the integrating capstone,
  hence sequenced last despite being P1.

### Within a story

- Tests written first and FAIL before implementation (Constitution II).
- Fake core (T010) before its git/PR/CI/merge layers (T011–T014).

### Parallel opportunities

- T005–T009 (US2 tests, all `[P]`, one file but independent cases) can be authored together.
- US3 (T016–T018) and US4 (T019–T021) can proceed in parallel once US2 is green.
- T024 (artifact models) and T025 (recording wrapper) are independent `[P]`.
- T031 (companion repo) is fully parallel — different repo — and can start anytime.

---

## Implementation Strategy

### MVP (thinnest end-to-end)

1. Phase 1 Setup + Phase 2 Foundational (Protocol).
2. Phase 3 US2 (fake) — the irreducible base.
3. Phase 4/5 US3 + US4 (approver + one tiny fixture).
4. Phase 6 US1 — runner + artifact + stubbed integration test. **STOP and VALIDATE**:
   `make test-all` green, `run.json` validates.
5. Phase 7 — companion repo + polish (can lag; different repo).

### Notes

- Commit after each task or logical group.
- The fake must never raise (a raise aborts `daemon.start()`).
- Keep the bench package free of any production import path.
- Cost is a token×rate estimate (`cost_estimated: true`), not authoritative USD.
