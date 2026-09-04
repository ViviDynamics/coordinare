# Tasks: Rank Backend Harnesses Per Role

**Feature**: `161-board-bench-harness` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Closes**: #248

Tests are included and ordered before implementation: the constitution makes Testing
Discipline non-negotiable, and the repo's standing rule requires that a test enforcing a
rule be mutation-checked in the real tree.

**MVP scope**: Phase 3 (US1) alone. It delivers the per-role harness table from
already-captured artifacts and needs no live inference host.

---

## Phase 1: Setup

- [x] T001 Confirm `coordinare.bench` imports cleanly in the venv and that `pyproject.toml` is untouched (no new dependencies), recording the check in the PR body — verified again by T043, this is the early smoke check only

## Phase 2: Foundational (blocking prerequisites for all user stories)

- [x] T002 Create a synthetic-artifact test helper building `RunArtifact` / `CardOutcome` / `PersonaDispatch` objects with arbitrary `terminal_marker`, `role` and `backend` values, in `tests/unit/test_161_fixtures.py`
- [x] T003 Add a recorded-artifact smoke fixture by **generating** a `run.json` with the existing stub runner (no inference host needed) and asserting it loads via `RunArtifact.load`, in `tests/unit/test_161_fixtures.py`. Do NOT assume `runs/` exists — verified: there is no `runs/` directory and no `run.json` anywhere in the tree, so a "find an existing artifact" task would silently no-op

---

## Phase 3: User Story 1 — See which harness is failing which role (P1)

**Goal**: From one or more existing run artifacts, produce a per-`(role, backend)` table
splitting credit, harness defect, environment and inconclusive outcomes.

**Independent test**: Point the rollup at a recorded `run.json` and confirm the table
distinguishes all four outcome classes, with no inference host running.

### Tests first (US1)

- [x] T004 [P] [US1] Test the four-way marker mapping, one case per class, covering every marker in `contracts/outcome-classification.md` (FR-001, FR-002, FR-003, FR-004) in `tests/unit/test_161_harness_outcome.py`
- [x] T005 [P] [US1] Test that a dispatch with `status="failed"` and `terminal_marker="changes_requested"` classifies as `CREDIT` — proves classification reads the marker and NOT the collapsed status (FR-002) in `tests/unit/test_161_harness_outcome.py`
- [x] T006 [P] [US1] Test the credit set equals the **imported** `TERMINAL_SUCCESS_STATES` plus the four negative verdicts, so a copied literal fails the suite (FR-002, R4) in `tests/unit/test_161_harness_outcome.py`
- [x] T007 [P] [US1] Test `None`, `""` and an unrecognized marker all classify as `INCONCLUSIVE`, and that the unrecognized value is surfaced in `unknown_markers` rather than silently credited (FR-005, FR-006) in `tests/unit/test_161_harness_outcome.py`
- [x] T008 [P] [US1] Test rollup groups by `(role, backend)` taken from the dispatch, deduplicates a dispatch registered under multiple stages, and satisfies the count invariant `credit + defect + environment + inconclusive == dispatches` (FR-007, FR-008) in `tests/unit/test_161_harness_rollup.py`
- [x] T009 [P] [US1] Test absent-vs-zero: `conclusive == 0` yields `defect_rate is None` and `credit_rate is None`, never `0.0`; and `tokens_total is None` when no dispatch reported tokens, never `0` (FR-013) in `tests/unit/test_161_harness_rollup.py`
- [x] T010 [P] [US1] Test a role present in config but never dispatched produces **no row** rather than a row of zeros, and that a retry which later succeeds still records its defect (edge cases) in `tests/unit/test_161_harness_rollup.py`
- [x] T011 [P] [US1] Test an unknown `artifact_schema_version` is refused rather than partially interpreted (edge case, mirrors existing scoring behavior) in `tests/unit/test_161_harness_rollup.py`

### Implementation (US1)

