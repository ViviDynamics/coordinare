"""Spec 137 — the automated config optimizer (the benchmark program's end goal).

A seed-deterministic **evolutionary local search** over a 136 search space:
evaluate the baseline and every named candidate (the FR-009 comparison), then
best-first single-dimension mutation from the best grid point, with seeded
random restarts and **fingerprint memoization** (no configuration is ever
evaluated twice; cache hits are free). Hard budget: evaluation count +
wall-clock; an in-flight evaluation completes, no new one starts past a cap.

Evaluation goes through one injectable seam (``Evaluator``): the shipped
``real_evaluator`` runs materialize → ``run_board(config=…)`` → ``score_run``
x repeats; tests inject synthetic objectives with a planted optimum — the
stub substrate is signal-free (136 ablation finding), so convergence is
proven synthetically and stub runs validate plumbing only.

Method justification (research.md R1): the space is small and discrete
(hundreds of combinations of mostly-boolean gates), evaluations are expensive
full-board runs — a memoized mutation search is sample-efficient here and
needs no new dependency; Bayesian-optimization libraries earn their weight on
continuous/high-cardinality spaces this program does not have yet.

Contract: ``specs/137-board-bench-optimizer/contracts/optimizer-artifact.md``.
"""

from __future__ import annotations

import itertools
import random
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

import structlog
from pydantic import BaseModel

from coordinare.bench.grader import score_run
from coordinare.bench.runner import run_board
from coordinare.bench.score import Weights
from coordinare.bench.space import config_fingerprint, materialize, resolve_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from coordinare.bench.fixtures import Fixture
    from coordinare.bench.judge import JudgeFn
    from coordinare.bench.score import ScoreObject
    from coordinare.bench.space import LoadedSpace
    from coordinare.config import CoordinareConfiguration

logger = structlog.get_logger(__name__)

OPTIMIZER_SCHEMA_VERSION = 1

_NUMERIC_COMPONENTS = (
    "correctness_rate",
    "harness_failure_rate",
    "tokens_processed",
    "cost_usd",
    "wall_clock_seconds",
)


class EvalOutcome(BaseModel):
    """What one evaluation of one configuration produced."""

    repeats: int = 1
    score_refs: list[str] = []
    mean_scalar: float | None = None
    mean_components: dict[str, float] = {}
    failed: bool = False
    error: str = ""


class Evaluator(Protocol):
    async def __call__(
        self, eval_id: int, overrides: dict[str, Any], config: CoordinareConfiguration
    ) -> EvalOutcome: ...


class EvaluationRecord(BaseModel):
    eval_id: int
    provenance: str  # baseline | candidate:<name> | restart | mutation-of:<eval_id>
    overrides: dict[str, Any]
    fingerprint: str
    cache_hit: bool = False
    repeats: int = 1
    score_refs: list[str] = []
    mean_scalar: float | None = None
    mean_components: dict[str, float] = {}
    failed: bool = False
    error: str = ""
    best_so_far: float | None = None


class ComparisonRow(BaseModel):
    label: str  # recommendation | baseline | candidate:<name>
    fingerprint: str
    mean_scalar: float | None = None
    delta_vs_recommendation: float | None = None
    duplicate_of_recommendation: bool = False


class Recommendation(BaseModel):
    eval_id: int
    overrides: dict[str, Any]
    fingerprint: str
    mean_scalar: float | None = None
    mean_components: dict[str, float] = {}
    tie_note: str = ""
    comparison: list[ComparisonRow] = []


class BudgetSpent(BaseModel):
    max_evaluations: int
    charged_evaluations: int = 0
    cache_hits: int = 0
    max_seconds: float
    elapsed_seconds: float = 0.0
    stop_reason: Literal["budget_evaluations", "budget_wall_clock", "space_exhausted"]


