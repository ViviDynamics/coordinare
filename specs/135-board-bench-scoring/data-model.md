# Data Model — Spec 135: score object + noise report

Pydantic 2.x models in `src/coordinare/bench/score.py` and `src/coordinare/bench/noise.py`,
mirroring `bench/artifact.py` conventions (module-level `SCHEMA_VERSION`, `Literal`
enums, round-trip self-validation before write).

## A. Score object (`score.py`)

### ScoreObject (top level → `score.json`, written next to `run.json`)

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` | `SCORE_SCHEMA_VERSION = 1`; consumers (136/137) branch on it |
| `run_id` | `str` | copied from the scored `RunArtifact.run_id` |
| `run_ref` | `str` | path of the scored `run.json` (relative to the score file) |
| `artifact_schema_version` | `int` | the artifact version consumed (must be a known version, else loud failure — FR-001) |
| `scored_at` | `datetime` | timestamp (excluded from the FR-007 reproducibility comparison) |
| `judged` | `bool` | whether the LLM judge participated |
| `judge_model` | `str \| None` | judge model id when `judged` |
| `deterministic_only` | `bool` | `not judged` — explicit label (FR-004) |
| `weights` | `Weights` | the weights + budgets used (embedded — FR-002c) |
| `cards` | `list[CardVerdict]` | one per card in the artifact (total coverage) |
| `components` | `ComponentVector` | the per-run component vector |
| `scalar` | `float` | the weighted objective (Clarifications formula) |
| `cost_component_missing` | `bool` | cost term omitted because `cost_usd` was `None` |

### Weights

| Field | Type | Default |
|---|---|---|
| `w_correctness` | `float` | `1.0` |
| `w_cost` | `float` | `0.1` |
| `w_time` | `float` | `0.1` |
| `cost_budget_usd` | `float` | `1.0` (normalizer; operator-set) |
| `time_budget_seconds` | `float` | `600.0` (normalizer; operator-set) |

### CardVerdict

| Field | Type | Notes |
|---|---|---|
| `card_id` / `fixture_id` | `str` | identity |
| `expected_final_state` | `"merged" \| "blocked"` | from the fixture (R2) |
| `observed_final_state` | `str` | from the artifact |
| `category` | `"PASS" \| "FAIL_MODEL" \| "FAIL_HARNESS" \| "ERROR"` | 077 categories (FR-005) |
| `correct` | `bool` | `category == "PASS"` convenience |
| `deterministic_ok` | `bool` | the deterministic contract held (R1 signals) |
| `deterministic_detail` | `str` | human-readable evidence trail |
| `judge_correct` | `bool \| None` | judge ruling (None = not judged / judge error) |
| `judge_quality` | `int \| None` | 0–5 |
| `judge_reason` | `str` | one sentence (or `judge_error: …`) |

Agree-to-PASS: `PASS` requires `deterministic_ok` AND (`judge_correct` is not
False). A judge **error** (None) falls back to deterministic-only for that card and
is visible in `judge_reason` — it never flips a verdict (FR-004, spec US2).
Empty fixture `ground_truth` ⇒ scoring aborts loudly (edge case; no partial score).

### ComponentVector

| Field | Type | Notes |
|---|---|---|
| `correctness_rate` | `float \| None` | correct / gradeable; `None` if zero gradeable cards |
| `harness_failure_rate` | `float` | FAIL_HARNESS / total cards (R4) |
| `tokens_processed` | `int \| None` | from artifact totals |
| `cost_usd` | `float \| None` | estimate-labeled (carries the artifact's caveat) |
| `cost_estimated` | `bool` | always `True` while the artifact says so |
| `wall_clock_seconds` | `float` | from the artifact |

## B. Fixture extension (`fixtures.py`)

`Fixture.expected_final_state: Literal["merged", "blocked"] = "merged"` — the
planted right outcome. Default preserves every existing manifest (FR-010).
`ground_truth` (existing free text) is the judge rubric.

## C. Noise report (`noise.py` → `noise-report.json` + rendered `noise-report.md`)

### NoiseReport

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` | `NOISE_SCHEMA_VERSION = 1` |
| `config_fingerprint` | `str` | hash shared by all repeats (must match across repeats) |
| `requested_repeats` | `int` | N asked for |
| `effective_repeats` | `int` | repeats that produced a valid artifact + score |
| `failures` | `list[RepeatFailure]` | index + error for each failed repeat (never silently dropped) |
| `component_stats` | `dict[str, ComponentStats]` | one entry per numeric component |
| `scalar_stats` | `ComponentStats` | stats over the scalar objective |
| `card_agreement` | `dict[str, float]` | fixture_id → fraction of repeats agreeing with the modal verdict |
| `score_refs` | `list[str]` | paths of the per-repeat score files |

### ComponentStats

`mean`, `variance` (sample, n−1), `stdev`, `min`, `max`, `n`,
`single_sample: bool` (variance reported as 0 with this flag when n < 2 — R5).
Components that are `None` in some repeats report `n` = count of present values.

### RepeatFailure

`index: int`, `error: str`.

## D. Relationships

```
RunArtifact (134, schema v1) ──scored by──▶ ScoreObject (v1) ──aggregated by──▶ NoiseReport (v1)
        ▲                                        ▲
   fixture manifest ──ground truth + expected────┘
```
