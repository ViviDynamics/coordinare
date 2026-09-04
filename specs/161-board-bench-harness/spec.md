# Feature Specification: Rank Backend Harnesses Per Role

**Feature Branch**: `161-board-bench-harness`
**Created**: 2026-09-04
**Status**: Draft
**Closes**: #248
**Extends**: Spec 134 (benchmark substrate), 135 (scoring), 136 (sweep), 151 (real performers)
**Input**: Rank backend harnesses per role in the board-simulation benchmark. Add a backend sweep dimension and a per-role, per-backend harness-comparison scoring view in which harness defects count against the harness, alongside the existing config-comparison view.

## Overview

The inference fleet now offers exactly one self-hosted model, so every coordinare
role runs the same model. With the model held constant, the **backend harness** is
the only remaining variable between roles, and the current role-to-harness mapping
was chosen for a retired fleet. It is provisional and unjustified.

The benchmark program (134-137) plus real performers (151) can already drive a
simulated board to terminal states and score it. It cannot answer *"which harness is
best for each role"* because nothing varies the harness and nothing attributes a
result to a role.

This spec closes both gaps and produces a ranked per-role harness recommendation. It
**extends** the existing bench architecture; it does not introduce a parallel stack.

## Clarifications

### Session 2026-09-04

- Q: Should a harness defect count against the harness, given spec-135 FR-005
  deliberately quarantines harness failure so optimization is never steered by it?
  → A: **Add a new harness-comparison view** in which defects count against the
  harness, kept alongside the existing config-comparison view. Existing scalars keep
  their present meaning, so fixed-harness config sweeps are unaffected.
- Q: Which model do harness comparisons run on? → A: The single self-hosted model the
  gateway serves. The model dimension is **fixed** for this work. No cloud models.
- Q: Is new per-dispatch instrumentation needed? → A: **No.** The run artifact's
  per-dispatch record already carries stage, role, backend, model, status, terminal
  marker, duration and tokens.
- Q: What shape does the harness-comparison objective take? → A: The **same shape as
  the existing objective** (a credit term minus normalized time and token penalties,
  with the weights embedded in the result), substituting a harness credit rate for
  card correctness. Decided solo to keep the two views structurally comparable and to
  avoid inventing a second scoring idiom.
- Q: How is evidence combined when a role-harness pair appears in several runs?
  → A: **Both**, for two different purposes. Pool the conclusive dispatches and compute
  the *rate* over the pooled total (more stable than averaging per-run rates when
  per-run counts are small), **and separately retain the per-run scalars** so the
  spread across runs is still available. Decided solo after analysis caught that
  pooling alone collapses the sample to a single value, leaving the tie rule with no
  variance to work from and silently degrading it to exact equality.
- Q: When are two harnesses a tie rather than ranked? → A: When the absolute
  difference of their means is **within the sum of their standard deviations**,
  computed over the **per-run scalars** (not the pooled row) using the existing repeat
  statistics. Decided solo to reuse the existing noise machinery rather than define a
  new statistical test. With a single contributing run the deviation is zero, so only
  exactly equal scores tie, and the ranking must say so rather than implying a
  measured tie.
- Q: How much evidence must a role-harness pair carry to be rankable? → A: At least
  **three conclusive dispatches**, configurable. Below that the pair reports
  insufficient evidence. Decided solo: a rate over one or two dispatches is not
  meaningfully distinguishable from noise.
- Q: Where does ranking output live? → A: Alongside the artifact it describes, matching
  the existing convention of writing the score next to the run it scores: a per-run
  rollup in the run directory, and a multi-run ranking at the sweep root. Decided solo
  for consistency with the established layout.

## The central distinction this feature rests on

Grading today classifies a **card** as a harness failure only when it ends in `error`
or records no dispatches. That is a coarse infrastructure signal. It is not a measure
of harness *quality*.

Worse, the per-dispatch status mapping collapses every non-success terminal marker
into a single `failed` value. That bucket mixes two opposite things:

