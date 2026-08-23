# Feature Specification: Board-Simulation Benchmark — Phase 2: Scoring + Noise Characterization

**Feature Branch**: `135-board-bench-scoring`
**Created**: 2026-08-23
**Status**: Draft
**Input**: Issue #183 — turn a spec-134 run artifact into a documented, reproducible score, and measure how noisy that score is across repeated identical runs. Phase 2 of the 4-phase benchmark program (134 → 137). Blocked by #182 (landed); blocks #184 (sweep runner) and #185 (optimizer).

## Clarifications

### Session 2026-08-23 (solo — decided and recorded, per Mode)

- Q: What is the default scalar objective formula? → A: `scalar = w_c·correctness_rate − w_cost·(cost_usd/cost_budget) − w_time·(wall_clock_seconds/time_budget)` with defaults `w_c=1.0, w_cost=0.1, w_time=0.1` and operator-configurable budgets; when cost is unknown (`None`) the cost term is omitted and the score object flags `cost_component_missing`. Weights + budgets are embedded in the score object (FR-002) so scalars are only compared across matching weight fingerprints.
- Q: Measure noise on the stubbed substrate or block on 134's deferred real-performer follow-up? → A: Build the machinery now; measure on what runs today; state the fidelity caveat in the go/no-go finding (see Assumptions — recorded fork).
- Q: Is the LLM judge required in CI? → A: No — deterministic-only grading is a first-class labeled mode; CI never needs a live judge.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Score a benchmark run against planted ground truth (Priority: P1)

An operator has completed one board-simulation benchmark run (spec 134) and holds its
run artifact. They ask the system to grade that run: for every seeded card, did the
pipeline reach the **right** outcome versus the fixture's planted ground truth — not
merely a terminal one? The result is a single documented score object combining a
correctness verdict per card with the run's cost and time, rolled up into a scalar
objective plus the component vector behind it.

**Why this priority**: The score is the entire point of the phase — it converts the
raw Phase 1 recording into the objective every later phase (sweep, optimizer)
consumes, and it is the standalone "how well does coordinare complete whole cards
under a given config" capability benchmark even if the program stops here.

**Independent Test**: Feed the scorer a stored run artifact plus its fixture
manifest; verify it emits a schema-valid score object whose per-card verdicts match
hand-graded expectations for known-good and known-bad recorded runs.

**Acceptance Scenarios**:

1. **Given** a valid spec-134 run artifact whose card genuinely satisfies its
   fixture's ground truth (correct code merged, CI green), **When** the scorer runs,
   **Then** the card is graded correct and the score object records it, with the
   evidence trail (deterministic signals + judge verdict) attached.
2. **Given** a run artifact where a card merged but the merged work does **not**
   satisfy the planted ground truth (wrong outcome rubber-stamped through), **When**
   the scorer runs, **Then** the card is graded incorrect — the grade follows truth,
   not the fake board's merge event.
3. **Given** a fixture whose correct outcome is a **non-merge** (e.g. a planted
   vulnerability must be blocked), **When** the recorded run shows the card was
   merged anyway, **Then** the card is graded incorrect; if the run shows it was
   correctly held/blocked, it is graded correct.
4. **Given** the same run artifact scored twice with judging disabled, **When** the
   two score objects are compared, **Then** they are identical (deterministic
   reproducibility of the scoring step itself).
5. **Given** a run artifact with an unrecognized schema version or a fixture id the
   manifest does not contain, **When** the scorer runs, **Then** it fails loudly
   with an actionable error instead of emitting a partial score.

---

### User Story 2 - Reuse the 077 agree-to-PASS grading model (Priority: P1)

The grader lifts the proven persona-benchmark (spec 077) grading approach from
per-role to whole-lifecycle: deterministic checks gather objective signals from the
artifact and the merged work, and an LLM judge rules on behavioural correctness
against the fixture's ground-truth rubric. A card is graded correct only when the
deterministic contract holds **and** the judge agrees (agree-to-PASS); the judge is
optional, and with it disabled the deterministic verdict stands alone and the score
object says so.

**Why this priority**: Reuse-not-reinvent is an explicit acceptance criterion of the
issue; the agree-to-PASS model is what makes the correctness component trustworthy.