class OptimizerArtifact(BaseModel):
    schema_version: int = OPTIMIZER_SCHEMA_VERSION
    space_name: str
    seed: int
    evaluator_kind: Literal["stub", "real", "synthetic"]
    repeats: int = 1
    repeats_source: Literal["default", "operator", "noise_report"] = "default"
    noise_report_ref: str | None = None
    weights: Weights = Weights()
    space_total_combinations: int = 0
    distinct_evaluated: int = 0
    trace: list[EvaluationRecord] = []
    recommendation: Recommendation | None = None
    budget: BudgetSpent
    notes: list[str] = []

    def to_validated_json(self) -> str:
        """Round-trip guard + the FR-010 reconciliation invariants, before write."""
        if len(self.trace) != self.budget.charged_evaluations + self.budget.cache_hits:
            msg = (
                f"trace does not reconcile: {len(self.trace)} entries != "
                f"charged={self.budget.charged_evaluations} + cache_hits={self.budget.cache_hits}"
            )
            raise ValueError(msg)
        if self.budget.charged_evaluations > self.budget.max_evaluations:
            msg = "budget does not reconcile: charged evaluations exceed the cap"
            raise ValueError(msg)
        distinct = len({r.fingerprint for r in self.trace if not r.cache_hit})
        if self.distinct_evaluated != distinct:
            msg = f"coverage does not reconcile: distinct_evaluated={self.distinct_evaluated} != {distinct}"
            raise ValueError(msg)
        if self.recommendation is not None:
            by_id = {r.eval_id: r for r in self.trace}
            rec = by_id.get(self.recommendation.eval_id)
            if rec is None or rec.failed:
                msg = "recommendation does not reconcile: not an evaluated, non-failed trace entry"
                raise ValueError(msg)
        text = self.model_dump_json(indent=2)
        OptimizerArtifact.model_validate_json(text)
        return text

    def write(self, session_dir: str | Path) -> Path:
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "optimizer.json"
        path.write_text(self.to_validated_json())
        (d / "optimizer-report.md").write_text(self.render_markdown())
        return path

    @classmethod
    def load(cls, path: str | Path) -> OptimizerArtifact:
        return cls.model_validate_json(Path(path).read_text())

    def render_markdown(self) -> str:
        b = self.budget
        lines = [
            f"# Optimizer report — {self.space_name} (seed {self.seed}, {self.evaluator_kind})",
            "",
            f"- budget: {b.charged_evaluations}/{b.max_evaluations} evaluations charged "
            f"(+{b.cache_hits} cache hits), {b.elapsed_seconds:.1f}s of {b.max_seconds:.0f}s",
            f"- stop reason: **{b.stop_reason}**",
            f"- coverage: {self.distinct_evaluated}/{self.space_total_combinations} distinct configurations",
            f"- repeats per evaluation: {self.repeats} (source: {self.repeats_source}"
            + (f", {self.noise_report_ref}" if self.noise_report_ref else "")
            + ")",
        ]
        lines += ["", "## Recommendation", ""]
        if self.recommendation is None:
            lines.append("**None** — every evaluation failed; see the trace.")
        else:
            r = self.recommendation
            lines.append(f"- eval #{r.eval_id}, scalar **{r.mean_scalar}**, fingerprint `{r.fingerprint}`")
            if r.tie_note:
                lines.append(f"- tie: {r.tie_note}")
            lines.extend(f"  - `{k}` = `{v!r}`" for k, v in sorted(r.overrides.items()))
            lines += ["", "### Head-to-head", "",
                      "| config | scalar | Δ vs recommendation | fingerprint |", "|---|---|---|---|"]
            for row in r.comparison:
                dup = " (identical)" if row.duplicate_of_recommendation and row.label != "recommendation" else ""
                delta = "" if row.delta_vs_recommendation is None else f"{row.delta_vs_recommendation:+.6g}"
                lines.append(f"| {row.label}{dup} | {row.mean_scalar} | {delta} | `{row.fingerprint}` |")
        lines += ["", "## Trace", "", "| # | provenance | scalar | best so far | cached | fingerprint |",
                  "|---|---|---|---|---|---|"]
        for t in self.trace:
            scalar = "FAILED" if t.failed else str(t.mean_scalar)
            lines.append(
                f"| {t.eval_id} | {t.provenance} | {scalar} | {t.best_so_far} | "
                f"{'yes' if t.cache_hit else ''} | `{t.fingerprint}` |"
            )
        if self.notes:
            lines += ["", "## Notes", ""]
            lines.extend(f"- {n}" for n in self.notes)
        lines.append("")
        return "\n".join(lines)


