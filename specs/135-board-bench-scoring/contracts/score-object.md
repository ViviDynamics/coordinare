# Contract: Score Object

The documented result of grading one spec-134 run (FR-002/FR-006). Pydantic models
in `src/coordinare/bench/score.py`; serialized to `score.json` **next to the
`run.json` it scores**. This is the objective surface specs 136/137 consume
(SC-005). Enforced by `tests/unit/test_135_score_schema.py`.

## Guarantees

1. **Versioned**: `schema_version: int` (`SCORE_SCHEMA_VERSION`), bumped on any
   shape change; `artifact_schema_version` records the consumed input version.
   Unknown artifact versions are rejected loudly, never partially scored (FR-001).
2. **Self-validating**: round-trip re-parse before the score is declared written,
   matching the 134 artifact guarantee (FR-006).
3. **Reproducible**: scoring the same artifact twice with judging disabled yields
   identical content excluding `scored_at` (FR-007).
4. **Truth-graded**: per-card `category` follows the fixture's planted ground
   truth (`expected_final_state` + `ground_truth` rubric), independent of whether
   the fake board merged (FR-003). Agree-to-PASS: PASS ⇔ deterministic contract
   holds AND the judge does not disagree; judge errors fall back to
   deterministic-only per card, visibly (FR-004).
5. **Infra-noise separated**: `FAIL_HARNESS` cards are excluded from
   `correctness_rate`'s denominator and surfaced via `harness_failure_rate`
   (FR-005) — optimizers must not be steered by infrastructure failures.
6. **Weight-fingerprinted**: the `weights` used (incl. budgets) are embedded;
   scalar objectives are comparable only across equal weights.
7. **Cost stays an estimate**: `cost_usd`/`cost_estimated` carry the artifact's
   labeling verbatim; unknown cost sets `cost_component_missing=True` and omits
   the cost term from the scalar — never treated as zero.

## Shape

See `data-model.md` §A for full field tables. Top level:

```
ScoreObject
├─ schema_version, run_id, run_ref, artifact_schema_version, scored_at
├─ judged, judge_model, deterministic_only
├─ weights {w_correctness, w_cost, w_time, cost_budget_usd, time_budget_seconds}
├─ cards: [CardVerdict{card_id, fixture_id, expected_final_state, observed_final_state,
│          category, correct, deterministic_ok, deterministic_detail,
│          judge_correct, judge_quality, judge_reason}]
├─ components {correctness_rate, harness_failure_rate, tokens_processed,
│              cost_usd, cost_estimated, wall_clock_seconds}
├─ scalar, cost_component_missing
```

## Scalar objective (Clarifications, spec)

```
scalar = w_correctness · correctness_rate
       − w_cost · (cost_usd / cost_budget_usd)      # omitted if cost unknown
       − w_time · (wall_clock_seconds / time_budget_seconds)
```

Defaults `1.0 / 0.1 / 0.1`. `correctness_rate = None` (zero gradeable cards) makes
the scalar `None`-safe: the score object is still written, with the condition
explicit, and downstream consumers must treat it as unrankable.