**Independent Test**: With a stubbed judge forced to disagree, a deterministically
passing card must not grade correct; with the judge unavailable, grading completes
deterministic-only and labels itself accordingly.

**Acceptance Scenarios**:

1. **Given** a card whose deterministic checks pass, **When** the judge rules the
   behaviour incorrect, **Then** the card grades incorrect and both verdicts are
   recorded.
2. **Given** judging disabled or unreachable, **When** the scorer runs, **Then**
   grading completes on deterministic signals alone and the score object is
   explicitly marked deterministic-only (a judge error never silently upgrades or
   downgrades a verdict).

---

### User Story 3 - Characterize score noise across repeated identical runs (Priority: P2)

An operator runs the **same** configuration N times end-to-end (run + score) and
receives a noise report: per-component mean, variance/spread, and per-card verdict
stability across the repeats. From that evidence the phase produces a written
go/no-go recommendation for Phase 4: is the objective stable enough for automated
config search, or does search need many-repeat averaging or a more deterministic
substrate first?

**Why this priority**: The noise measurement is the go/no-go gate for the optimizer
(spec 137) — but it depends on User Story 1 existing, so it is P2.

**Independent Test**: Point the repeat runner at a fixed config with a small N;
verify it produces N run artifacts, N score objects, and one aggregate noise report
quantifying per-component spread.

**Acceptance Scenarios**:

1. **Given** a fixed config and N ≥ 3 repeats, **When** the repeat runner completes,
   **Then** a noise report exists with per-component mean and variance (and per-card
   verdict agreement) across exactly N scored runs.
2. **Given** one repeat fails to produce a valid artifact, **When** aggregation
   runs, **Then** the failed repeat is reported as such (not silently dropped or
   averaged in) and the report states the effective N.
3. **Given** the completed noise measurement, **Then** a written finding exists in
   the feature's spec directory recording the go/no-go recommendation for Phase 4
   and the evidence behind it, including any substrate-fidelity caveats.

---

### Edge Cases

- A card that never dispatched (final_state `error` before any performer ran):
  graded incorrect for correctness, but flagged as a harness failure — mirroring
  077's PASS/FAIL_MODEL/FAIL_HARNESS split — so config search doesn't optimize
  against infrastructure noise.
- Cost or token fields absent (`None`) in the artifact: the cost component reports
  "unknown", never treated as zero (free) when aggregating or comparing.
- Fixture with an empty `ground_truth` marker: scorer refuses to grade that card's
  correctness and fails loudly, so silently ungraded fixtures can't inflate scores.
- Judge returns malformed output: recorded as a judge error on that card; the card's
  verdict falls back to deterministic-only and is labeled as such.
- Score weights changed between runs: the score object embeds the weights used, so
  two scalar objectives are only comparable when their weight fingerprints match.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST consume a spec-134 run artifact (schema_version 1)
  plus the fixture manifest that seeded the run, and emit one score object per run.
  Unknown artifact schema versions MUST be rejected loudly.