def real_evaluator(
    loaded: LoadedSpace,
    fixtures: list[Fixture],
    session_dir: str | Path,
    *,
    repeats: int = 1,
    weights: Weights | None = None,
    judge: JudgeFn | None = None,
    judge_model: str | None = None,
    stub: bool = True,
    human_login: str = "reviewer1",
    max_cycles: int | None = None,
    cost_per_million_tokens: float = 3.0,
) -> Evaluator:
    """The shipped evaluator: materialize → run_board(config) → score_run x repeats.

    Mirrors ``sweep._run_point`` semantics: per-repeat run.json + score.json
    under ``<session>/<eval_id>/<repeat>/``, mean scalar/components, failures
    captured (returned, never raised)."""
    session = Path(session_dir)
    weights = weights or Weights()

    async def evaluate(
        eval_id: int, overrides: dict[str, Any], config: CoordinareConfiguration
    ) -> EvalOutcome:
        try:
            scores: list[ScoreObject] = []
            refs: list[str] = []
            for i in range(1, repeats + 1):
                repeat_dir = session / str(eval_id) / str(i)
                artifact = await run_board(
                    fixtures, repeat_dir,
                    human_login=human_login, max_cycles=max_cycles,
                    cost_per_million_tokens=cost_per_million_tokens,
                    stub=stub, config=config,
                )
                score = score_run(
                    artifact, fixtures, weights=weights, judge=judge, judge_model=judge_model
                )
                score.write(repeat_dir)
                scores.append(score)
                refs.append(str(repeat_dir.relative_to(session) / "score.json"))
            scalars = [s.scalar for s in scores if s.scalar is not None]
            components: dict[str, float] = {}
            for name in _NUMERIC_COMPONENTS:
                values = [float(v) for s in scores if (v := getattr(s.components, name)) is not None]
                if values:
                    components[name] = statistics.fmean(values)
            return EvalOutcome(
                repeats=repeats,
                score_refs=refs,
                mean_scalar=statistics.fmean(scalars) if scalars else None,
                mean_components=components,
            )
        except Exception as exc:  # a failed evaluation is data, not a crash (FR-007)
            return EvalOutcome(repeats=repeats, failed=True, error=f"{type(exc).__name__}: {exc}")

    return evaluate


def _coords_key(coords: dict[str, Any]) -> tuple[str, ...]:
    return tuple(f"{k}={coords[k]!r}" for k in sorted(coords))


