# Implementation Plan: Rank Backend Harnesses Per Role

**Branch**: `161-board-bench-harness` | **Date**: 2026-09-04 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/161-board-bench-harness/spec.md`
**Closes**: #248

## Summary

Make the board-simulation benchmark able to rank backend harnesses per role, now that
the model is fixed and the harness is the only variable left.

The technical approach is deliberately small because the substrate already exists. The
run artifact already records `role`, `backend` and `terminal_marker` per dispatch, and
the sweep's override path already reaches `performers.<role>.backend` (verified by
execution). So this feature is three additive pieces:

1. A **classifier** over `PersonaDispatch.terminal_marker` producing a four-way outcome
   class, and a **rollup** aggregating by `(role, backend)`. Reads existing artifacts;
   needs no live host. (US1, P1)
2. A **harness dimension** for the search space, whose real work is rejecting unknown
   harness names at declaration time (the schema currently accepts any string). (US2, P2)
3. A **harness-comparison scoring view** plus a **ranking** with tie and
   insufficient-evidence handling. (US3, P3)

The load-bearing design decision: classification MUST read `terminal_marker`, never the
`status` field. `runner.py::_dispatch_status` collapses every non-success marker to
`"failed"`, which merges a reviewer correctly rejecting bad code (`changes_requested`)
with a genuine harness defect (`malformed_output`). Ranking on `status` would penalize
correct behavior. The raw marker is preserved, so the distinction is recoverable.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: existing only — pydantic 2.x (rollup/ranking models),
`coordinare.bench` (artifact, score, grader, space, sweep, noise),
`coordinare.graph.nodes.monitor_performer.TERMINAL_SUCCESS_STATES` (credit set source of
truth). **No new external dependencies.**
**Storage**: N/A for coordinare state. Outputs are files beside the artifacts they
describe: a per-run rollup in the run dir, a multi-run ranking at the sweep root
(FR-026). No `state_store.py` change. The only schema movement is a
`SCORE_SCHEMA_VERSION` bump for the additive view discriminator (FR-011); existing score
documents load with the config-comparison default.
**Testing**: pytest. `tests/unit/test_161_*.py`, matching the established
`test_<NNN>_<topic>.py` convention (`tests/benchmarks/` is perf-only and is not the home
for this).
**Target Platform**: macOS + Linux for US1/US2 (pure artifact analysis). US3's live run
uses the 151 real-performer path, portable across Docker Desktop and native Linux.
**Project Type**: single
**Performance Goals**: Rollup is O(dispatches) over a local JSON artifact; no target
beyond "not perceptibly slow on a normal run artifact".
**Constraints**: Additive only. For a fixed-harness sweep the existing config-comparison
scalar and component **values** MUST be numerically unchanged (FR-011), enforced by a
regression test. This is a value guarantee, not byte-identity: adding the view
discriminator changes the serialized score document, so `SCORE_SCHEMA_VERSION` is bumped
and the discriminator defaults to `CONFIG_COMPARISON`.
**Scale/Scope**: ~7 harnesses x ~10 roles. Artifacts hold tens to low hundreds of
dispatches per run.

## Constitution Check

Evaluated against `.specify/memory/constitution.md` v1.1.0.

| Gate | Status | Notes |
| --- | --- | --- |
| 1. Lint & Format | Planned | `make lint` (ruff over src/ and tests/); `make fmt` for fixes. |
| 2. Type Check | Planned | mypy is configured in pyproject; new modules are fully annotated. |
| 3. Unit Tests | Planned | `make test`. No skipped tests introduced. |
| 4. Integration Tests | Planned | `make test-all` (unit + contract) before PR. |
| 5. Coverage Check | Planned | pytest-cov configured; new modules ship with tests, so coverage does not regress. |
| 6. Performance Check | N/A | Not a performance-critical path; no benchmark budget applies to offline artifact analysis. |
| 7. Accessibility Check | N/A | No UI change. |
| 8. Code Review (one approving review from a non-author) | **DEVIATION** | See below. |

**Gate 8 deviation, stated honestly**: this is a solo-authored change and the requester
has directed solo mode, so a non-author human approval is not available. It is
substituted with the mandatory adversarial multi-lens `Workflow` review over the full
branch diff, with a refute-oriented skeptic pass, per the repo's standing review rule.
That is a substitution, not an equivalent, and it is recorded here rather than quietly
skipped. Merge is by the requester's explicit instruction (merge when CI is green).

**Principle V (Clarity Before Action)**: the two forks that materially changed scope
(harness-defect semantics, model scope) were resolved with the requester before drafting.
Five further ambiguities were resolved solo and recorded in the spec's Clarifications.

**Constitution re-check after design**: no new violations. The design adds modules
alongside existing ones and changes no existing scalar semantics, which is what keeps
Gate 5 and FR-011 satisfiable.

## Project Structure

### Documentation (this feature)

```
specs/161-board-bench-harness/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── outcome-classification.md
│   └── harness-rollup.md
└── checklists/
    └── requirements.md
