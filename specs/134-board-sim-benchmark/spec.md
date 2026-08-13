# Feature Specification: Board-Simulation Benchmark — Phase 1 (evaluation substrate)

**Feature Branch**: `134-board-sim-benchmark`
**Created**: 2026-07-23
**Status**: Draft
**Input**: User description: "Run the real coordinare daemon end-to-end against a simulated GitHub board, driving fresh cards through the whole lifecycle with real performers, real local git, and real pytest CI, faking only the GitHub API, and emit a structured run artifact."

## Overview

This is Phase 1 of a four-phase config-optimization benchmark program (Spec 134 →
135 scoring → 136 config sweep → 137 optimizer). Phase 1 builds the **evaluation
substrate**: the ability to run coordinare over a fake board end-to-end, once, under
one config, and emit a complete run artifact. It does **not** score, sweep, or
optimize — it produces the raw material those later phases consume.

The program optimizes *outcome quality*. Outcome quality is only observable if the
personas produce real good/bad code that gets really tested. So the design fakes
**only the GitHub API**; everything below it stays real — performers dispatch real
models, git is a real local repository, and CI is a real test run whose result is
reported back in the exact shape coordinare already reads.

## Landed scope (Phase 1)

The substrate (Protocol, `FakeGitHubService`, runner, run artifact, tests, and the
conductor-bench fixture-map truing) lands here, verified end-to-end with a **stubbed
performer** — which the ticket sanctions for the integration test. Driving cards with
**real performers** (acceptance criterion 3) is **deferred to a scoped follow-up**:
a real performer creates its PR over the network itself (HTTPS clone/push + REST
`POST /pulls`), so it does not touch the in-process `FakeGitHubService` — faking
GitHub for it needs a separate performer-boundary HTTP surface. See
[real-performer-followup.md](./real-performer-followup.md) for the code-grounded
finding, requirements, and the open PM question.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run a config end-to-end and get an artifact (Priority: P1)

A coordinare maintainer (or a downstream automated harness for specs 135–137) picks
one coordinare configuration and one set of seeded work items, runs the benchmark,
and receives a single structured artifact describing exactly what happened: every
card's final outcome, the personas that were dispatched, the gate decisions, the CI
results, merge status, timing, and estimated cost.

**Why this priority**: This is the whole point of the phase — the atom every later
phase stacks on. Without it, nothing in 135/136/137 can be built. It is the MVP.

**Independent Test**: Seed one trivial work item, run the benchmark under one
config, and confirm the run reaches a terminal state and emits an artifact that
validates against the documented schema.

**Acceptance Scenarios**:

1. **Given** a valid config and one seeded work item, **When** the benchmark is
   run, **Then** the item is driven through the lifecycle to a terminal outcome and
   a schema-valid run artifact is written.
2. **Given** a run that cannot make progress (a stuck or non-mergeable item),
   **When** a time/cycle budget is exceeded, **Then** the run still terminates and
   still emits a complete artifact, with that item recorded as a non-merge terminal
   outcome (never an unbounded hang).
3. **Given** a completed run, **When** the artifact writer finishes, **Then** the
   run is only declared complete after the artifact has been validated against the
   schema.

### User Story 2 - Faithfully simulate the board without touching real GitHub (Priority: P1)

The benchmark drives the **real** coordinare daemon, but coordinare must never reach
live GitHub. A simulated board stands in for the GitHub API and models the board
columns, branches, pull requests, reviews, CI checks, comments, and merges in
process — returning data in the exact shapes coordinare already consumes, and
mutating its own state as coordinare calls it.

**Why this priority**: The daemon only makes sound decisions if the simulated API
returns faithful shapes; an unfaithful stand-in silently diverges and the whole
substrate becomes untrustworthy. Equal-priority with US1 because US1 cannot produce
a trustworthy artifact without it.

**Independent Test**: Exercise the simulated board directly (no daemon): create a
branch/PR, run CI, approve, merge — and confirm each read returns the same shape the
real service returns and that a merge is reflected truthfully in subsequent reads.