- [x] T012 [US1] Implement `OutcomeClass` enum and `classify(dispatch) -> OutcomeClass` reading `terminal_marker` and importing `TERMINAL_SUCCESS_STATES` from `coordinare.graph.nodes.monitor_performer`, in `src/coordinare/bench/harness_outcome.py` (FR-001 through FR-006)
- [x] T013 [US1] Implement `RoleHarnessRow` and `HarnessRollup` pydantic models per `data-model.md`, with the round-trip validation guard the existing artifacts use, in `src/coordinare/bench/harness_rollup.py` (FR-008)
- [x] T014 [US1] Implement `roll_up(artifacts) -> HarnessRollup` with grouping, dedup, conclusive-only rates and `None`-not-zero semantics, in `src/coordinare/bench/harness_rollup.py` (FR-007, FR-009, FR-013)
- [x] T015 [US1] Implement multi-run pooling: sum counts across artifacts, record `runs_contributing`, **and retain `per_run_scalars`** so run-to-run spread stays computable (FR-025) in `src/coordinare/bench/harness_rollup.py`
- [x] T015a [P] [US1] Test that pooling retains `per_run_scalars` with one entry per contributing run, and that pooled counts alone are never the sole retained form — the regression that would silently disable the FR-020 tie band (FR-025) in `tests/unit/test_161_harness_rollup.py`
- [x] T016 [US1] Implement the rollup writer placing `harness_rollup.json` in the run directory beside the `run.json` it describes (FR-026) in `src/coordinare/bench/harness_rollup.py`
- [x] T017 [US1] Add a `python -m coordinare.bench.harness_rollup <run.json>` entrypoint printing the operator table shown in `quickstart.md`, in `src/coordinare/bench/harness_rollup.py`

**Checkpoint**: US1 is independently shippable here. `make lint` and `make test` pass; the
rollup runs against a recorded artifact.

---

## Phase 4: User Story 2 — Vary the harness in a sweep (P2)

**Goal**: A search space can vary one role's harness, and a typo'd harness is rejected
when the space loads rather than failing mid-sweep.

**Independent test**: Declare a harness dimension, expand it, confirm one point per
harness with only that role changed; confirm an unknown harness name is rejected at load.

### Foundational for US2 (R8)

- [x] T018 [US2] Test that `performer.backends.SUPPORTED_BACKENDS` is importable, is a module-level mapping, and contains exactly the eight known harnesses, in `tests/unit/test_161_harness_space.py` (R8)
- [x] T019 [US2] Test `get_backend` behavior is unchanged after the extraction: a valid name still returns its adapter and an unknown name still raises `UnsupportedBackendError` with the same message shape (R8 regression pin) in `tests/unit/test_161_harness_space.py`
- [x] T020 [US2] Promote the function-local `supported_backends` dict to a module-level `SUPPORTED_BACKENDS` constant and have `get_backend` read it, in `agent/performer/src/performer/backends/__init__.py` (R8; makes the existing docstring true, no behavior change)

### Tests first (US2)

- [x] T021 [P] [US2] Test a harness dimension expands to one point per choice, overriding only `global_config.performers.<role>.backend` and holding all else fixed (FR-014, FR-015) in `tests/unit/test_161_harness_space.py`
- [x] T022 [P] [US2] Test each generated point materializes to a schema-valid configuration with the role's harness set to the swept value (FR-016) in `tests/unit/test_161_harness_space.py`
- [x] T023 [P] [US2] Test an unknown **harness name** is rejected at space-load time naming the offending value, and confirm an unknown **role** is already rejected by `SpaceError` (FR-017; R2 showed the harness half is currently accepted silently) in `tests/unit/test_161_harness_space.py`
- [x] T024 [P] [US2] Test a harness that fails for a role is recorded in that point's artifact as a failed dispatch attributable to that harness and the sweep continues to remaining points (FR-018 — note classification into defect-vs-verdict is US1's rollup, so this test must NOT depend on the classifier) in `tests/unit/test_161_harness_sweep.py`

### Implementation (US2)

- [x] T025 [US2] Add harness-dimension support and load-time harness-name validation against `SUPPORTED_BACKENDS`, in `src/coordinare/bench/space.py` (FR-014 through FR-017)
- [x] T026 [US2] Ensure a failing sweep point is recorded and the sweep continues, in `src/coordinare/bench/sweep.py` (FR-018)
- [x] T027 [US2] Create the harness search space declaring per-role harness dimensions, in `benchmarks/spaces/harness.yaml` (FR-014)

**Checkpoint**: US1 + US2 shippable. A sweep can vary the harness and rejects typos early.

---

## Phase 5: User Story 3 — Rank harnesses and recommend (P3)