| Terminal marker | What it means | Ranking treatment |
| --- | --- | --- |
| `pr_opened`, `plan_committed`, `approved`, `security_passed`, `qa_passed`, `docs_committed`, `assessment_complete` | The role did its job | **Credit** |
| `changes_requested`, `qa_failed`, `security_failed`, `blocked` | The role rendered a legitimate negative verdict | **Credit** (not a defect) |
| `malformed_output`, `system_error` | The harness could not produce usable output | **Penalty** |
| `env_blocked` | The environment failed, not the harness or model | **Excluded** |
| absent marker | Never reached terminal; budget or teardown cut it off | **Excluded as inconclusive** |

A reviewer that correctly rejects a bad pull request emits `changes_requested`. Ranking
on the collapsed `failed` status would **penalize that reviewer for being right**. The
terminal marker is retained in the artifact, so the distinction is recoverable, and
recovering it is the core of this feature.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - See which harness is failing which role (Priority: P1)

An operator has one run artifact and wants to know, per role, which harness produced
usable output and which one broke. Today the whole-board scalar cannot tell them.

**Why this priority**: This is the smallest slice that delivers real value, and it
works on **recorded** artifacts with no new model calls and no healthy inference host.
Where no recorded artifact exists yet, one is produced by the existing stub runner,
which needs no inference host either. It is the foundation the other stories build on.

**Independent Test**: Point the rollup at an existing run artifact and confirm it
emits a per-role, per-backend table separating credit, defect, environment and
inconclusive outcomes.

**Acceptance Scenarios**:

1. **Given** a run artifact with dispatches for several roles, **When** the per-role
   rollup runs, **Then** it emits one row per (role, backend) with counts for credited
   outcomes, harness defects, environment failures and inconclusive dispatches.
2. **Given** a dispatch whose terminal marker is `changes_requested`, **When** the
   rollup classifies it, **Then** it is credited and **not** counted as a defect.
3. **Given** a dispatch whose terminal marker is `malformed_output`, **When** the
   rollup classifies it, **Then** it is counted as a harness defect.
4. **Given** a dispatch whose terminal marker is `env_blocked`, **When** the rollup
   classifies it, **Then** it is excluded from both credit and defect totals.
5. **Given** a dispatch with no terminal marker, **When** the rollup classifies it,
   **Then** it is excluded as inconclusive rather than counted as a defect.

---

### User Story 2 - Vary the harness in a sweep (Priority: P2)

An operator wants a sweep to vary which harness serves a role, so that two sweep
points differ only by harness and become comparable.

**Why this priority**: Without this there is nothing to compare. It ranks below US1
because US1 already extracts value from existing artifacts.

**Independent Test**: Declare a harness dimension for one role, expand the sweep, and
confirm the generated points differ in that role's harness and that each point's
configuration resolves.

**Acceptance Scenarios**:

1. **Given** a search space declaring a harness dimension for a role, **When** the
   sweep expands, **Then** it produces one point per declared harness with that role's
   harness overridden and all other settings held fixed.
2. **Given** such a point, **When** its configuration is materialized, **Then** the
   role's harness is the swept value and the configuration passes validation.
3. **Given** a harness that cannot serve a role, **When** that point runs, **Then** the
   failure is attributed to that harness as a defect rather than aborting the sweep.

---

### User Story 3 - Rank harnesses and get a recommendation (Priority: P3)

An operator wants a ranked recommendation per role, with ties and thin evidence called
out, so they can update the live configuration with justification.

**Why this priority**: The payoff, but it depends on US1 and US2 and on a healthy
inference host, so it is last.

**Independent Test**: Feed several scored runs covering multiple harnesses per role and
confirm a ranked table with an explicit winner or a declared tie per role.

**Acceptance Scenarios**:

1. **Given** scored runs covering two or more harnesses for a role, **When** the
   ranking runs, **Then** each role lists harnesses ordered by the harness-comparison
   objective.
2. **Given** two harnesses whose scores fall within the noise band, **When** the
   ranking runs, **Then** it reports a tie rather than an arbitrary winner.
3. **Given** a role with dispatches from only one harness, **When** the ranking runs,
   **Then** it reports insufficient evidence rather than declaring that harness best.
4. **Given** a ranking result, **When** an operator reads it, **Then** each recommended
   harness cites the evidence behind it (dispatch counts and the defect split).

---

### Edge Cases

