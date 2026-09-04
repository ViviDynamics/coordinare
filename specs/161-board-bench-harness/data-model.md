# Data Model: Rank Backend Harnesses Per Role (161)

All models are pydantic 2.x, matching the existing bench artifacts. Nothing here changes
an existing model. `PersonaDispatch`, `RunArtifact`, `ScoreObject` and `Weights` are
consumed as-is.

## OutcomeClass (enum)

The four-way classification of one dispatch, derived from its terminal marker (FR-001).

| Value | Meaning | Counts toward |
| --- | --- | --- |
| `CREDIT` | The role produced a usable result, including a legitimate negative verdict | numerator and denominator |
| `HARNESS_DEFECT` | The harness could not produce usable output | denominator only |
| `ENVIRONMENT` | The environment failed; neither harness nor model at fault | neither (FR-004) |
| `INCONCLUSIVE` | No terminal marker, or an unrecognized one | neither (FR-005, FR-006) |

"Conclusive" means `CREDIT` or `HARNESS_DEFECT`. Rates are computed over conclusive
dispatches only, never over the raw dispatch count.

### Marker mapping (the contract)

| Marker | Class |
| --- | --- |
| any member of `TERMINAL_SUCCESS_STATES` (imported, not duplicated) | `CREDIT` |
| `changes_requested`, `qa_failed`, `security_failed`, `blocked` | `CREDIT` |
| `malformed_output`, `system_error` | `HARNESS_DEFECT` |
| `env_blocked` | `ENVIRONMENT` |
| `None`, `""` | `INCONCLUSIVE` |
| anything else | `INCONCLUSIVE` (surfaced, never silently credited) |

**Invariant**: classification reads `PersonaDispatch.terminal_marker`. It MUST NOT read
`PersonaDispatch.status`, which collapses negative verdicts and defects into one value.

## RoleHarnessRow

One `(role, backend)` pair's aggregated evidence (FR-007, FR-008).

| Field | Type | Notes |
| --- | --- | --- |
| `role` | `str` | e.g. `reviewer` |
| `backend` | `str` | e.g. `openclaw` |
| `dispatches` | `int` | total rows aggregated |
| `credit` | `int` | `CREDIT` count |
| `harness_defect` | `int` | `HARNESS_DEFECT` count |
| `environment` | `int` | `ENVIRONMENT` count |
| `inconclusive` | `int` | `INCONCLUSIVE` count |
| `conclusive` | `int` | `credit + harness_defect` |
| `defect_rate` | `float \| None` | `harness_defect / conclusive`; **`None`** when `conclusive == 0` (FR-013) |
| `credit_rate` | `float \| None` | `credit / conclusive`; `None` when `conclusive == 0` |
| `seconds_total` | `float` | summed observed durations |
| `tokens_total` | `int \| None` | `None` when no dispatch reported tokens |
| `tokens_partial` | `bool` | true when SOME dispatches reported tokens and others did not, so the sum is a lower bound. Distinct from `None` (nothing reported). A partial sum makes the cost term decline rather than undercharge.
| `runs_contributing` | `int` | how many runs were pooled (FR-025) |
| `per_run_scalars` | `list[float]` | one scalar per contributing run, retained so run-to-run spread stays computable for the tie rule (FR-025). Pooled counts alone collapse the sample and leave FR-020 with no variance. |
| `unknown_markers` | `list[str]` | distinct unrecognized markers seen (FR-006) |

**Validation**: counts are non-negative; the four class counts sum to `dispatches`;
`None` rates are distinct from `0.0` rates and MUST NOT be coerced.

## HarnessRollup

The per-run (or pooled multi-run) rollup artifact, written beside the run it describes
(FR-026).

| Field | Type | Notes |
| --- | --- | --- |
| `schema_version` | `int` | starts at 1 |
| `run_ids` | `list[str]` | one entry per pooled run |
| `rows` | `list[RoleHarnessRow]` | one per `(role, backend)` |
| `generated_at` | `datetime` | |
| `artifact_schema_versions` | `list[int]` | refuse unknown versions, matching existing scoring behavior |

**Round-trip guard**: like `RunArtifact` and `ScoreObject`, `HarnessRollup` MUST serialize
and re-parse against its own schema before being declared written, raising if it does not.
`schema_version` starts at 1 and is bumped on any field change.

## ScoringView (enum)

Identifies which objective produced a score, so the two are never compared (FR-012).