- **FR-002**: The score object MUST contain (a) a per-card correctness verdict with
  its evidence (deterministic signals, judge verdict/reason when judged), (b) a
  component vector — at minimum correctness rate, cost (tokens and estimated USD,
  carrying the artifact's "estimate" label), and time (wall clock) — and (c) a
  scalar objective derived from documented, configurable weights that are embedded
  in the score object itself.
- **FR-003**: Correctness MUST be graded against the fixture's planted ground truth,
  independent of whether the fake board merged the card: fixtures declare the
  expected right outcome (including expected **non-merge** for adversarial
  fixtures), and a merge of wrong work or of work that should have been blocked
  grades incorrect.
- **FR-004**: Grading MUST reuse the spec-077 agree-to-PASS model: deterministic
  checks plus an LLM-judge rubric ruling, with correct = deterministic AND judge
  when judging is enabled; deterministic-only grading MUST work with the judge
  disabled and MUST be labeled as such in the score object.
- **FR-005**: Grading MUST distinguish model/capability failures from harness
  failures (mirroring 077's FAIL_MODEL vs FAIL_HARNESS), so downstream optimization
  is not steered by infrastructure noise; harness-failed cards MUST be visible in
  the score object.
- **FR-006**: The score object MUST be schema-versioned, self-validating
  (round-trip re-parse before being declared written, matching the 134 artifact
  guarantee), and persisted alongside the run artifact it scores.
- **FR-007**: Scoring the same artifact twice with judging disabled MUST produce
  identical score objects (bitwise-stable content excluding timestamps).
- **FR-008**: The system MUST provide a repeat runner that executes the same
  configuration N times (run + score per repeat) and emits an aggregate noise
  report: per-component mean and variance/spread, per-card verdict agreement across
  repeats, and the effective N with failed repeats reported rather than dropped.
- **FR-009**: A written go/no-go finding for Phase 4 MUST be produced from a real
  noise measurement and committed with the feature: it states whether the objective
  is stable enough for automated search, the evidence, and the substrate-fidelity
  caveats under which the measurement was taken.
- **FR-010**: All new grading capability MUST build on the existing benchmark
  surface (134 artifact/fixtures/runner and the 077 grading approach) rather than a
  parallel stack; fixture ground truth MAY be extended with structured
  expected-outcome fields, backward-compatibly (existing manifests keep loading).

### Key Entities

- **Score object**: the documented, versioned result of grading one run — per-card
  verdicts + evidence, component vector (correctness, cost, time), scalar objective,
  the weights and judge configuration used, and a reference to the scored run
  artifact.
- **Card verdict**: one card's grade — correct/incorrect, failure category
  (model vs harness), deterministic signals, judge verdict/quality/reason (when
  judged).
- **Ground truth / expected outcome**: the fixture's planted truth — what the right
  terminal outcome is (merge with satisfying work, or hold/block), and the rubric
  text the judge grades against.
- **Noise report**: the aggregate over N same-config scored runs — per-component
  mean and spread, per-card verdict stability, effective N, failures.
- **Go/no-go finding**: the written Phase 4 recommendation with evidence and
  caveats.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Given any valid Phase 1 run artifact and its manifest, an operator
  obtains a schema-valid score object in a single command, and re-running the
  command on the same inputs (judging disabled) reproduces it exactly.
- **SC-002**: On a curated set of recorded runs with known-correct and known-wrong
  outcomes (including a should-not-merge fixture that merged), per-card verdicts
  match the hand-graded expectation in 100% of cases for deterministic grading.
- **SC-003**: A noise report over ≥ 3 repeats of one fixed configuration exists and
  quantifies mean and variance for every score component, plus per-card verdict
  agreement.
- **SC-004**: A written go/no-go recommendation for Phase 4 is committed, traceable
  to the noise report's numbers, with substrate-fidelity caveats stated.
- **SC-005**: Specs 136/137 can consume the score object without reading Phase 2
  internals: the scalar objective, component vector, and weights are documented in
  a contract.

## Assumptions

- **Substrate fidelity caveat (recorded fork)**: spec 134's real-performer path was
  explicitly deferred (`specs/134-board-sim-benchmark/real-performer-followup.md`);
  the substrate currently runs stubbed performers deterministically, with `--real`
  wired but requiring the performer-boundary GitHub fake. Phase 2 therefore builds
  the full scoring + noise machinery now and takes its committed noise measurement
  on what the substrate can run end-to-end today, characterizing the noise sources
  that exist (LLM-judge variance, timing) — and the go/no-go finding must state
  this caveat explicitly. Full-stack real-performer noise re-measurement re-uses the
  same tooling once the 134 follow-up lands. This keeps #183 standalone and
  unblocks #184/#185's tooling without silently expanding scope into the deferred
  follow-up.
- Judge access uses the same LiteLLM-proxy pathway 077 already uses; judge model
  and endpoint are operator-supplied configuration, and CI never requires a live
  judge (deterministic-only paths are fully testable offline).
- Default scalar objective is correctness-dominant (correctness weighted far above
  cost/time), with weights configurable and always embedded in the score object;
  Phase 3/4 may re-weight or use the component vector directly.
- N for the committed noise measurement defaults to 5 repeats (≥ 3 required by
  SC-003); higher N is an operator choice against cost.
- The `tiny_fixture` and manifest-loaded fixtures carry free-text `ground_truth`
  today; structured expected-outcome fields added by FR-010 default to "merge with
  satisfying work" so existing fixtures keep their meaning.
