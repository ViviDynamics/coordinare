# Data Model — Spec 136: search space + sweep artifact

Pydantic 2.x models in `src/coordinare/bench/space.py` and
`src/coordinare/bench/sweep.py`, following the 134/135 conventions
(module-level `SCHEMA_VERSION`, round-trip self-validation before write).

## A. Search space (`space.py`, loaded from YAML)

### SearchSpace

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | space identity, recorded in the sweep artifact |
| `baseline_config` | `str` | path to the baseline coordinare config YAML (resolved relative to the space file) |
| `dimensions` | `list[Dimension]` | swept dimensions; everything unlisted is fixed (FR-003) |
| `candidates` | `list[Candidate]` | named whole-config candidates |

Validation (load time, FR-002): baseline loads as the root `CoordinareConfiguration`;
every dimension path resolves in the baseline dump; every dimension choice and
every candidate materializes through `CoordinareConfiguration(**dict)`. At least
one of dimensions/candidates non-empty (FR-001). Failures name the offending
element.

### Dimension

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | unique within the space |
| `path` | `str` | dotted config path; `symphonies.<name>.…` addresses the list by symphony name (research R2) |
| `choices` | `list[Any]` | explicit allowed values (≥1); booleans or enum strings in practice |
| `description` | `str` | what the dimension buys (documentation, FR-011) |

### Candidate

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | unique within the space |
| `overrides` | `dict[str, Any]` | path → value applied together over the baseline |
| `description` | `str` | intent (e.g. "cheap-models") |

### Materialization (functions, not models)

`materialize(baseline_dump, overrides) -> CoordinareConfiguration` — deep-copy,
set each dotted path, re-validate; raises `SpaceError` naming dimension+value
on rejection. `config_fingerprint(config) -> str` — SHA-256 (16 hex) of the
canonical `model_dump_json()`.

## B. Sweep artifact (`sweep.py` → `sweep.json` + rendered `sweep-report.md`)

### SweepArtifact

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` | `SWEEP_SCHEMA_VERSION = 1` |
| `space_name` | `str` | from the definition |
| `mode` | `"ablation" \| "candidates"` | |
| `substrate_mode` | `"stub" \| "real"` | drives the fidelity caveat in the report (spec Assumptions) |
| `repeats` | `int` | per-point repeats used |
| `repeats_source` | `"default" \| "operator" \| "noise_report"` | + `noise_report_ref: str \| None` |
| `weights` | `Weights` | the 135 weights used for every point (one set per sweep) |
| `baseline` | `PointResult \| None` | ablation only |
| `points` | `list[PointResult]` | one per declared non-baseline point |
| `deltas` | `list[AblationDelta]` | ablation only; vs the same sweep's baseline |
| `ranking` | `list[str]` | candidates only; point ids ordered by mean scalar, unrankable last |
| `coverage` | `Coverage` | the honesty section (FR-010, SC-004) |

### PointResult

| Field | Type | Notes |
|---|---|---|
| `point_id` | `str` | `baseline`, `<dimension>=<value>`, or `candidate:<name>` |
| `kind` | `"baseline" \| "ablation" \| "candidate"` | |
| `dimension` / `value` | `str \| None` / `Any` | ablation points |
| `candidate` | `str \| None` | candidate points |
| `fingerprint` | `str` | the materialized config's own fingerprint (FR-004) |
| `duplicate_of` | `str \| None` | point id with an identical fingerprint, if any (flagged, US3 sc.2) |
| `score_refs` | `list[str]` | per-repeat `score.json` paths (nothing averaged away silently) |
| `mean_scalar` | `float \| None` | mean over repeats; `None` = unrankable |
| `mean_components` | `dict[str, float]` | means of the numeric 135 components present |
| `failed` | `bool` | any run/scoring failure |
| `error` | `str` | failure detail (FR-009) |

### AblationDelta

`dimension`, `value`, `point_id`, `scalar_delta: float | None`,
`component_deltas: dict[str, float]` — point minus baseline (means).

### Coverage

| Field | Type | Notes |
|---|---|---|
| `declared_points` | `int` | enumerated before running (FR-003) |
| `scored_points` | `int` | points with a usable mean score |
| `dropped` | `list[DroppedPoint]` | id + reason for every failed/skipped/unrankable point |
| `notes` | `list[str]` | e.g. baseline-coinciding choices skipped, repeat fallback, stub caveat |

`DroppedPoint`: `point_id: str`, `reason: str`.

Invariant (SC-004): `declared_points == scored_points + len(dropped)`.

## C. Substrate seam (existing model touched)

`run_board(..., config: CoordinareConfiguration | None = None)` — when set:
`daemon.state["config"] = config.global_config`,
`daemon.state["symphony_configs"] = {s.name: s}` and the run artifact's
`config_fingerprint.hash` is the materialized fingerprint with
`source_path="<materialized>"`. Default `None` = today's behavior (backward
compatible; existing 134/135 tests unchanged).

## D. Relationships

```
SearchSpace ──enumerates──▶ config points ──materialize──▶ CoordinareConfiguration
     │                                                        │ inject
     │                                                        ▼
     │                                    run_board (134) ──▶ RunArtifact
     │                                                        │ score_run (135)
     │                                                        ▼
     └──────────────── SweepArtifact ◀──aggregate──── ScoreObject × repeats
                          │
                          └─▶ sweep-report.md (deltas / ranking / coverage)
```