**Acceptance Scenarios**:

1. **Given** the simulated board, **When** coordinare polls it, **Then** it receives
   the same board-snapshot shape the real service returns.
2. **Given** a PR whose head has a passing test suite, **When** coordinare reads the
   CI check status, **Then** it sees the required check(s) reported as passing in the
   exact check shape it already reads.
3. **Given** an approved, mergeable PR, **When** coordinare merges it, **Then** the
   merge is performed truthfully so later mergeability/branch reads reflect it.
4. **Given** any call coordinare makes, **When** the simulated board handles it,
   **Then** it never raises — it degrades to safe defaults — so a fake failure never
   aborts the daemon loop.

### User Story 3 - Reach a merge without a human, via a pluggable approval policy (Priority: P2)

Coordinare only merges on human approval, but a headless benchmark has no human. A
pluggable approval policy stands in: the default policy approves a PR once
coordinare's own quality gates and CI are green, so cards can reach a terminal merged
state. The policy is a swap point so a later phase can substitute a
ground-truth-aware ("oracle") approver.

**Why this priority**: Without it, no card ever reaches merge and every run ends in a
non-merge terminal — the substrate would exercise only part of the lifecycle. P2
because a run still terminates and still emits an artifact without it (US1 holds).

**Independent Test**: Drive a PR to green gates + green CI and confirm the default
policy withholds approval until both conditions hold, then approves — after which
coordinare sees an approving human review and the merge path proceeds.

**Acceptance Scenarios**:

1. **Given** a PR with incomplete gates, **When** the approval policy is consulted,
   **Then** it withholds approval.
2. **Given** a PR with all gates and CI green, **When** the approval policy is
   consulted, **Then** it approves, and coordinare subsequently sees an approving
   review from a configured reviewer identity.

### User Story 4 - Seed whole-lifecycle work items and keep the fixture map honest (Priority: P3)

The benchmark seeds fresh, from-scratch work items (not mid-pipeline PRs) so
coordinare drives each through the entire lifecycle. A small set of self-contained
fixtures — each with a clear task, a real test suite expressing acceptance, and a
planted ground-truth marker for later scoring — is authored in the sibling
benchmark repository, and its fixture map is trued up to match what actually exists.

**Why this priority**: Needed for realistic runs and to fix known fixture-map drift,
but the P1 MVP only needs one trivial fixture; the broader set and the map cleanup
are follow-on. P3.

**Independent Test**: Load the fixture manifest, seed the board, and confirm the
seeded items appear in the backlog column via a board poll.

**Acceptance Scenarios**:

1. **Given** a fixture manifest, **When** the benchmark seeds the board, **Then**
   each fixture's work item appears in the backlog column with its task body and
   ground-truth marker.
2. **Given** the sibling benchmark repository, **When** the fixture map is reviewed,
   **Then** every planted branch/card on origin has a corresponding, accurate map
   entry (no drift).

### Edge Cases

- **Stuck / looping item**: a real model can loop, stall, or produce an
  un-mergeable PR. The run must be bounded by a hard time and cycle budget so the
  loop always closes; the item is recorded with a non-merge terminal outcome.
- **Non-readable CI signal**: when required-check information cannot be resolved,
  the simulated board returns the same "unresolved" signal the real service does, so
  coordinare's existing fallback path is exercised rather than a crash.
- **Fake raising an exception**: an exception thrown by the simulated board would
  abort the daemon loop rather than terminate cleanly; the fake must degrade to safe
  defaults on every path.
- **Card that never reaches a terminal column** before budget exhaustion is recorded
  as `abandoned`; an unhandled error is recorded as `error`. Every card gets a
  terminal outcome in the artifact.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The benchmark MUST drive the real coordinare lifecycle end-to-end,
  with only the GitHub API replaced by an in-process simulation; performers, git,
  and CI MUST be real.
- **FR-002**: A documented contract MUST capture the full surface the coordinare
  lifecycle depends on from the GitHub service, and the real service MUST satisfy it
  with no behavior change (the existing test suite stays green).
