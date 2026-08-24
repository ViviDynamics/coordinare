"""Spec 136 — the sweep runner: non-adaptive exploration of a config search space.

Two modes over a loaded space (``bench/space.py``):

* **ablation** — the baseline once, plus one run per (dimension, non-baseline
  value); reports each dimension's marginal effect (scalar + component deltas
  vs the same sweep's baseline).
* **candidates** — each named whole-config candidate, ranked head-to-head by
  mean scalar (unrankable last, marked; duplicate fingerprints flagged).

Every point is materialized into a real root configuration, injected into the
134 substrate (``run_board(config=…)``), and scored with the 135 scorer,
``repeats`` times. A point failure never aborts the sweep — it is recorded and
counted as dropped coverage. The artifact enforces the honesty invariant
``declared == scored + dropped`` (FR-010/SC-004) before it will write itself.

Contract: ``specs/136-board-bench-sweep/contracts/sweep-artifact.md``.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel

from coordinare.bench.grader import score_run
from coordinare.bench.runner import run_board
from coordinare.bench.score import Weights
from coordinare.bench.space import config_fingerprint, materialize, resolve_path

if TYPE_CHECKING:
    from coordinare.bench.fixtures import Fixture
    from coordinare.bench.judge import JudgeFn
    from coordinare.bench.noise import NoiseReport
    from coordinare.bench.score import ScoreObject
    from coordinare.bench.space import LoadedSpace

logger = structlog.get_logger(__name__)

SWEEP_SCHEMA_VERSION = 1

# The 135 components averaged into mean_components when numeric and present.
_NUMERIC_COMPONENTS = (
    "correctness_rate",
    "harness_failure_rate",
    "tokens_processed",
    "cost_usd",
    "wall_clock_seconds",
)


class PointResult(BaseModel):
    point_id: str
    kind: Literal["baseline", "ablation", "candidate"]
    dimension: str | None = None
    value: Any = None
    candidate: str | None = None
    fingerprint: str = ""
    duplicate_of: str | None = None
    score_refs: list[str] = []
    mean_scalar: float | None = None
    mean_components: dict[str, float] = {}
    failed: bool = False
    error: str = ""


class AblationDelta(BaseModel):
    dimension: str
    value: Any = None
    point_id: str
    scalar_delta: float | None = None
    component_deltas: dict[str, float] = {}


class DroppedPoint(BaseModel):
    point_id: str
    reason: str


class Coverage(BaseModel):
    declared_points: int = 0
    scored_points: int = 0
    dropped: list[DroppedPoint] = []
    notes: list[str] = []


class SweepArtifact(BaseModel):
    schema_version: int = SWEEP_SCHEMA_VERSION
    space_name: str
    mode: Literal["ablation", "candidates"]
    substrate_mode: Literal["stub", "real"]
    repeats: int
    repeats_source: Literal["default", "operator", "noise_report"]
    noise_report_ref: str | None = None
    weights: Weights = Weights()
    baseline: PointResult | None = None
    points: list[PointResult] = []
    deltas: list[AblationDelta] = []
    ranking: list[str] = []
    coverage: Coverage = Coverage()

    def to_validated_json(self) -> str:
        """Round-trip guard + the SC-004 honesty invariant, before write."""
        if self.coverage.declared_points != self.coverage.scored_points + len(self.coverage.dropped):
            msg = (
                f"coverage does not reconcile: declared={self.coverage.declared_points} "
                f"!= scored={self.coverage.scored_points} + dropped={len(self.coverage.dropped)}"
            )
            raise ValueError(msg)
        text = self.model_dump_json(indent=2)
        SweepArtifact.model_validate_json(text)
        return text

    def write(self, session_dir: str | Path) -> Path:
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "sweep.json"
        path.write_text(self.to_validated_json())
        (d / "sweep-report.md").write_text(self.render_markdown())
        return path

    @classmethod
    def load(cls, path: str | Path) -> SweepArtifact:
        return cls.model_validate_json(Path(path).read_text())

    def render_markdown(self) -> str:
        lines = [
            f"# Sweep report — {self.space_name} ({self.mode})",
            "",
            f"- substrate mode: **{self.substrate_mode}**",
        ]
        if self.substrate_mode == "stub":
            lines.append(
                "  - *fidelity caveat: the stub bypasses dispatch, so gate dimensions are "
                "exercised structurally (materialize → inject → run → score) but most gate "
                "deltas are expected ~0 until real-performer runs land (spec-134 follow-up).*"
            )
        lines.append(
            f"- repeats per point: {self.repeats} (source: {self.repeats_source}"
            + (f", {self.noise_report_ref}" if self.noise_report_ref else "")
            + ")"
        )
        lines += ["", "## Points", "", "| point | kind | mean scalar | correctness | wall clock (s) | fingerprint |",
                  "|---|---|---|---|---|---|"]
        rows = ([self.baseline] if self.baseline else []) + list(self.points)
        for p in rows:
            scalar = "FAILED" if p.failed else ("unrankable" if p.mean_scalar is None else f"{p.mean_scalar:.6g}")
            corr = p.mean_components.get("correctness_rate")
            wall = p.mean_components.get("wall_clock_seconds")
            dup = f" (=**{p.duplicate_of}**)" if p.duplicate_of else ""
            lines.append(
                f"| {p.point_id}{dup} | {p.kind} | {scalar} | "
                f"{'' if corr is None else f'{corr:.3f}'} | "
                f"{'' if wall is None else f'{wall:.2f}'} | `{p.fingerprint}` |"
            )
        if self.deltas:
            lines += ["", "## Ablation — marginal effect vs baseline", "",
                      "| dimension | value | Δ scalar | Δ correctness | Δ wall clock (s) |",
                      "|---|---|---|---|---|"]
            for dl in self.deltas:
                lines.append(
                    f"| {dl.dimension} | {dl.value!r} | "
                    f"{'' if dl.scalar_delta is None else f'{dl.scalar_delta:+.6g}'} | "
                    f"{dl.component_deltas.get('correctness_rate', 0.0):+.3f} | "
                    f"{dl.component_deltas.get('wall_clock_seconds', 0.0):+.2f} |"
                )
        if self.ranking:
            lines += ["", "## Ranking", ""]
            lines.extend(f"{i}. `{pid}`" for i, pid in enumerate(self.ranking, start=1))
        lines += ["", "## Coverage", "",
                  f"- declared: {self.coverage.declared_points}, scored: {self.coverage.scored_points}, "
                  f"dropped: {len(self.coverage.dropped)}"]
        lines.extend(f"- DROPPED `{d.point_id}`: {d.reason}" for d in self.coverage.dropped)
        lines.extend(f"- note: {n}" for n in self.coverage.notes)
        lines.append("")
        return "\n".join(lines)


@dataclass
class PointSpec:
    """One enumerated config point, before execution."""

    point_id: str
    kind: Literal["baseline", "ablation", "candidate"]
    overrides: dict[str, Any] = field(default_factory=dict)
    dimension: str | None = None
    value: Any = None
    candidate: str | None = None


def enumerate_ablation(loaded: LoadedSpace) -> list[PointSpec]:
    """One PointSpec per (dimension, non-baseline value). Baseline-coinciding
    choices are skipped (they'd duplicate the baseline run — spec edge case);
    the skip is surfaced as a coverage note by run_sweep."""
    points: list[PointSpec] = []
    for dim in loaded.definition.dimensions:
        baseline_value = resolve_path(loaded.baseline_dump, dim.path)
        for choice in dim.choices:
            if choice == baseline_value:
                continue
            points.append(PointSpec(
                point_id=f"{dim.name}={choice}",
                kind="ablation",
                overrides={dim.path: choice},
                dimension=dim.name,
                value=choice,
            ))
    return points


def enumerate_candidates(loaded: LoadedSpace) -> list[PointSpec]:
    return [
        PointSpec(
            point_id=f"candidate:{c.name}",
            kind="candidate",
            overrides=dict(c.overrides),
            candidate=c.name,
        )
        for c in loaded.definition.candidates
    ]


def compute_deltas(baseline: PointResult, points: list[PointResult]) -> list[AblationDelta]:
    """Marginal effect of each scored ablation point vs the same sweep's baseline."""
    if baseline.failed or baseline.mean_scalar is None:
        return []
    deltas: list[AblationDelta] = []
    for p in points:
        if p.failed or p.mean_scalar is None or p.dimension is None:
            continue
        deltas.append(AblationDelta(
            dimension=p.dimension,
            value=p.value,
            point_id=p.point_id,
            scalar_delta=round(p.mean_scalar - baseline.mean_scalar, 9),
            component_deltas={
                k: round(p.mean_components[k] - baseline.mean_components[k], 9)
                for k in p.mean_components
                if k in baseline.mean_components
            },
        ))
    return deltas


def rank_candidates(points: list[PointResult]) -> list[str]:
    """Point ids by mean scalar descending; unrankable (failed/None) last."""
    rankable = [p for p in points if not p.failed and p.mean_scalar is not None]
    unrankable = [p for p in points if p.failed or p.mean_scalar is None]
    ordered = sorted(rankable, key=lambda p: p.mean_scalar, reverse=True)
    return [p.point_id for p in ordered] + [p.point_id for p in unrankable]


def flag_duplicates(points: list[PointResult]) -> None:
    """Mark points whose materialized fingerprint repeats an earlier point's."""
    seen: dict[str, str] = {}
    for p in points:
        if p.fingerprint in seen:
            p.duplicate_of = seen[p.fingerprint]
        else:
            seen[p.fingerprint] = p.point_id


def derive_repeats(
    noise_report: NoiseReport | None,
    *,
    default: int = 1,
    resolution: float = 0.01,
    max_repeats: int = 10,
) -> tuple[int, Literal["default", "noise_report"], str]:
    """Repeats from a 135 noise report's scalar spread (research R4).

    n = ceil((stdev/resolution)²) — the point where the standard error of the
    mean falls to ``resolution`` — clamped to [1, max_repeats]. An unusable
    report (no scalar stats / zero effective repeats) falls back to ``default``
    with a note (spec edge case). Returns (repeats, source, note)."""
    if noise_report is None:
        return default, "default", ""
    stats = noise_report.scalar_stats
    if stats is None or noise_report.effective_repeats < 1:
        return default, "default", "noise report unusable (no scalar stats) — fell back to default repeats"
    raw = math.ceil((stats.stdev / resolution) ** 2)
    if raw > max_repeats:
        return max_repeats, "noise_report", f"derived repeats {raw} hit the cap {max_repeats}"
    return max(1, raw), "noise_report", ""


def _mean_components(scores: list[ScoreObject]) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in _NUMERIC_COMPONENTS:
        values = [float(v) for s in scores if (v := getattr(s.components, name)) is not None]
        if values:
            out[name] = statistics.fmean(values)
    return out


async def _run_point(
    spec: PointSpec,
    loaded: LoadedSpace,
    fixtures: list[Fixture],
    session: Path,
    *,
    repeats: int,
    weights: Weights,
    judge: JudgeFn | None,
    judge_model: str | None,
    stub: bool,
    human_login: str,
    max_cycles: int | None,
    cost_per_million_tokens: float,
) -> PointResult:
    result = PointResult(
        point_id=spec.point_id, kind=spec.kind, dimension=spec.dimension,
        value=spec.value, candidate=spec.candidate,
    )
    try:
        cfg = materialize(loaded.baseline_dump, spec.overrides)
        result.fingerprint = config_fingerprint(cfg)
        scores: list[ScoreObject] = []
        for i in range(1, repeats + 1):
            repeat_dir = session / spec.point_id.replace("/", "_") / str(i)
            artifact = await run_board(
                fixtures, repeat_dir,
                human_login=human_login, max_cycles=max_cycles,
                cost_per_million_tokens=cost_per_million_tokens,
                stub=stub, config=cfg,
            )
            score = score_run(artifact, fixtures, weights=weights, judge=judge, judge_model=judge_model)
            score.write(repeat_dir)
            scores.append(score)
            result.score_refs.append(str(repeat_dir.relative_to(session) / "score.json"))
        scalars = [s.scalar for s in scores if s.scalar is not None]
        result.mean_scalar = statistics.fmean(scalars) if scalars else None
        result.mean_components = _mean_components(scores)
        logger.info("bench.sweep_point_done", point=spec.point_id, scalar=result.mean_scalar)
    except Exception as exc:  # a failed point is data, not a crash (FR-009)
        result.failed = True
        result.error = f"{type(exc).__name__}: {exc}"
        logger.warning("bench.sweep_point_failed", point=spec.point_id, error=result.error)
    return result


async def run_sweep(
    loaded: LoadedSpace,
    mode: Literal["ablation", "candidates"],
    session_dir: str | Path,
    *,
    fixtures: list[Fixture] | None = None,
    repeats: int = 1,
    repeats_source: Literal["default", "operator", "noise_report"] = "default",
    noise_report_ref: str | None = None,
    repeat_notes: list[str] | None = None,
    weights: Weights | None = None,
    judge: JudgeFn | None = None,
    judge_model: str | None = None,
    stub: bool = True,
    human_login: str = "reviewer1",
    max_cycles: int | None = None,
    cost_per_million_tokens: float = 3.0,
) -> SweepArtifact:
    """Execute one sweep; write and return the validated artifact."""
    from coordinare.bench.fixtures import tiny_fixture  # local: avoids cycle at import

    session = Path(session_dir)
    session.mkdir(parents=True, exist_ok=True)
    fixtures = fixtures or [tiny_fixture()]
    weights = weights or Weights()

    if mode == "ablation":
        specs = [PointSpec(point_id="baseline", kind="baseline"), *enumerate_ablation(loaded)]
    else:
        specs = enumerate_candidates(loaded)
    logger.info("bench.sweep_start", mode=mode, declared_points=len(specs), repeats=repeats)

    run_kwargs: dict[str, Any] = dict(
        repeats=repeats, weights=weights, judge=judge, judge_model=judge_model,
        stub=stub, human_login=human_login, max_cycles=max_cycles,
        cost_per_million_tokens=cost_per_million_tokens,
    )
    results = [await _run_point(spec, loaded, fixtures, session, **run_kwargs) for spec in specs]

    baseline = next((r for r in results if r.kind == "baseline"), None)
    points = [r for r in results if r.kind != "baseline"]
    flag_duplicates(([baseline] if baseline else []) + points)

    dropped = [DroppedPoint(point_id=r.point_id, reason=r.error or "unrankable score (no scalar)")
               for r in results if r.failed or r.mean_scalar is None]
    notes = list(repeat_notes or [])
    skipped = _baseline_coinciding_notes(loaded) if mode == "ablation" else []
    notes.extend(skipped)
    if stub:
        notes.append(
            "substrate=stub: gate dimensions exercised structurally; most gate deltas "
            "expected ~0 until real-performer runs land (spec-134 follow-up)"
        )

    artifact = SweepArtifact(
        space_name=loaded.definition.name,
        mode=mode,
        substrate_mode="stub" if stub else "real",
        repeats=repeats,
        repeats_source=repeats_source,
        noise_report_ref=noise_report_ref,
        weights=weights,
        baseline=baseline,
        points=points,
        deltas=compute_deltas(baseline, points) if baseline is not None else [],
        ranking=rank_candidates(points) if mode == "candidates" else [],
        coverage=Coverage(
            declared_points=len(results),
            scored_points=len(results) - len(dropped),
            dropped=dropped,
            notes=notes,
        ),
    )
    artifact.write(session)
    return artifact


def _baseline_coinciding_notes(loaded: LoadedSpace) -> list[str]:
    notes = []
    for dim in loaded.definition.dimensions:
        baseline_value = resolve_path(loaded.baseline_dump, dim.path)
        if baseline_value in dim.choices:
            notes.append(
                f"dimension {dim.name!r}: choice {baseline_value!r} coincides with the "
                "baseline and was not re-run"
            )
    return notes
