# Research — Spec 135: scoring + noise characterization

All decisions verified against the code on `main` (post-134 merge, 46d1f67).

## R1 — What the scorer can deterministically know (verified against the artifact)

**Decision**: deterministic per-card signals are (a) `final_state` vs the fixture's
`expected_final_state`, (b) `merge.merged`, and (c) the CI conclusion of the card's
merged head (`ci_results` matched on `head_sha` where available, else the last CI
result). Harness failure = `final_state == "error"` **or** zero dispatches recorded.

**Rationale**: the 134 artifact records what happened without judging it
(`bench/artifact.py` docstring). The fake's CI runs the fixture's **real pytest
acceptance suite** (`fake_github.py` ci_run events, `runner.py:238-247`), so "CI
green on the merged head" *is* "the planted acceptance tests pass" — the strongest
truth signal available without re-executing anything. `runner._build_artifact`
currently attaches every `ci_run` event to every card (single-fixture runs in
practice, per the runner's `# ponytail` note), so the grader matches on `head_sha`
first and falls back to the last result, and single-fixture-per-run remains the
documented whole-lifecycle mode.

**Alternatives considered**: re-running the acceptance suite against the post-merge
repo at scoring time — rejected: the runner tears down the scratch repo
(`runner.py:196`), and re-execution would duplicate what the recorded CI already
proved; scoring must work from the artifact alone (FR-001).

## R2 — Expected outcome: structured field on Fixture (backward-compatible)

**Decision**: add `expected_final_state: Literal["merged","blocked"] = "merged"` to
`bench.fixtures.Fixture`. Free-text `ground_truth` remains the judge rubric; an
empty `ground_truth` makes the card ungradeable → the scorer fails loudly (spec
edge case).

**Rationale**: FR-003 requires grading adversarial fixtures whose *correct* outcome
is a non-merge (planted vulnerability must be held). The 134 model only carries
free-text `ground_truth` ("planted ground-truth marker consumed later by spec 135" —
`fixtures.py` docstring); a structured field is the smallest lift that makes
correct-merge vs correct-block decidable deterministically. Default `"merged"`
preserves every existing manifest's meaning (FR-010).

**Alternatives considered**: parsing intent out of the free-text marker — rejected,
not testable/unambiguous; a full expected-checks DSL — rejected as speculative
(YAGNI) until Phase 3 needs it.

## R3 — Reusing 077's agree-to-PASS: contract reuse, not code import

**Decision**: implement `bench/judge.py` + the categorize step in `bench/grader.py`
to the *same contract* as `scripts/persona_bench.py` (077): judge prompt = rubric +
deterministic signals + evidence blob → strict JSON `{"correct": bool, "quality":
0-5, "reason": str}`, temperature 0, over the LiteLLM proxy `chat/completions`;
verdict categories `PASS | FAIL_MODEL | FAIL_HARNESS | ERROR`; PASS requires
deterministic contract **and** judge agreement when judging is enabled
(`persona_bench.categorize`, lines 130-156); a judge error never flips a verdict —
the card falls back to deterministic-only and is labeled.

**Rationale**: `scripts/` is not an importable package, and refactoring the
standalone 077 harness into a shared library is out of scope (PR-scope
discipline; 077 keeps working untouched). The issue's "reused (not reinvented)"
acceptance is about the grading *model* — deterministic + judge, agree-to-PASS,
harness/model failure split — which is preserved verbatim and asserted by unit
tests mirroring 077's categorize truth table.

**Alternatives considered**: importing via `sys.path` hacks (fragile, hides the
dependency); moving 077's grading into `coordinare.bench` and re-pointing
persona_bench (right long-term, wrong PR — noted as an option for Phase 3 if the
sweep runner needs it).

## R4 — Score components, weights, and the scalar (from spec Clarifications)

**Decision**: `ComponentVector = {correctness_rate, harness_failure_rate,
tokens_processed, cost_usd (estimate-labeled), wall_clock_seconds}`.
`correctness_rate` = correct / gradeable cards, where harness-failed cards are
excluded from the denominator but surfaced via `harness_failure_rate` — config
search must see infra noise, not have it silently folded into capability.
Scalar: `w_c·correctness_rate − w_cost·(cost_usd/cost_budget) −
w_time·(wall_clock_seconds/time_budget)`, defaults `w_c=1.0, w_cost=0.1,
w_time=0.1`, budgets operator-configurable; unknown cost ⇒ term omitted +
`cost_component_missing=True`. Weights/budgets embedded in the score object;
comparability requires matching weight fingerprints.

**Rationale**: mirrors 077's FAIL_MODEL/FAIL_HARNESS separation at the aggregate
level; keeps the component vector loss-free for a later Pareto treatment (SC-005).

## R5 — Noise statistics

**Decision**: per component: mean, **sample** variance/stdev (n−1; reported as 0
with a `single_sample` flag when effective N < 2), min, max. Per-card verdict
stability: fraction of repeats agreeing with the modal verdict per fixture id.
Failed repeats (no valid artifact/score) are listed with their error and excluded
from the statistics; the report states requested N and effective N (FR-008).
Implementation: stdlib `statistics` — no new dependency.

**Rationale**: sample variance is the standard estimator for "how noisy would the
optimizer's objective be"; dropping failed repeats *silently* would understate
noise, so they are first-class in the report.

## R6 — Where outputs live

**Decision**: `score.json` is written into the run dir next to `run.json`
(self-validating round-trip before declared written, mirroring
`RunArtifact.to_validated_json`). The repeat runner creates
`runs/<ts>-noise/<i>/run.json|score.json` per repeat plus one
`noise-report.json` + `noise-report.md` at the session root. The committed
go/no-go finding is `specs/135-board-bench-scoring/noise-findings.md`, written
from a real stub-substrate measurement (N=5) and stating the fidelity caveat
(spec Assumptions; 134's `real-performer-followup.md`).

## R7 — CLI shape

**Decision**: one new `scripts/board_score.py` with argparse subcommands:
`score --run-dir <dir> --fixtures <manifest> [--judge-base-url … --judge-model …
--judge-api-key-env …] [--w-correctness/--w-cost/--w-time --cost-budget
--time-budget]` and `noise --repeats N [--fixtures …] [--config …] [--run-dir …]`
(noise reuses `run_board` exactly as `board_bench.py` does, then scores each
repeat). `board_bench.py` stays untouched (single responsibility: one raw run).

**Rationale**: mirrors the existing CLI convention; keeps 134's entry point
stable for anything already calling it.