- **FR-003**: An in-process simulated board MUST implement that same contract,
  modeling board columns, branches, pull requests, reviews, CI checks, comments, and
  merges, returning data in the shapes coordinare already consumes.
- **FR-004**: The simulated board MUST back its git-reading operations with a real
  local repository so diffs, file reads, branch existence, and post-merge reads are
  truthful.
- **FR-005**: CI MUST be computed by running the fixture's real test suite against
  the PR head and reported in the exact check-status shape coordinare reads.
- **FR-006**: A merge MUST be performed truthfully against the local repository so
  subsequent mergeability and branch reads reflect the merged state.
- **FR-007**: A pluggable approval policy MUST allow a PR to reach an approved,
  mergeable state without a human; the default policy MUST approve only once the
  coordinare quality gates and CI are green.
- **FR-008**: The benchmark MUST seed fresh, whole-lifecycle work items from a
  fixture manifest into the simulated board's backlog.
- **FR-009**: The run MUST be bounded by a hard time budget and cycle budget so it
  always terminates; every seeded card MUST receive a terminal outcome
  (`merged`, `blocked`, `abandoned`, or `error`).
- **FR-010**: The run MUST emit exactly one structured artifact per run capturing,
  per card: final outcome, the personas dispatched, gate decisions, CI results,
  merge status, timing, and estimated cost — plus run-level totals.
- **FR-011**: The artifact MUST carry a schema version and MUST be validated against
  its schema before the run is declared complete.
- **FR-012**: Cost in the artifact MUST be an estimate derived from the token counts
  coordinare already tracks (token count × configured price rate) and MUST be flagged
  as an estimate; authoritative per-request cost is out of scope for this phase.
- **FR-013**: The benchmark MUST tear down cleanly after a run — stopping the
  daemon, closing the simulation, removing ephemeral performer containers, and
  deleting temporary repositories and scratch.
- **FR-014**: The phase MUST NOT introduce scoring, noise measurement, config
  sweeping, or optimization; the artifact is raw material only.
- **FR-015**: The sibling benchmark repository's fixture map MUST be trued up to
  match origin — including the new whole-lifecycle fixtures and the two currently
  undocumented planted branches.

### Key Entities

- **GitHub service contract**: the documented set of operations the coordinare
  lifecycle depends on; the single source of truth both the real service and the
  simulation satisfy.
- **Simulated board**: the in-process stand-in holding, per run, the board columns,
  branches, PRs, reviews, CI states, comments, and merges, plus a recorded log of
  every mutating call (the artifact's raw material).
- **Approval policy**: a swappable decision that determines when a PR becomes
  approved/mergeable in the absence of a human reviewer.
- **Fixture**: a self-contained work item — task description, a from-scratch repo
  state, a real test suite expressing acceptance, and a planted ground-truth marker
  for later scoring.
- **Run artifact**: the structured, versioned, schema-validated record of one run —
  per-card outcomes (dispatches, gate decisions, CI results, merge, timing, cost)
  plus run-level totals.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A single benchmark run over at least one seeded work item completes
  and produces exactly one artifact that validates against its schema.
- **SC-002**: 100% of runs terminate within their configured time/cycle budget —
  no run hangs — and 100% of seeded cards receive a terminal outcome in the artifact.
- **SC-003**: Adding the contract and the simulation causes zero regressions in the
  existing test suite (the real GitHub service's behavior is unchanged).
- **SC-004**: The simulation's reads for board, CI checks, reviews, mergeability, and
  merge match the shapes the real service returns, verified by isolated tests of each
  operation group.
- **SC-005**: A card whose fixture is satisfiable can reach a `merged` terminal
  outcome purely through the daemon's normal flow plus the default approval policy —
  no manual intervention.
- **SC-006**: The one automated end-to-end test runs deterministically and at no
  model cost (stubbed performer), proving the loop closes and the artifact validates.
- **SC-007**: The sibling repository's fixture map has no drift — every planted
  branch/card on origin has an accurate entry.
