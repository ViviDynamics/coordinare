# Feature Specification: Board-Simulation Benchmark — Phase 3: Config Search-Space + Sweep Runner

**Feature Branch**: `136-board-bench-sweep`
**Created**: 2026-08-23
**Status**: Draft
**Input**: Issue #184 — make coordinare configuration a first-class sweepable object with a non-adaptive explorer over it: ablation results ("what does each gate/model actually buy you") and a curated-candidate ranking. Phase 3 of the 4-phase benchmark program (134 → 137). Blocked by #183 (landed); blocks #185 (the Phase 4 optimizer, which plugs into this harness).

## Clarifications

### Session 2026-08-23 (solo — decided and recorded, per Mode)

- Q: What counts as one "dimension" in the search space? → A: A named entry binding one coordinare-config setting (a dotted path into the configuration) to an explicit list of allowed values (booleans or enum choices). The space is the set of these entries plus a named baseline; everything not named is fixed at the baseline's value by definition.
- Q: How do materialized config points reach the run, given the 134 substrate never injects configuration? → A: The substrate gains a config-injection seam: the sweep hands each materialized configuration object to the run, which places it where the production daemon places it, so gates and role-model choices actually govern behavior. Runs record the materialized config's own fingerprint (not the baseline file's).
- Q: Stub-mode sweeps can't differentiate most gates (the stub bypasses dispatch) — measure anyway or block on real-performer support? → A: Build the machinery now and let it run in both modes; the report must carry an explicit fidelity caveat naming the mode and, in stub mode, that gate dimensions are exercised structurally (config materialization, run, scoring) but most deltas are expected ~0 until real-performer runs land (134 follow-up). Same recorded-fork pattern as spec 135.
- Q: Repeats per point? → A: Default 1; operator-settable; when a spec-135 noise report is supplied, the runner derives repeats from its scalar spread (enough repeats that the observed stdev of the mean is below a settable resolution threshold, capped), and the report states which source set it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Declare a bounded, validated search space (Priority: P1)

An operator writes a declarative search-space definition: a named baseline
configuration, plus an explicit list of swept dimensions — each one config
setting with its allowed values (the boolean gates such as env-blocked,
baseline-repair, local-test, CI gate; and per-role model/mode choices). The
definition is validated on load: unknown settings, values the configuration
schema rejects, or an unreadable baseline fail loudly before anything runs.
Everything not listed is explicitly fixed at the baseline value — there is no
implicit full cross-product.

**Why this priority**: The search space is the contract every other part
consumes — the sweep runner here and the Phase 4 optimizer later. A wrong or
silently-permissive space poisons everything downstream.

**Independent Test**: Load valid and invalid definitions; verify validation
catches unknown dimension paths, out-of-schema values, and missing baselines,
and that a loaded space enumerates exactly the declared points.

**Acceptance Scenarios**:

1. **Given** a definition naming a baseline and three dimensions with choices,
   **When** it loads, **Then** the space reports exactly those dimensions as
   swept, everything else as fixed, and each dimension's choice list verbatim.
2. **Given** a definition with a dimension path that does not exist in the
   configuration schema, **When** it loads, **Then** loading fails with an
   error naming the bad path.
3. **Given** a dimension value the configuration schema rejects (e.g. a
   non-boolean for a gate), **When** the point is materialized, **Then**
   materialization fails loudly naming the dimension and value — never a
   silently coerced config.

---

### User Story 2 - Run an ablation study (Priority: P1)

From the baseline, the runner varies **one dimension at a time** (each
alternative value of each swept dimension = one point), materializes each
point into a complete concrete configuration, runs it through the Phase 1
substrate, scores it with the Phase 2 scorer, and reports the **marginal
effect of each dimension**: the score delta (scalar and per component) of each
point against the same-sweep baseline run.

**Why this priority**: Ablation — "what does each gate/model actually buy
you" — is the independently valuable deliverable even if Phase 4 never lands.

**Independent Test**: Run an ablation over a small space in stub mode; verify
one run per (dimension, alternative value) plus the baseline, each run's
recorded fingerprint differing per point, and a delta table versus baseline.

**Acceptance Scenarios**:

1. **Given** a space with a baseline and N single-choice-alternative
   dimensions, **When** the ablation runs, **Then** exactly N+1 configurations
   execute (baseline + one per alternative) and the results pair every
   dimension with its delta versus the baseline.
