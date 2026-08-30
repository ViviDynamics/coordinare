# Tasks: A settle-aware egress probe

**Feature**: `154-egress-probe-settle` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)
**Issue**: #233

Tests before implementation (Constitution II). Decisions are settled in
[research.md](./research.md) — R2 in particular: exactly one re-measurement, never a loop.

## Phase 1: Tests first

- [x] T001 (FR-001, SC-001) In `tests/unit/test_225_kubernetes_egress.py`, a fake cluster whose
      denied Pod reaches the first time and is blocked the second yields `enforced=True`.
      This is the defect; without this test the fix is unverified.
- [x] T002 [P] (FR-004, SC-002) A fake cluster that reaches both times yields `enforced=False` —
      a true negative must survive the fix, not be laundered into "inconclusive".
- [x] T003 [P] (FR-003) A cluster blocked on the first measurement yields `enforced=True` and
      takes **no** second measurement — the common path must not get slower.
- [x] T004 [P] (FR-005) Reached first, then a re-measurement that never ran, yields inconclusive
      (`measured_under_policy=False`), not either verdict.
- [x] T005 [P] (FR-007) Exactly one re-measurement is taken, never more. Asserted on the number
      of denied Pods created, so an accidental loop fails.
- [x] T006 [P] (FR-008, SC-005) The re-measurement's Pod is deleted, including when the probe
      returns early.
- [x] T007 [P] (FR-009, SC-003) Each of the three outcomes produces a distinct detail naming
      what was observed; the late-settling one says the first attempt got through.

## Phase 2: Implementation

- [x] T008 (FR-002) In `src/coordinare/services/kubernetes_egress.py`, extract the
      create-denied-Pod-and-read-its-outcome sequence into one helper taking a Pod name, so a
      second measurement is a second call rather than a copied block.
- [x] T009 (FR-001, FR-002, FR-003, FR-004, FR-005) On `reached`, re-measure once and decide
      from the pair: blocked → enforced (late); reached → not enforced; never_ran →
      inconclusive.
- [x] T010 (FR-006) Confirm the control-Pod guarantee is untouched: no reachable control still
      means inconclusive, never "enforced".
- [x] T011 (FR-009) Write the three details.

## Phase 3: The live test

- [x] T012 (FR-010, SC-006) In `tests/integration/test_225_egress_probe_live.py`, make the
      hand-run `manual-deny` comparison sample the same way, so a correct probe cannot fail it.
      Keep it an independent check — its own policy, its own Pod — sharing only the discipline.

## Phase 4: Verification and review

- [x] T013 `.venv/bin/ruff check src tests` and `.venv/bin/mypy` on the changed module.
- [x] T014 `.venv/bin/pytest tests/ -q`, including the live test against the local cluster.
- [x] T015 (SC-004) State the probe's worst-case duration in the docstring, so an operator
      knows what they are waiting for.
- [x] T016 Acceptance re-read against SC-001..SC-006.
- [x] T017 Commit code and spec together, referencing #233.
- [x] T018 Adversarial `Workflow` review over the full branch diff before merge. Lenses:
      over-correction hiding a true negative, unbounded retry, leaked Pods, and whether the
      live test now merely copies the implementation instead of checking it.

## Dependencies

```
Phase 1 (T001-T007) -> Phase 2 (T008-T011) -> Phase 3 (T012) -> Phase 4 (T013-T018)
```

T008 must precede T009: the re-measurement calls what the extraction creates.

## Implementation strategy

There is one user-visible behaviour here, so there is no partial delivery: Phase 1 + Phase 2 is
the whole fix. Phase 3 stops the suite contradicting it.

## Phase 5: Review remediation (adversarial `Workflow`)

- [x] T019 The "second Pod never ran" case reused the generic never-ran wording, which fits
      the first-Pod case and not this one. It now says what was actually seen: traffic got
      through once, and the confirming measurement could not be taken.
- [x] T020 Review argued this case should be conclusive, on the grounds that the first
      reach already proves no enforcement. **Rejected**, and the reasoning recorded here
      because it is the premise of the whole spec: a first reach is exactly what a policy
      the CNI has not programmed yet produces. If it were proof, there would be no bug and
      no need for a second measurement. The review contradicted itself on this — the same
      idea was raised as "Conclusiveness lost when second Pod never runs" and refuted.
- [x] T018 Adversarial `Workflow` review over the full branch diff.