async def run_optimizer(
    loaded: LoadedSpace,
    evaluate: Evaluator,
    *,
    seed: int = 1,
    max_evaluations: int = 30,
    max_seconds: float = 3600.0,
    repeats: int = 1,
    repeats_source: Literal["default", "operator", "noise_report"] = "default",
    noise_report_ref: str | None = None,
    weights: Weights | None = None,
    evaluator_kind: Literal["stub", "real", "synthetic"] = "stub",
    session_dir: str | Path | None = None,
    extra_notes: list[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> OptimizerArtifact:
    """Run one budgeted search; write (when ``session_dir`` given) and return the artifact."""
    rng = random.Random(seed)
    dims = loaded.definition.dimensions
    paths = [d.path for d in dims]
    axes = {d.path: list(dict.fromkeys(d.choices)) for d in dims}
    total_combinations = 1
    for axis in axes.values():
        total_combinations *= len(axis)

    # The full grid in a seeded-shuffled order — the deterministic restart queue
    # and the exhaustion authority (low hundreds of combos; research R5).
    all_combos = [dict(zip(paths, values, strict=True)) for values in itertools.product(*axes.values())]
    rng.shuffle(all_combos)

    baseline_coords = {p: resolve_path(loaded.baseline_dump, p) for p in paths}

    trace: list[EvaluationRecord] = []
    memo: dict[str, EvaluationRecord] = {}  # fingerprint -> first record
    seen_coords: set[tuple[str, ...]] = set()
    charged = 0
    cache_hits = 0
    best: EvaluationRecord | None = None  # best charged non-failed (grid or candidate)
    best_grid: tuple[EvaluationRecord, dict[str, Any]] | None = None
    stop_reason: str | None = None
    start = clock()
    notes: list[str] = list(extra_notes or [])
    if evaluator_kind == "stub":
        notes.append(
            "substrate=stub: gate/model dimensions carry no signal (136 ablation finding) — "
            "this search validates the machinery; real recommendations require a --real "
            "search gated on a real-substrate noise report (135)"
        )

    def _overrides_for(coords: dict[str, Any]) -> dict[str, Any]:
        return {p: v for p, v in coords.items() if v != baseline_coords.get(p)}

    def _best_scalar() -> float | None:
        return best.mean_scalar if best is not None else None

    async def _evaluate(provenance: str, overrides: dict[str, Any]) -> EvaluationRecord:
        nonlocal charged, cache_hits, best
        config = materialize(loaded.baseline_dump, overrides)
        fingerprint = config_fingerprint(config)
        eval_id = len(trace) + 1
        cached = memo.get(fingerprint)
        if cached is not None:
            record = EvaluationRecord(
                eval_id=eval_id, provenance=provenance, overrides=overrides,
                fingerprint=fingerprint, cache_hit=True, repeats=cached.repeats,
                mean_scalar=cached.mean_scalar, mean_components=cached.mean_components,
                failed=cached.failed, error=cached.error, best_so_far=_best_scalar(),
            )
            cache_hits += 1
            trace.append(record)
            return record
        outcome = await evaluate(eval_id, overrides, config)
        charged += 1
        record = EvaluationRecord(
            eval_id=eval_id, provenance=provenance, overrides=overrides,
            fingerprint=fingerprint, repeats=outcome.repeats, score_refs=outcome.score_refs,
            mean_scalar=outcome.mean_scalar, mean_components=outcome.mean_components,
            failed=outcome.failed, error=outcome.error,
        )
        if (
            not record.failed
            and record.mean_scalar is not None
            and (best is None or best.mean_scalar is None or record.mean_scalar > best.mean_scalar)
        ):
            best = record
        record.best_so_far = _best_scalar()
        memo[fingerprint] = record
        trace.append(record)
        logger.info("bench.optimizer_eval", eval_id=eval_id, provenance=provenance,
                    scalar=record.mean_scalar, cached=False)
        return record

    def _budget_stop() -> str | None:
        if charged >= max_evaluations:
            return "budget_evaluations"
        if clock() - start >= max_seconds:
            return "budget_wall_clock"
        return None

    # --- 1) baseline + candidates (the FR-009 comparison set; charged) ---
    seed_queue: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = [
        ("baseline", {}, baseline_coords)
    ]
    seed_queue.extend(
        (f"candidate:{c.name}", dict(c.overrides), None) for c in loaded.definition.candidates
    )
    for i, (provenance, overrides, coords) in enumerate(seed_queue):
        stop_reason = _budget_stop()
        if stop_reason:
            # The shortfall must be stated, never silent (FR-009/FR-010, spec
            # edge case): name every comparison-set member the budget cut off.
            skipped = [p for p, _, _ in seed_queue[i:]]
            notes.append(
                "budget exhausted before evaluating the comparison set: "
                f"{', '.join(skipped)} — absent from the head-to-head (shortfall)"
            )
            break
        record = await _evaluate(provenance, overrides)
        if coords is not None:
            seen_coords.add(_coords_key(coords))
            if not record.failed and record.mean_scalar is not None:
                best_grid = (record, coords)

    # --- 2) best-first mutation with seeded restarts over the grid ---
    while stop_reason is None:
        stop_reason = _budget_stop()
        if stop_reason:
            break
        proposal: tuple[str, dict[str, Any]] | None = None
        if best_grid is not None:
            parent_record, parent_coords = best_grid
            neighbors = [
                {**parent_coords, path: value}
                for path in paths
                for value in axes[path]
                if value != parent_coords.get(path)
            ]
            neighbors = [n for n in neighbors if _coords_key(n) not in seen_coords]
            if neighbors:
                proposal = (f"mutation-of:{parent_record.eval_id}", rng.choice(neighbors))
        if proposal is None:
            restart = next((c for c in all_combos if _coords_key(c) not in seen_coords), None)
            if restart is None:
                stop_reason = "space_exhausted"
                break
            proposal = ("restart", restart)

        provenance, coords = proposal
        seen_coords.add(_coords_key(coords))
        record = await _evaluate(provenance, _overrides_for(coords))
        if not record.failed and record.mean_scalar is not None and (
            best_grid is None
            or best_grid[0].mean_scalar is None
            or record.mean_scalar > best_grid[0].mean_scalar
        ):
            best_grid = (record, coords)

    elapsed = clock() - start
    if stop_reason == "budget_wall_clock":
        overshoot = elapsed - max_seconds
        if overshoot > 0:
            notes.append(
                f"wall-clock cap exceeded by {overshoot:.1f}s — the in-flight evaluation "
                "was completed before stopping"
            )
        else:
            notes.append("wall-clock cap reached; no new evaluation started past the cap")

    # --- 3) recommendation + head-to-head ---
    recommendation: Recommendation | None = None
    scored = [r for r in trace if not r.cache_hit and not r.failed and r.mean_scalar is not None]
    if scored:
        top = max(r.mean_scalar for r in scored)  # type: ignore[type-var]
        winners = [r for r in scored if r.mean_scalar == top]
        winner = min(winners, key=lambda r: r.eval_id)
        tie_note = (
            f"{len(winners)} configurations tied at {top}; earliest evaluation recommended"
            if len(winners) > 1 else ""
        )
        rows = [ComparisonRow(
            label="recommendation", fingerprint=winner.fingerprint,
            mean_scalar=winner.mean_scalar, delta_vs_recommendation=0.0,
            duplicate_of_recommendation=True,
        )]
        for r in trace:
            if r.provenance == "baseline" or r.provenance.startswith("candidate:"):
                delta = (
                    None if r.mean_scalar is None or winner.mean_scalar is None
                    else round(r.mean_scalar - winner.mean_scalar, 9)
                )
                rows.append(ComparisonRow(
                    label=r.provenance, fingerprint=r.fingerprint, mean_scalar=r.mean_scalar,
                    delta_vs_recommendation=delta,
                    duplicate_of_recommendation=r.fingerprint == winner.fingerprint,
                ))
        recommendation = Recommendation(
            eval_id=winner.eval_id, overrides=winner.overrides, fingerprint=winner.fingerprint,
            mean_scalar=winner.mean_scalar, mean_components=winner.mean_components,
            tie_note=tie_note, comparison=rows,
        )
    else:
        notes.append("no recommendation: every evaluation failed or was unrankable")

    artifact = OptimizerArtifact(
        space_name=loaded.definition.name,
        seed=seed,
        evaluator_kind=evaluator_kind,
        repeats=repeats,
        repeats_source=repeats_source,
        noise_report_ref=noise_report_ref,
        weights=weights or Weights(),
        space_total_combinations=total_combinations,
        distinct_evaluated=len({r.fingerprint for r in trace if not r.cache_hit}),
        trace=trace,
        recommendation=recommendation,
        budget=BudgetSpent(
            max_evaluations=max_evaluations,
            charged_evaluations=charged,
            cache_hits=cache_hits,
            max_seconds=max_seconds,
            elapsed_seconds=round(elapsed, 3),
            stop_reason=stop_reason,  # type: ignore[arg-type]
        ),
        notes=notes,
    )
    if session_dir is not None:
        artifact.write(session_dir)
    return artifact