| Value | Harness failure treatment |
| --- | --- |
| `CONFIG_COMPARISON` | quarantined (existing spec-135 FR-005 behavior) |
| `HARNESS_COMPARISON` | penalized (this feature, FR-010) |

Emitted alongside the embedded `Weights`, extending the existing rule that scores compare
only within matching weights to also require a matching view.

**Additive-change policy (FR-011)**: adding this discriminator to the existing score
document changes its serialized form, so `SCORE_SCHEMA_VERSION` is bumped and the field
**defaults to `CONFIG_COMPARISON`**. The guarantee FR-011 makes is that the computed
`scalar` and component *values* are numerically unchanged for a fixed-harness sweep. It is
explicitly **not** a byte-identity guarantee, which would be impossible alongside a new
field.

## HarnessScore

One `(role, backend)` pair's harness-comparison score (FR-010, FR-024).

| Field | Type | Notes |
| --- | --- | --- |
| `role` / `backend` | `str` | |
| `view` | `ScoringView` | always `HARNESS_COMPARISON` |
| `weights` | `Weights` | reused, not redefined |
| `credit_rate` | `float \| None` | the credit term |
| `seconds` / `tokens` | numeric | penalty inputs |
| `scalar` | `float \| None` | `None` when evidence is insufficient (FR-013) |
| `evidence` | `RoleHarnessRow` | the row the scalar came from (FR-022) |

**Objective** (mirrors `compute_scalar`, FR-024):

```
scalar = w_correctness * credit_rate
       - w_cost * (estimate_cost_usd(tokens) / cost_budget_usd)   # units must match
       - w_time * (seconds / time_budget_seconds)
```

**Unit rule (FR-024)**: the cost term divides an estimated **USD** cost by
`cost_budget_usd`. Tokens are converted first via the existing
`artifact.estimate_cost_usd()`. Dividing a raw token count by a currency budget is a unit
error and is explicitly forbidden.

`scalar` is `None`, never `0.0`, when `credit_rate is None`. When no token count is
available the cost term is **omitted** and `cost_component_missing` is recorded, mirroring
the existing scorer, never treated as zero cost.

## HarnessRanking

The advisory per-role result (FR-019 through FR-023).

| Field | Type | Notes |
| --- | --- | --- |
| `role` | `str` | |
| `ordered` | `list[HarnessScore]` | best first, among rankable candidates |
| `ties` | `list[list[str]]` | **pairs** of harnesses that cannot be distinguished (FR-020). Not transitive groups: `x~y` and `y~z` can hold while `x~z` does not, so merging a chain would assert an equivalence the data denies.
| `insufficient_evidence` | `list[str]` | harnesses below the conclusive-dispatch minimum (FR-021) |
| `verdict` | `Literal["ranked", "tie", "insufficient_evidence", "no_viable_harness"]` | |
| `min_conclusive` | `int` | the threshold applied, default 3, configurable |
| `single_run` | `bool` | true when no candidate had more than one contributing run, so no spread was measured (FR-020) |

**Tie rule** (FR-020): candidates `a` and `b` tie when
`abs(mean_a - mean_b) <= stdev_a + stdev_b`, where the stats come from
`noise.py::component_stats(row.per_run_scalars)` — the **per-run** scalars, not the pooled
row. With a single contributing run `stdev` is 0, so only exactly equal scalars tie, and
`single_run` is set so the caller can see the tie band was not measured.

**Verdict rules**:
- fewer than 2 rankable harnesses → `insufficient_evidence`
- every candidate is a harness defect throughout (`credit_rate == 0` for all) →
  `no_viable_harness`, never "least bad wins"
- top candidates within the tie band → `tie`
- otherwise → `ranked`

**Advisory only** (FR-023): this model is serialized to a file. Nothing in this feature
writes live deployment configuration.

## HarnessDimension (search space)

A search-space dimension varying one role's harness (FR-014 through FR-018).

| Field | Type | Notes |
| --- | --- | --- |
| `name` | `str` | dimension label |
| `role` | `str` | must resolve in the baseline (already enforced by `_walk`) |
| `choices` | `list[str]` | harness names; **each validated against the known harness set at load time** (FR-017) |

`role` is authoring shorthand and is **cleared once normalized into `path`**. Left populated, the model would carry both fields and fail its own exactly-one-of check on every round-trip (dump then reload, `model_copy`, revalidating a dumped dict).

Expands to the override path `global_config.performers.<role>.backend`, which is verified
to work. Unknown harness names are rejected at declaration time, because the config schema
does not constrain `backend` and would otherwise accept a typo and fail mid-sweep.