```

### Source Code (repository root)

```
src/coordinare/bench/
├── harness_outcome.py     # NEW — US1: four-way classification of a terminal marker
├── harness_rollup.py      # NEW — US1: (role, backend) aggregation + rollup artifact
├── harness_rank.py        # NEW — US3: harness-comparison view, tie rule, ranking
├── space.py               # EDIT — US2: harness-dimension validation (unknown names)
├── score.py               # EDIT — US3: view tag; additive, existing scalars untouched
├── artifact.py            # READ-ONLY — source of PersonaDispatch; not modified
├── grader.py              # READ-ONLY — card-level FAIL_HARNESS stays as-is
└── noise.py               # REUSED — component_stats() for the tie rule

agent/performer/src/performer/backends/
└── __init__.py            # EDIT — R8: promote the function-local harness dict to a
                           #        module-level SUPPORTED_BACKENDS constant (the
                           #        docstring already claims it exists). Single source
                           #        of truth for FR-017; no behavior change.

benchmarks/spaces/
└── harness.yaml           # NEW — US2: search space declaring harness dimensions

tests/unit/
├── test_161_fixtures.py          # synthetic + stub-generated artifact builders
├── test_161_harness_outcome.py   # classification, incl. one case per marker class
├── test_161_harness_rollup.py    # aggregation, pooling, per-run scalars, absent-vs-zero
├── test_161_harness_space.py     # dimension validation, unknown harness rejected
├── test_161_harness_sweep.py     # FR-018: failed point recorded, sweep continues
├── test_161_harness_rank.py      # objective shape + units, ties, insufficient evidence
└── test_161_score_regression.py  # FR-011: fixed-harness scalar VALUES unchanged
```

**Structure Decision**: single project, extending `src/coordinare/bench/` in place. New
behavior goes in new sibling modules rather than inside `score.py`/`grader.py`, because
FR-011 requires the existing config-comparison path to be provably untouched. Editing the
existing scorer in place would make that regression guarantee much harder to argue. The
one unavoidable edit to `score.py` is the additive view discriminator, which is why FR-011
is a value guarantee rather than a byte guarantee.

**Story independence**: US2 is independent of US1 because FR-018 only requires the failing
dispatch to be *recorded* in the artifact; classifying it as a defect is US1's rollup.
That separation is deliberate, so the two stories can be built in parallel.

## Phase 0: Research

See [research.md](./research.md). All Technical Context items are resolved; no
NEEDS CLARIFICATION remains. Key findings were obtained by execution, not inference:

- The harness override path already works; US2 is declaration plus validation.
- The config schema does not constrain `backend`, so an invalid harness is silently
  accepted. This is the actual work item behind FR-017.
- An unknown role is already rejected by `SpaceError`, so FR-017's role half holds.
- `terminal_marker` is preserved on every dispatch, so US1 works on recorded artifacts.
- **No `runs/` directory or `run.json` currently exists in the tree** (verified). US1 is
  still host-independent, but its fixtures must be produced by the existing stub runner
  rather than found on disk. The plan and T003 reflect this rather than assuming data.

## Phase 1: Design

See [data-model.md](./data-model.md) for the entity shapes, `contracts/` for the two
behavioral contracts (marker classification, rollup aggregation), and
[quickstart.md](./quickstart.md) for how an operator runs a rollup against an artifact.

## Complexity Tracking

| Deviation | Current need | Why the simpler option is insufficient |
| --- | --- | --- |
| Three new modules rather than editing `score.py` | FR-011 requires existing config-comparison scalars to be provably unchanged | Editing the shared scorer in place makes "unchanged for fixed-harness sweeps" a claim about diffs rather than a structural fact. Separate modules make the regression test meaningful. |
| A second scoring view rather than one objective | Harness comparison and config comparison want opposite treatment of harness failure (spec-135 FR-005 vs this spec's FR-010) | One objective cannot both quarantine and penalize harness failure. Collapsing them would silently change the meaning of every existing scalar. |

## Deferrals

None. Every functional requirement is assigned to a task in
[tasks.md](./tasks.md). The single external blocker (US3's live ranking run needs a
healthy inference host, currently unresponsive) is a dependency, not a deferral: the code
path is implemented and unit-tested with recorded artifacts, and only the live
confirmation run waits on the host.
