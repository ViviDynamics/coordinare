# Contract: Role-Harness Rollup

**Owner**: `src/coordinare/bench/harness_rollup.py` (spec 161, FR-007 through FR-009, FR-025)

## Input

One or more `RunArtifact`s (spec 134, schema v1). Artifacts of an unknown schema version
are refused rather than partially interpreted, matching existing scoring behavior.

## Output

A `HarnessRollup` with one `RoleHarnessRow` per distinct `(role, backend)` pair observed.

## Aggregation rules

1. **Group by `(role, backend)`**, taken from the dispatch itself, never inferred from
   configuration. A run whose config says `reviewer: openclaw` but whose dispatch records
   `backend="junie"` is grouped as `junie`; the artifact is the evidence.
2. **Deduplicate by dispatch identity.** A dispatch registered under several stages is
   counted once per `(role, backend)`.
3. **Pool across runs** (FR-025): counts sum, and `runs_contributing` records how many
   artifacts fed the row.
4. **Rates over conclusive only**: denominators are `credit + harness_defect`.
   `ENVIRONMENT` and `INCONCLUSIVE` are excluded from both numerator and denominator.
5. **Absent is not zero** (FR-013): when `conclusive == 0`, `defect_rate` and
   `credit_rate` are `None`. They MUST NOT be `0.0`. A test asserts `None`, because a
   `0.0` defect rate would read as a flawless harness when in fact nothing is known.
6. **Tokens**: `tokens_total` is `None` when no dispatch reported tokens, never `0`.
   Summing treats missing values as absent, not zero.
7. **A retry that later succeeds still records the defect.** A harness needing retries is
   worse than one that does not, so defects are counted per dispatch, not per card
   outcome.

## Invariants

- `credit + harness_defect + environment + inconclusive == dispatches` for every row.
- All counts are non-negative.
- `conclusive == credit + harness_defect`.
- A role present in the config but never dispatched produces **no row**, rather than a
  row of zeros. Consumers distinguish "no evidence" from "measured zero".

## Guarantees

- **Works on already-captured artifacts.** No re-run, no live inference host. This is what
  makes US1 independently shippable.
- **Read-only.** The rollup never mutates the artifact it reads.
