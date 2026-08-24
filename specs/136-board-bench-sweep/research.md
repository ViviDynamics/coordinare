# Research — Spec 136: config search-space + sweep runner

All decisions verified against the code on `main` (post-135 merge, b0f0315).

## R1 — The config-injection seam (verified)

**Decision**: `run_board` gains `config: CoordinareConfiguration | None = None`
(the multi-symphony ROOT: `global_config` + `symphonies`, `config.py:1703`);
when provided it is split exactly as the production daemon splits it —
`daemon.state["config"] = config.global_config` (`__main__.py:841`; all 27 node
call sites read `state.get("config")`) and `daemon.state["symphony_configs"] =
{name: SymphonyConfig}` (`__main__.py:976`; the per-symphony gate reads, e.g.
`dispatch_performer.py:75`). The artifact's
`config_fingerprint` becomes the SHA-256 (first 16 hex, matching 134's
convention) of the **materialized config's canonical JSON dump** when injected;
the existing file-hash path remains for `config_path`-only calls. Default
`None` keeps today's behavior bit-for-bit.

**Rationale**: nodes already read configuration through one seam; injecting at
that seam means zero node changes and full production parity for whichever
gates execute. Fingerprinting the materialized object (not the baseline file)
is what makes two sweep points distinguishable (FR-004).

**Alternatives considered**: writing each point to a temp YAML and passing
`config_path` — rejected: the 134 runner never *loads* config_path (it only
fingerprints it), so a file round-trip adds I/O without adding fidelity.

## R2 — Dimension paths: dotted segments with symphonies-by-name (verified)

**Decision**: a dimension `path` is dot-separated keys navigated over the
ROOT baseline's `model_dump()` dict (global knobs live under
`global_config.…`). The segment after `symphonies` is a **symphony name**
resolved to its list entry by matching `SymphonyConfig.name`
(`config.py:1569-1587`). Examples from the shipped default space:
`global_config.performers.implementer.mode`, `global_config.max_concurrent_cards`,
`symphonies.bench.persona_scope.ci_gate.enabled`. Materialization = deep-copy
baseline dump → set path → re-validate through `CoordinareConfiguration(**dict)`;
any pydantic rejection is surfaced naming the dimension and value (FR-002/US1
scenario 3 — never a silently coerced config).

**Rationale**: the two structures that matter are a flat model tree
(`performers.<role>` are plain fields, `PerformersConfig` `config.py:542`) and
one list (`symphonies`); name-addressing the list keeps definitions readable
and stable under reordering. Full JSONPath is unwarranted (YAGNI).

**Validation**: path existence is checked against the baseline dump at load
time (every intermediate key must exist); value admissibility is checked by
materializing every declared choice once at load (FR-002) — load is cheap
(pydantic re-parse per choice, no runs).

## R3 — What differentiates in stub mode (verified; SC-006 lever)

**Decision**: the SC-006 behavioral-injection test sweeps
`assignee_filter` (spec 050; read via `state["config"]` in
`check_board.py:1355`): the fake's cards carry no assignees, so a configured
filter makes every card ineligible — the same fixture merges under the
baseline config and never dispatches under the filtered point, provable from
the run artifact alone. (`max_concurrent_cards` was evaluated first and is
**not** observable in stub mode: the stub completes a card's whole lifecycle
within one cycle, so pickup can never interleave — recorded as part of the
stub-fidelity caveat.) Gate dimensions under `symphonies[…].persona_scope` are exercised
structurally in stub mode (materialize → validate → inject → fingerprint) but
their deltas are expected ~0 because the stub bypasses `dispatch_card` — the
recorded stub-fidelity caveat (spec Assumptions) that every stub-mode report
must carry.

## R4 — Repeats-per-point derivation from a 135 noise report

**Decision**: `repeats = clamp(ceil((scalar_stdev / resolution)²), 1, max_repeats)`
— the n at which the standard error of the mean (stdev/√n) falls to the
resolution threshold. Defaults: `resolution = 0.01` (an order of magnitude
below one card-verdict step at ≤5 cards, per the 135 finding),
`max_repeats = 10`. Sources recorded in the artifact as
`repeats_source ∈ {default, operator, noise_report}` with the report path.
A noise report with no scalar stats or `effective_repeats == 0` falls back to
the default and the report says so (spec edge case).

**Rationale**: implements the 135 go/no-go finding's recommendation ("the
sweep runner should treat repeats-per-point as data-driven, not fixed")
with the simplest statistically-grounded rule.

## R5 — Sweep artifact conventions

**Decision**: `SWEEP_SCHEMA_VERSION = 1`; `SweepArtifact` mirrors the
134/135 pattern (`to_validated_json()` round-trip before write; `sweep.json`
+ rendered `sweep-report.md` at the session root;
`runs/<ts>-sweep/<point-id>/<repeat>/` per-run dirs holding the usual
`run.json` + `score.json`). Coverage is a first-class sub-object:
`declared`, `scored`, `dropped[]` (each with point id + reason) — the
reconciliation SC-004 demands. Ablation deltas are computed scalar-and-
per-component against the same sweep's baseline point; candidate ranking
sorts by scalar with `None` scalars last and flagged. Duplicate materialized
fingerprints across points are detected and flagged (US3 scenario 2).

## R6 — The shipped default space + baseline (`benchmarks/spaces/`)

**Decision**: commit `benchmarks/spaces/baseline.yaml` — a minimal valid root
`CoordinareConfiguration` (one `bench` symphony with `persona_scope` present so
gate paths resolve; performer roles configured with the stub-typical
defaults) — and `benchmarks/spaces/default.yaml` — the documented swept set:
gates `symphonies.bench.persona_scope.{ci_gate,local_test_gate,
baseline_prevention_gate,baseline_classification_gate,inherited_repair_gate,
env_blocked_gate}.enabled` (075/089/090/095, all boolean), plus
`max_concurrent_cards` [1,2] and `performers.{implementer,reviewer}.mode`
choices, plus four candidates (conservative/aggressive/cheap-models/premium).
Blocked-recovery (129) is env-var-gated (`COORDINARE_BLOCKED_RECOVERY`), not a
config field — documented as outside the sweepable set until it moves into
configuration (spec Assumptions). Security-scan (083) ships no boolean gate
field — verified: its only config surface is the weak-judge denylist validator
on the security role's model (`config.py:1023`) — so 083 participates in the
space through `performers.security.mode` like any other role, not through an
invented gate.

**Rationale**: FR-011 wants the known dimensions named and the rest fixed;
committing space + baseline as data keeps the definition reviewable and gives
137 a concrete starting space.

## R7 — CLI shape

**Decision**: new `scripts/board_sweep.py` with subcommands `ablate` and
`candidates`: shared flags `--space <yaml> --run-dir --repeats N |
--noise-report <path> [--resolution --max-repeats] --real` plus the
board_score.py judge/weight flag set (same helper conventions). Prints the
enumerated point count before running (FR-003) and the coverage summary
after. `board_bench.py`/`board_score.py` stay untouched.