- A role never dispatched in a run: reported as no evidence, never as a zero score.
- A harness whose dispatches are all inconclusive: no defect rate is claimed; the row
  is marked unusable.
- Every harness for a role fails: the role reports no viable harness rather than
  ranking the least-bad one as a winner.
- A run whose artifact schema version is unknown: refuse to grade rather than
  misinterpret it, matching existing scoring behavior.
- A retry that eventually succeeds: the defect is still recorded, because a harness
  needing retries is worse than one that does not.
- A single dispatch appearing under multiple stages: counted once per (role, backend).
- Existing artifacts predating this feature: must roll up without re-running.

## Requirements *(mandatory)*

### Functional Requirements

**Classification**

- **FR-001**: The system MUST classify every per-dispatch terminal marker into exactly
  one of: credited outcome, harness defect, environment failure, or inconclusive.
- **FR-002**: The system MUST credit legitimate negative role verdicts
  (`changes_requested`, `qa_failed`, `security_failed`, `blocked`) rather than counting
  them as harness defects.
- **FR-003**: The system MUST count `malformed_output` and `system_error` as harness
  defects.
- **FR-004**: The system MUST exclude `env_blocked` from both credit and defect totals.
- **FR-005**: The system MUST exclude dispatches lacking a terminal marker as
  inconclusive, and MUST report the inconclusive count so a thin result is visible.
- **FR-006**: The system MUST treat an unrecognized terminal marker as inconclusive and
  surface it, never silently as credit or defect.

**Per-role rollup**

- **FR-007**: The system MUST aggregate dispatches by (role, backend) from a run
  artifact, deriving all counts from already-recorded per-dispatch fields with no new
  instrumentation.
- **FR-008**: Each rollup row MUST report dispatch count, credited count, harness-defect
  count, environment count, inconclusive count, defect rate over conclusive dispatches,
  and observed latency and token totals.
- **FR-009**: The rollup MUST operate on previously captured artifacts without
  re-running a board.

**Harness-comparison scoring view**

- **FR-010**: The system MUST provide a harness-comparison view in which harness defects
  count against the harness.
- **FR-011**: The harness-comparison view MUST be additive: for a fixed-harness sweep the
  existing config-comparison **scalar values and component values MUST be numerically
  unchanged**, preserving spec-135 FR-005. This is a guarantee about computed values, not
  about serialized bytes: adding a view discriminator necessarily changes the serialized
  score document, so the score schema version MUST be bumped and the discriminator MUST
  default to the config-comparison view for every previously-written score.
- **FR-012**: Each emitted score MUST identify which view produced it, so the two are
  never compared to each other.
- **FR-013**: A harness-comparison score MUST be withheld (not defaulted to zero) when a
  (role, backend) pair has no conclusive dispatches.

**Sweep dimension**

- **FR-014**: The search space MUST support a dimension that varies a named role's
  harness across declared choices.
- **FR-015**: Expanding such a dimension MUST override only that role's harness and hold
  all other settings fixed.
- **FR-016**: Each generated point's configuration MUST pass configuration validation
  before the point runs.
- **FR-017**: The system MUST reject at declaration time a harness dimension naming an
  unknown role or an unknown harness, rather than failing mid-sweep.
- **FR-018**: A harness that fails for a role MUST be recorded in that point's run
  artifact as a failed dispatch attributable to that harness, and the sweep MUST continue
  to the remaining points. Classifying that failure as a harness defect rather than a
  legitimate negative verdict is the rollup's job (FR-001 through FR-003), so this
  requirement is satisfiable without the classifier and keeps the sweep story
  independently shippable.

**Ranking**

- **FR-019**: The system MUST emit a per-role ranking of harnesses ordered by the
  harness-comparison objective.
- **FR-020**: The ranking MUST declare a tie when the absolute difference between two
  candidates' mean scores is within the sum of their standard deviations, computed over
  the per-run scalars retained per FR-025, rather than presenting an arbitrary winner.
  When only one run contributes, the deviation is zero and the result MUST be reported
  as single-run (no measured spread) rather than as a confident ranking.
- **FR-021**: The ranking MUST report insufficient evidence for any role with fewer than
  two harnesses carrying conclusive dispatches, and for any role-harness pair carrying
  fewer than a configurable minimum of conclusive dispatches (default three).