2. **Given** a point whose run or scoring fails, **When** the sweep completes,
   **Then** that point appears in the results as failed with its error — the
   sweep continues, and the summary's coverage line counts it as dropped.
3. **Given** the same sweep re-run with the same space and repeats in stub
   mode, **Then** the scored results are reproducible (per the Phase 2
   determinism guarantees).

---

### User Story 3 - Rank a curated candidate set (Priority: P2)

The operator names a handful of complete candidate configurations (e.g.
conservative / aggressive / cheap-models / premium) in the definition; the
runner executes each end-to-end and produces a head-to-head ranking by scalar
objective, with the full component vector alongside so a reader can see what a
rank costs (tokens, time, harness failures).

**Why this priority**: Head-to-head ranking of whole configs is the second
deliverable of the issue; it reuses everything US2 builds.

**Independent Test**: Define two candidates differing in one gate; run the
comparison in stub mode; verify both ran, both scored, and the ranking table
lists them ordered by scalar with components shown.

**Acceptance Scenarios**:

1. **Given** K named candidates, **When** the comparison runs, **Then** the
   results rank all K by scalar objective (unrankable scores listed last,
   marked) with each candidate's component vector and fingerprint.
2. **Given** two candidates that materialize to the identical configuration,
   **When** the comparison runs, **Then** the report flags the duplicate
   fingerprint rather than presenting them as independent evidence.

---

### User Story 4 - Honest reporting (Priority: P2)

Every sweep produces a machine-readable results artifact plus a human-readable
summary. The summary states: mode (ablation/candidates), substrate mode
(stub/real) with the fidelity caveat, repeats per point and what set that
number, every point that failed or was skipped and why, and — for ablation —
the per-dimension marginal-effect table. If coverage was capped in any way
(sampling, failed points, repeat reduction), the report says exactly what was
dropped. No silent truncation.

**Why this priority**: The honesty rule is an explicit acceptance criterion;
a sweep that quietly drops points produces confidently wrong ablation claims.

**Independent Test**: Force one point to fail in a sweep; verify the artifact
lists it as failed with its error and the summary's coverage section names it.

**Acceptance Scenarios**:

1. **Given** a completed sweep, **Then** a results artifact exists that is
   versioned and self-validating (matching the Phase 1/2 artifact guarantees)
   and a rendered summary exists beside it.
2. **Given** any dropped coverage (failed point, unrankable score, reduced
   repeats), **Then** both the artifact and the summary enumerate it — count
   and identity, not just a count.

---

### Edge Cases

- A dimension whose baseline value equals one of its listed choices: the
  duplicate point is not re-run; the report notes it as coinciding with
  baseline.
- The baseline configuration itself fails validation: the sweep refuses to
  start (nothing runs against an invalid baseline).
- A noise report supplied for repeats derivation that has zero effective
  repeats or a missing scalar spread: the runner falls back to the default
  repeats and says so in the report.
- Repeats > 1: a point's score is the mean over its repeats; the artifact
  keeps every per-repeat score reference so nothing is averaged away silently.
- An empty dimensions list with candidates present is a valid
  candidates-only definition; an empty definition (no dimensions, no
  candidates) fails loudly.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST load a declarative search-space definition
  containing a named baseline configuration, zero or more swept dimensions
  (each a dotted configuration path plus an explicit list of allowed values),
  and zero or more named whole-config candidates; at least one of
  dimensions/candidates MUST be present.
- **FR-002**: Loading MUST validate the definition: the baseline must load as
  a valid coordinare configuration; every dimension path must resolve into the
  configuration schema; every listed value and every candidate must
  materialize into a configuration the schema accepts. Failures are loud and
  name the offending element; nothing runs on a partially valid space.
- **FR-003**: The space MUST be explicitly bounded: dimensions listed are
  swept, everything else is fixed at the baseline value, and the enumerated
  point count is reported before running. No implicit cross-product is ever
  generated.
