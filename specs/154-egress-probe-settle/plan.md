# Implementation Plan: A settle-aware egress probe

**Branch**: `154-egress-probe-settle` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)
**Issue**: #233 (follow-up to spec 146 / #225)

## Summary

Stop the NetworkPolicy egress probe concluding "this cluster does not enforce NetworkPolicy"
from a single measurement taken before the CNI had programmed the policy. A reach under policy
is re-measured once; only a reach that survives re-measurement becomes that verdict.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12)
**Primary Dependencies**: existing only — the kubernetes client, asyncio, structlog. **None added.**
**Storage**: N/A — a diagnostic, no persisted state.
**Testing**: pytest. Unit tests with fake clients in `tests/unit/test_225_kubernetes_egress.py`
style; the live test in `tests/integration/` needs a real cluster and is skipped without one.
**Target Platform**: any Kubernetes cluster an operator points `coordinare doctor` at
**Project Type**: single
**Performance Goals**: worst case adds one Pod lifetime, and only on a cluster that does not
enforce; the common paths are unchanged.
**Constraints**: must not weaken the existing guarantee that enforcement is never claimed
without a reachable control.
**Scale/Scope**: one source file, one unit test file, one integration test file.

## Constitution Check

| Principle | Assessment |
|---|---|
| I. Code Quality First | Passes. The repeated measurement is extracted into one named helper rather than duplicated, so "measure under policy" exists once. |
| II. Testing Discipline (NON-NEGOTIABLE) | Tests first. Deterministic: fake clients script the sequence reach-then-blocked with no sleeps and no cluster. The live test stays opt-in. |
| III. User Experience Consistency | Central to the change — FR-009 requires the operator be told what was actually observed, including that a re-measurement happened. |
| Minimal dependencies | Passes — none added. |

No violations.

## Project Structure

```
specs/154-egress-probe-settle/
├── spec.md, plan.md, research.md, tasks.md

src/coordinare/services/kubernetes_egress.py   # extract _measure_under_policy; re-measure a reach
tests/unit/test_225_kubernetes_egress.py      # the three outcomes, with fake clients
tests/integration/test_225_egress_probe_live.py  # the manual comparison stops racing
```

## Phases

**Phase 1 — Tests first.** Fake clients driving: blocked first time (enforced, no second Pod);
reached then blocked (enforced, late); reached twice (not enforced); reached then never_ran
(inconclusive). Plus: the second Pod is cleaned up.

**Phase 2 — Extract.** Pull the create-denied-Pod-and-read-its-outcome sequence into one helper
taking a name, so measuring twice is calling it twice rather than a copy.

**Phase 3 — Re-measure.** On `reached`, measure once more and decide from the pair. Details name
what was seen.

**Phase 4 — The live test.** Make its hand-run comparison sample the same way, so a correct
probe cannot fail it.

**Phase 5 — Verify and review.** Full suite, lint, mypy, then the mandatory adversarial
`Workflow` review over the branch diff before merge.

## Risks

- **Laundering a true negative into "inconclusive".** The failure mode of over-correcting.
  Guarded by a test that a cluster reaching twice is still reported as not enforcing (SC-002).
- **An unbounded retry.** Would make "does not enforce" arrive as a timeout instead of an
  answer. Exactly one re-measurement, asserted.
- **Leaked Pods.** More Pods means more to clean. `created.pods` already covers it; a test
  asserts the second Pod is deleted rather than trusting that.