- **FR-022**: Each ranking entry MUST cite its supporting evidence (dispatch counts and
  the defect split).
- **FR-023**: The ranking MUST be advisory output only. It MUST NOT write live
  deployment configuration.
- **FR-024**: The harness-comparison objective MUST take the same structural form as the
  existing objective (a credit term less a normalized cost penalty and a normalized time
  penalty) with its weights embedded in the emitted result, so that results are only
  compared across matching weights. The cost penalty MUST be expressed in the same unit as
  the cost budget it is divided by: token counts MUST be converted to an estimated cost
  using the existing estimator before division, never divided by a currency budget
  directly. When no token count is available the cost term MUST be omitted and the
  omission recorded, never treated as zero cost.
- **FR-025**: When a role-harness pair appears in more than one run, the system MUST pool
  its conclusive dispatches and compute rates over the pooled total, MUST record how many
  runs contributed, and MUST additionally retain the per-run scalars so that run-to-run
  spread remains computable for FR-020. Pooling alone MUST NOT be the only retained form,
  because it collapses the sample and leaves the tie rule without variance.
- **FR-026**: Ranking output MUST be written alongside the artifact it describes: a
  per-run rollup in that run's directory, and a multi-run ranking at the sweep root.
- **FR-027**: When every candidate harness for a role has a credit rate of zero, the
  ranking MUST report that no viable harness was found rather than ranking the least-bad
  candidate as a winner.

### Key Entities

- **Dispatch outcome class**: The four-way classification of one dispatch (credited,
  harness defect, environment, inconclusive), derived from its terminal marker.
- **Role-harness rollup row**: One (role, backend) pair's aggregated evidence from a run:
  counts per outcome class, defect rate, latency, tokens.
- **Scoring view**: A named objective identifying whether harness defects are penalized
  (harness comparison) or quarantined (config comparison). Carried on every score so
  scores are only compared within a view.
- **Harness dimension**: A search-space dimension varying one role's harness across
  declared choices.
- **Harness ranking**: The ordered per-role result, with ties, evidence citations, and
  insufficient-evidence markers.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For any run artifact, an operator can obtain a per-role, per-backend table
  distinguishing all four outcome classes, without re-running the board.
- **SC-002**: A legitimate negative role verdict never appears as a harness defect,
  verified by a case for each such marker.
- **SC-003**: Two sweep points differing only in one role's harness produce
  independently attributable results for that role.
- **SC-004**: For a fixed-harness sweep, config-comparison scalars are identical to those
  produced before this feature, demonstrated by a regression check.
- **SC-005**: Every role in a completed comparison receives a ranked recommendation, a
  declared tie, or an explicit insufficient-evidence result. No role is silently absent.
- **SC-006**: Every recommendation is traceable to the dispatch counts behind it.

## Assumptions

- The single self-hosted gateway model is used for all harness comparisons. The gateway's
  served model list is read-only to this work; it is never modified.
- The terminal-marker vocabulary is the source of truth for classification. New markers
  appear as inconclusive (FR-006) until explicitly classified.
- Per-dispatch records are sufficient for attribution, so no new instrumentation is
  added.
- Ranking consumes runs produced by the existing real-performer path; this feature does
  not change how performers are dispatched.
- Noise-band width for tie detection reuses the existing repeat and noise machinery
  rather than defining a new statistical method.

## Dependencies

- The benchmark substrate, scoring, sweep and real-performer capabilities already
  merged (specs 134, 135, 136, 151).
- The per-dispatch record in the run artifact, including its terminal marker.
- **External blocker, out of scope to fix here**: real-performer runs require a healthy
  self-hosted inference host. The host is currently unresponsive. US1, US2 and all
  classification, rollup, scoring-view and sweep work are testable without it; only US3's
  live ranking run depends on it.

## Out of Scope

- Modifying the inference gateway deployment or its served model list.
- Cloud model backends.
- Varying the model. The model is fixed for this work.
- Changing the meaning of existing config-comparison scalars.
- Automatically writing the winning harnesses into live deployment configuration
  (FR-023); applying the recommendation stays a human decision.
- Fixing any harness defect this benchmark reveals. Measuring is in scope, repairing is
  not.