- **FR-004**: Materializing a point MUST produce a complete concrete
  configuration object (baseline + that point's overrides) whose own content
  fingerprint is recorded with the run — two distinct points MUST have
  distinct fingerprints, and identical materializations MUST be detected and
  flagged.
- **FR-005**: The substrate MUST gain a config-injection seam so a
  materialized configuration actually governs the run (placed where the
  production daemon places it); runs without an injected configuration keep
  today's behavior unchanged.
- **FR-006**: The runner MUST support ablation mode: baseline once, plus one
  run per (dimension, non-baseline value), with per-dimension deltas (scalar
  and per component) computed against the same sweep's baseline score.
- **FR-007**: The runner MUST support candidate mode: run each named
  candidate and rank by scalar objective, listing unrankable scores last and
  marked, each with its component vector.
- **FR-008**: The runner MUST support repeats-per-point: default 1,
  operator-settable, or derived from a supplied Phase 2 noise report's scalar
  spread (with a stated cap and a stated resolution threshold); the source of
  the repeat count MUST be recorded. With repeats > 1 a point's headline score
  is the mean and every per-repeat score is referenced in the artifact.
- **FR-009**: A point whose run or scoring fails MUST NOT abort the sweep: it
  is recorded as failed with its error, excluded from deltas/ranking, and
  counted as dropped coverage.
- **FR-010**: Every sweep MUST emit (a) a versioned, self-validating results
  artifact (matching the Phase 1/2 artifact conventions) and (b) a rendered
  human-readable summary, both including: mode, substrate mode with the
  stub-fidelity caveat when applicable, repeats and their source, per-point
  scores/fingerprints, ablation deltas or candidate ranking, and an explicit
  coverage section enumerating everything dropped or capped. No silent
  truncation (SC-004).
- **FR-011**: All new capability MUST extend the existing benchmark surface
  (the Phase 1 substrate and Phase 2 scorer are consumed, not reimplemented;
  artifacts follow the same schema conventions); the shipped default search
  space MUST document the known gate dimensions (env-blocked, baseline-repair
  set, local-test, CI gate) and per-role mode choices as its swept set.

### Key Entities

- **Search-space definition**: named baseline + swept dimensions (path +
  choices) + named candidates; validated on load; everything unlisted is
  fixed.
- **Config point**: one concrete configuration to run — (dimension, value)
  override in ablation, or a named candidate; carries its materialized
  fingerprint.
- **Sweep result**: per-point outcome — score(s), fingerprint, repeat refs,
  or failure record.
- **Sweep artifact + summary**: the versioned machine-readable results plus
  the rendered report with deltas/ranking and the coverage/honesty section.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can run a complete ablation study over the shipped
  default space in stub mode with one command, and every declared point either
  produced a scored run or is enumerated as failed — 100% of points accounted
  for.
- **SC-002**: For a space with N dimensions of one alternative each, the
  ablation executes exactly N+1 runs and reports N marginal-effect rows, each
  traceable to two concrete fingerprints (point vs baseline).
- **SC-003**: A curated comparison of K candidates yields a strict ranking by
  scalar (ties and unrankables explicitly marked) with component vectors
  shown, in one command.
- **SC-004**: Zero silent truncation: for any sweep where a point failed or
  coverage was capped, the summary names each dropped element; a reader can
  reconcile declared points vs scored points vs dropped points exactly.
- **SC-005**: The Phase 4 optimizer (spec 137) can consume the search space
  and per-point results without reading Phase 3 internals: space, points,
  scores, and fingerprints are documented in contracts.
- **SC-006**: Injected configuration demonstrably governs a run: at least one
  test proves a materialized config point changes runtime behavior versus
  baseline (not merely the recorded fingerprint).

## Assumptions

- **Substrate fidelity (recorded fork)**: stub-mode sweeps exercise the full
  machinery (materialize → inject → run → score → report) but the stub
  bypasses dispatch, so most gate dimensions are expected to show ~0 delta
  until real-performer runs land (134's deferred follow-up). The report
  carries this caveat whenever substrate mode is stub. Same pattern as spec
  135's noise finding.
- The scalar objective, weights, and score comparability rules are Phase 2's
  (weights embedded; only same-weight scalars compared). A sweep uses one
  weights set for all its points.
- Sweeps run points sequentially (the substrate builds one daemon per run);
  parallel execution is a Phase 4 concern if ever needed.
- The shipped default search space is documentation-plus-data (a committed
  definition file), not new gate behavior: dimension paths reference existing
  configuration settings only.
- Blocked-recovery (spec 129) is env-var-gated rather than configuration-file
  gated today, so it is documented as out of the sweepable set until it moves
  into configuration.
