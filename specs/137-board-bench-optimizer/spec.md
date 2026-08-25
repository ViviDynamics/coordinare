# Feature Specification: Board-Simulation Benchmark — Phase 4: Automated Config Optimizer

**Feature Branch**: `137-board-bench-optimizer`
**Created**: 2026-08-24
**Status**: Draft
**Input**: Issue #185 — close the benchmark program's loop (134 substrate → 135 scorer → 136 space/sweep): an adaptive search that proposes configurations, evaluates them, learns from scores, and converges toward the best config under a hard evaluation budget. Blocked by #184 (landed). Gated by 135's noise finding — see Clarifications.

## Clarifications

### Session 2026-08-24 (solo — decided and recorded, per Mode)

- Q: Is the issue's explicit go/no-go gate satisfied? → A: Yes for building and validating the optimizer. 135's finding was **GO — conditional**: the objective is stable (scalar spread orders of magnitude below one verdict step); the condition — a fresh noise re-measurement under real performers — governs the first **real** search, not the machinery. That condition is enforced in-product: see the real-search precondition below.
- Q: 136's ablation found the stub substrate carries no config signal (all deltas are timing jitter) — how can an optimizer demonstrate outperformance? → A: Recorded fork, same pattern as 135/136: the search algorithm's convergence is **proven against a synthetic objective with a planted optimum** (injected behind the same evaluator interface the real path uses), and the end-to-end real path (materialize → run → score × repeats) is exercised in stub mode for plumbing only, reported honestly as signal-free. The committed report states both.
- Q: Which search method, and why not Bayesian-optimization libraries? → A: A seed-deterministic **evolutionary local search** (best-first hill-climb over single-dimension mutations, with random restarts and fingerprint memoization so no configuration is ever evaluated twice). Justification: the 136 space is small and discrete (~9 mostly-boolean dimensions, low hundreds of combinations), evaluations are expensive full-board runs, and the constitution's minimal-dependency principle rules out heavyweight GP/BO libraries for a space this size; a memoized mutation search is sample-efficient here and trivially auditable.
- Q: Real-search precondition? → A: Running the optimizer against the real substrate (`--real`) requires the operator to supply a 135 noise report measured on that substrate; repeats-per-evaluation derive from it (136's derivation). Refusing to run a real search without one enforces 135's condition rather than documenting it.
- Q: Single objective or Pareto front? → A: Single scalar objective (the 135 weighted scalar) for this phase; the full component vector is recorded for every evaluation so a Pareto view can be derived from the trace later without re-running. Recorded as an assumption.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run a budgeted adaptive search that recommends a config (Priority: P1)

An operator points the optimizer at a 136 search space and a budget (a hard
cap on evaluations and on wall-clock). The optimizer proposes configurations,
evaluates each through the benchmark (run + score, with repeats), learns from
the results, and stops when the budget is exhausted or the space is fully
explored. It returns a recommended configuration with its evidence: score,
component vector, fingerprint, and the exact overrides that produce it.

**Why this priority**: The budgeted loop returning a defensible
recommendation is the entire point of the phase — the program's end goal.

**Independent Test**: Run the optimizer with a small budget against an
injected synthetic objective with a known planted optimum; verify it finds
the optimum within the budget, never exceeds the cap, and never evaluates the
same configuration twice.

**Acceptance Scenarios**:

1. **Given** a space with a planted optimum reachable by single-dimension
   improvements, **When** the optimizer runs with a sufficient budget,
   **Then** the recommendation is the planted optimum and the trace shows
   monotonically non-decreasing best-so-far scores.
2. **Given** an evaluation budget of N, **When** the search would want more,
   **Then** exactly ≤ N evaluations run and the artifact says the budget
   stopped the search (not convergence).
3. **Given** the memoization guarantee, **When** the search proposes a
   configuration whose fingerprint was already evaluated, **Then** the cached
   result is reused and the evaluation budget is not charged.
4. **Given** the same space, seed, and budget, **When** the optimizer runs
   twice against a deterministic evaluator, **Then** the traces and
   recommendations are identical (seed-deterministic search).
5. **Given** a space whose total distinct configurations number fewer than
   the budget, **When** the search exhausts them, **Then** it stops early and
   reports full-space coverage.

---

### User Story 2 - Noise-aware evaluation per 135's prescription (Priority: P1)

Each evaluation of a proposed configuration runs the benchmark
repeats-per-evaluation times, with the repeat count derived from a supplied
135 noise report (the same derivation the 136 sweep uses) — never hardcoded.
A **real-substrate search refuses to start without a noise report** measured
on that substrate, enforcing 135's go/no-go condition; stub-mode searches
default to the stub measurement's conclusion (repeats = 1) and say so.

**Why this priority**: The issue's gate exists because a naive optimizer
chases noise; noise handling is what makes the loop's comparisons meaningful.

**Independent Test**: Supply noise reports implying different repeat counts;
verify evaluations run that many repeats and the artifact records the source.
Verify a `--real` search without a noise report is refused with an
actionable error.

**Acceptance Scenarios**:

1. **Given** a noise report whose scalar spread implies R repeats, **When**
   an evaluation runs, **Then** R benchmark runs back it and the evaluation's
   score is their mean, with every per-repeat score referenced.
2. **Given** a real-substrate search invoked without a noise report, **Then**
   it refuses to start, naming the missing prerequisite and how to produce it
   (the 135 noise tool on the real substrate).
3. **Given** an evaluation whose runs all fail, **Then** the configuration is
   recorded as failed (not scored), does not become the recommendation, and
   the search continues.

---

### User Story 3 - Convergence trace, honest budget accounting, and the report (Priority: P2)

Every search emits a machine-readable optimizer artifact plus a rendered
report: the full evaluation trace (every proposal in order, with score,
fingerprint, repeats, and what proposed it), best-so-far progression, budget
spent vs cap (evaluations and wall-clock), space coverage (distinct
configurations evaluated vs total), the stop reason, and the recommendation
compared against the 136 baseline and named candidates evaluated under the
same conditions in the same session.

**Why this priority**: "Returns a config" without the trace and honest
accounting is an unauditable claim; the comparison against baseline and
candidates is the issue's outperformance criterion.

**Independent Test**: Run a small search; verify the artifact reconciles
(evaluations = trace length ≤ budget; coverage counts match distinct
fingerprints), the report renders, and baseline + candidates appear in the
comparison table.

**Acceptance Scenarios**:

1. **Given** a completed search, **Then** a versioned, self-validating
   artifact exists (the 134/135/136 conventions) with the full trace, and a
   rendered report beside it.
2. **Given** the recommendation, **Then** the report compares it head-to-head
   with the 136 baseline and every named candidate, all evaluated in-session
   under identical repeats/weights, and states whether it outperformed them
   on the objective.
3. **Given** any capping or dropping (budget stop, failed evaluations,
   unreachable configurations), **Then** the artifact enumerates it — count
   and identity — never silently.

---

### Edge Cases

- Budget smaller than the initial sampling: the search still returns the best
  of what it evaluated, with stop_reason = budget and the shortfall stated.
- Every evaluation fails: no recommendation is emitted; the artifact says so
  explicitly rather than recommending a failed config.
- Ties on the objective: the earliest-evaluated of the tied configurations is
  recommended (deterministic given the seed) and the tie is noted.
- Wall-clock cap hits mid-evaluation: the in-flight evaluation completes (a
  benchmark run is not killed mid-flight), then the search stops; the
  overshoot is reported.
- A candidate list containing the eventual recommendation: the comparison
  table flags the identical fingerprint instead of double-counting evidence.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The optimizer MUST consume a 136 search space (dimensions +
  baseline + candidates) and search over the dimensions' choice combinations;
  everything the space fixes stays fixed.
- **FR-002**: The search MUST run under a hard budget: a maximum evaluation
  count AND a wall-clock cap, both operator-set with sane defaults. The
  budget is a ceiling, never advisory; an in-flight evaluation may complete
  but no new evaluation starts past either cap.
- **FR-003**: The search MUST be adaptive (later proposals depend on earlier
  scores — best-first single-dimension mutation with random restarts) and
  seed-deterministic: identical space + seed + budget + evaluator ⇒ identical
  trace and recommendation.
- **FR-004**: The optimizer MUST memoize by materialized-config fingerprint:
  a configuration is never evaluated twice; cache hits are recorded in the
  trace and do not consume evaluation budget.
- **FR-005**: Evaluation MUST reuse the existing pipeline behind one
  evaluator interface — materialize (136) → run (134, config injected) →
  score (135) × repeats — and the same interface MUST accept an injected
  evaluator so the search algorithm is testable against a synthetic objective
  with a planted optimum (Clarifications fork).
- **FR-006**: Repeats-per-evaluation MUST derive from a supplied 135 noise
  report via 136's derivation (source recorded); a real-substrate search
  without a noise report MUST refuse to start with an actionable error
  (135's go/no-go condition, enforced). Stub searches default to 1 repeat
  per the committed stub measurement, and the artifact says so.
- **FR-007**: A failed evaluation (all repeats failed) MUST be recorded with
  its error, excluded from recommendation, and MUST NOT abort the search.
- **FR-008**: The optimizer MUST emit a versioned, self-validating artifact
  (134/135/136 conventions) plus a rendered report containing: the full
  ordered trace (proposal provenance, score, fingerprint, repeats, cache
  hits), best-so-far progression, budget spent vs caps, space coverage
  (distinct evaluated / total distinct), stop reason, and the recommendation
  with its evidence.
- **FR-009**: The session MUST evaluate the 136 baseline and every named
  candidate under the same conditions and report the recommendation against
  them head-to-head, flagging fingerprint duplicates; "outperforms" is
  decided on the recorded objective and stated honestly (including "no
  signal — tie within noise" when true, as expected on the stub substrate).
- **FR-010**: All accounting MUST reconcile: trace length = budget-charged
  evaluations + cache hits; recommendation ∈ evaluated configs; every
  failed/capped element enumerated. No silent truncation.

### Key Entities

- **Optimizer run**: one budgeted search — space ref, seed, budget, repeats
  + source, evaluator kind (real stub/real/synthetic), trace, recommendation,
  stop reason, coverage.
- **Evaluation record**: one proposed configuration's outcome — overrides,
  fingerprint, provenance (restart seed point / mutation of which parent /
  baseline / candidate), score(s) + component vector, repeats, cache-hit
  flag, or failure.
- **Recommendation**: the winning configuration — overrides, fingerprint,
  evidence, and its head-to-head standing vs baseline/candidates.
- **Optimizer artifact + report**: the versioned machine-readable result and
  the rendered narrative.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Against a synthetic objective with a planted optimum over the
  shipped default space, the optimizer finds the optimum within a budget of
  at most half the space's distinct configurations, in 100% of seeds tested
  (≥ 3 seeds), never exceeding the budget and never re-evaluating a
  fingerprint.
- **SC-002**: Two runs with identical space/seed/budget/evaluator produce
  byte-identical traces and recommendations (timestamps aside).
- **SC-003**: A real end-to-end (stub-substrate) search completes in one
  command, producing an artifact whose accounting reconciles exactly
  (trace = charged + cached; coverage counts match distinct fingerprints)
  and a report containing the baseline/candidate comparison.
- **SC-004**: A `--real` search without a noise report is refused with an
  error naming the 135 prerequisite; with one supplied, the derived repeat
  count and its source appear in the artifact.
- **SC-005**: A committed findings report exists documenting: the synthetic
  convergence evidence, the stub-substrate end-to-end result (honestly
  labeled signal-free), budget accounting, and the standing instruction that
  real recommendations require the real-substrate noise report first.

## Assumptions

- Single scalar objective (the 135 weighted scalar, one weights set per
  search); the component vector is recorded per evaluation so a Pareto view
  is derivable from the trace without re-running (Clarifications).
- Evaluations run sequentially (one daemon per benchmark run, as in 136);
  parallel evaluation is out of scope.
- The search treats dimensions as independent coordinates (mutation = change
  one dimension); conditional/nested spaces are out of scope until a space
  needs them.
- The stub substrate remains signal-free for gate/model dimensions (136
  finding); stub-mode optimizer output is plumbing validation and is labeled
  as such everywhere it appears.
- Candidates and baseline count against the evaluation budget (they are real
  evaluations run in-session for the comparison).