**Goal**: A per-role ranking with explicit ties, insufficient-evidence and
no-viable-harness verdicts, each citing its evidence.

**Independent test**: Feed scored runs covering multiple harnesses per role; confirm a
ranked table with a winner, a declared tie, or an explicit insufficient-evidence result.

### Tests first (US3)

- [x] T028 [P] [US3] Test `ScoringView` is carried on every emitted score and that a `HARNESS_COMPARISON` score is never compared against a `CONFIG_COMPARISON` one (FR-012) in `tests/unit/test_161_harness_rank.py`
- [x] T029 [P] [US3] **FR-011 regression**: test that for a fixed-harness sweep the existing config-comparison **scalar and component values are numerically unchanged** (assert on values via `compute_scalar`, NOT on serialized bytes — verified that adding any field changes `ScoreObject`'s 13-key JSON output, so a byte-identity assertion would be unsatisfiable), in `tests/unit/test_161_score_regression.py`
- [x] T029a [P] [US3] Test the additive-change policy: `SCORE_SCHEMA_VERSION` is bumped, the view discriminator defaults to `CONFIG_COMPARISON`, and a score document written without a view still loads and reads as config-comparison (FR-011, FR-012) in `tests/unit/test_161_score_regression.py`
- [x] T030 [P] [US3] Test the harness objective mirrors `compute_scalar`'s shape (credit term less normalized time and token penalties) with weights embedded, and that a differing weight fingerprint blocks comparison (FR-024, FR-010) in `tests/unit/test_161_harness_rank.py`
- [x] T031 [P] [US3] Test a scalar is **withheld as `None`**, not `0.0`, when a pair has no conclusive dispatches (FR-013) in `tests/unit/test_161_harness_rank.py`
- [x] T031a [P] [US3] Test the cost-term unit rule: tokens are converted via `estimate_cost_usd()` before division by `cost_budget_usd`, a raw token count is never divided by the currency budget, and a missing token count omits the cost term and records the omission rather than assuming zero cost (FR-024) in `tests/unit/test_161_harness_rank.py`
- [x] T032 [P] [US3] Test the tie rule reads **`per_run_scalars`** (not the pooled row): candidates tie when `|Δmean| <= stdev_a + stdev_b` via `noise.py::component_stats`, and a single contributing run yields `stdev == 0` **and** sets `single_run` so an unmeasured band is not reported as a confident tie (FR-020) in `tests/unit/test_161_harness_rank.py`
- [x] T033 [P] [US3] Test insufficient evidence: a role with fewer than two rankable harnesses, and a pair below the configurable minimum of three conclusive dispatches, both report `insufficient_evidence` (FR-021) in `tests/unit/test_161_harness_rank.py`
- [x] T034 [P] [US3] Test `no_viable_harness` when every candidate has `credit_rate == 0`, rather than crowning the least-bad option (FR-027) in `tests/unit/test_161_harness_rank.py`
- [x] T035 [P] [US3] Test every ranking entry cites its evidence row, and that ranking writes only a file and never mutates live deployment config (FR-022, FR-023) in `tests/unit/test_161_harness_rank.py`

### Implementation (US3)

- [x] T036 [US3] Add the `ScoringView` enum and attach a view tag to emitted scores, additively so existing scalars keep their meaning, in `src/coordinare/bench/score.py` (FR-011, FR-012)
- [x] T037 [US3] Implement `HarnessScore` and the harness-comparison objective mirroring `compute_scalar`, in `src/coordinare/bench/harness_rank.py` (FR-010, FR-024)
- [x] T038 [US3] Implement `HarnessRanking` with the tie rule, the configurable conclusive minimum, and the four verdicts, in `src/coordinare/bench/harness_rank.py` (FR-019 through FR-022)
- [x] T039 [US3] Implement the ranking writer at the sweep root and a `python -m coordinare.bench.harness_rank <sweep-dir>` entrypoint (FR-023, FR-026) in `src/coordinare/bench/harness_rank.py`
- [ ] T040 [US3] **BLOCKED (external, still open at merge)**: execute a real-performer confirmation run on the single self-hosted model and record the resulting per-role ranking in the PR body. Blocked on the Spark inference host, currently unresponsive (accepts TCP, returns zero bytes on HTTP, SSH banner timeout). Do NOT drop or fake this; if still blocked at merge, note it explicitly as unverified-live and keep it open under #248.

---

## Phase 6: Polish & Cross-Cutting

- [x] T041 **Mutation-check the rule-enforcing tests** in the real tree, per the repo's standing rule — one mutation per rule instance, each confirmed to FAIL: (a) make `classify` read `status` instead of `terminal_marker` (T005 must fail), (b) replace the imported `TERMINAL_SUCCESS_STATES` with a copied literal and remove a member (T006 must fail), (c) coerce a `None` rate to `0.0` (T009, T031 must fail), (d) accept an unknown harness name (T023 must fail), (e) drop the tie band so any difference ranks (T032 must fail), (f) discard `per_run_scalars` and keep only pooled counts (T015a and T032 must fail), (g) divide a raw token count by `cost_budget_usd` (T031a must fail), (h) change a config-comparison scalar's computed value (T029 must fail). Revert every mutation. A MISSED result means the mutation never applied — verify it did.
- [x] T042 [P] Run `make lint` and `make fmt`, resolving every warning to zero
- [x] T043 [P] Run `make test-all` (unit + contract) and confirm green, capturing output for the PR body
- [x] T044 Verify every one of the 26 functional requirements maps to at least one completed task, and record the FR-to-task map in the PR body
- [x] T045 Update `benchmarks/spaces/README` guidance or the spec's quickstart if operator-facing behavior drifted during implementation (constitution: documentation updated when user-facing behavior changes)

---

## Dependencies

```
Phase 1 (T001)
  └─> Phase 2 (T002, T003)         foundational fixtures
        ├─> Phase 3 US1 (T004..T017)      ← MVP, no live host
        ├─> Phase 4 US2 (T018..T027)      needs T020 (R8) before T025
        └─> Phase 5 US3 (T028..T039)      needs US1's rollup (T014)
              └─> T040 (blocked on external host)
                    └─> Phase 6 (T041..T045)
```

- **US1** depends only on Phase 2. Independently shippable.
- **US2** is independent of US1 and requires its own T020 first. Independence holds only
  because FR-018 was narrowed to *recording* the failed dispatch; classifying it as a
  defect is US1's rollup. If FR-018 is ever widened back to classification, US2 gains a
  hard dependency on T012.
- **US3** consumes US1's rollup output (including `per_run_scalars`), so it follows Phase 3.
- **T040** is the only task gated on external infrastructure.

## Parallel Opportunities

- T004-T007 (classification tests) are parallel: same file, independent cases; write together.
- T008-T011 (rollup tests) parallel with each other.
- T021-T024 (space tests) parallel after T020.
- T028-T035 (ranking tests) parallel with each other.
- US1 and US2 phases can proceed in parallel by two workers, since they touch disjoint files
  (`harness_outcome.py`/`harness_rollup.py` vs `space.py`/`sweep.py`/`backends/__init__.py`).

## FR Coverage Map

| FR | Tasks |
| --- | --- |
| FR-001 | T004, T012 |
| FR-002 | T004, T005, T006, T012 |
| FR-003 | T004, T012 |
| FR-004 | T004, T012 |
| FR-005 | T007, T012 |
| FR-006 | T007, T012 |
| FR-007 | T008, T014 |
| FR-008 | T008, T013 |
| FR-009 | T003, T014 |
| FR-010 | T030, T037 |
| FR-011 | T029, T029a, T036 |
| FR-012 | T028, T036 |
| FR-013 | T009, T031, T014 |
| FR-014 | T021, T025, T027 |
| FR-015 | T021, T025 |
| FR-016 | T022, T025 |
| FR-017 | T018, T019, T020, T023, T025 |
| FR-018 | T024, T026 |
| FR-019 | T038 |
| FR-020 | T032, T038, T015a |
| FR-021 | T033, T038 |
| FR-022 | T035, T038 |
| FR-023 | T035, T039 |
| FR-024 | T030, T031a, T037 |
| FR-025 | T015, T015a |
| FR-026 | T016, T039 |
| FR-027 | T034, T038 |

All 27 functional requirements are covered.

## Implementation Strategy

Ship US1 first and stop to verify: it is the slice that produces a usable answer from
data already on disk, and it is the only slice that cannot be blocked by infrastructure.
Add US2 next so fresh comparable data can be generated. US3 last, since it is the only
part that needs a healthy inference host, and its live confirmation (T040) is currently
blocked.
