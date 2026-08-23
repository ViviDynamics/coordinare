"""Spec 135 — noise characterization: N same-config runs → one aggregate report.

``run_repeats`` executes the same configuration N times (``run_board`` + score per
repeat) and aggregates per-component sample statistics, scalar statistics, and
per-card verdict agreement. Failed repeats are first-class: listed with their
error, excluded from the statistics, and the report carries both requested and
effective N — silent drops would understate noise.

Contract: ``specs/135-board-bench-scoring/contracts/noise-report.md``.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from pydantic import BaseModel

from coordinare.bench.grader import score_run
from coordinare.bench.runner import run_board

if TYPE_CHECKING:
    from coordinare.bench.fixtures import Fixture
    from coordinare.bench.judge import JudgeFn
    from coordinare.bench.score import ScoreObject, Weights

logger = structlog.get_logger(__name__)

NOISE_SCHEMA_VERSION = 1

# The numeric per-run components aggregated into the report.
_NUMERIC_COMPONENTS = (
    "correctness_rate",
    "harness_failure_rate",
    "tokens_processed",
    "cost_usd",
    "wall_clock_seconds",
)


class NoiseError(ValueError):
    """Aggregation that cannot answer 'how noisy is THIS config' fails loudly."""


class ComponentStats(BaseModel):
    mean: float
    variance: float
    stdev: float
    min: float
    max: float
    n: int
    single_sample: bool = False


class RepeatFailure(BaseModel):
    index: int
    error: str


class NoiseReport(BaseModel):
    schema_version: int = NOISE_SCHEMA_VERSION
    config_fingerprint: str = ""
    requested_repeats: int
    effective_repeats: int
    failures: list[RepeatFailure] = []
    component_stats: dict[str, ComponentStats] = {}
    scalar_stats: ComponentStats | None = None
    card_agreement: dict[str, float] = {}
    score_refs: list[str] = []

    def to_validated_json(self) -> str:
        """Round-trip guard before the report is declared written (134/135 convention)."""
        text = self.model_dump_json(indent=2)
        NoiseReport.model_validate_json(text)
        return text

    def write(self, session_dir: str | Path) -> Path:
        """Write ``noise-report.json`` + a rendered ``noise-report.md``."""
        d = Path(session_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "noise-report.json"
        path.write_text(self.to_validated_json())
        (d / "noise-report.md").write_text(self.render_markdown())
        return path

    @classmethod
    def load(cls, path: str | Path) -> NoiseReport:
        return cls.model_validate_json(Path(path).read_text())

    def render_markdown(self) -> str:
        lines = [
            "# Noise report",
            "",
            f"- config fingerprint: `{self.config_fingerprint or 'none'}`",
            f"- repeats: {self.effective_repeats} effective of {self.requested_repeats} requested",
        ]
        if self.failures:
            lines.append("- failed repeats:")
            lines.extend(f"  - #{f.index}: {f.error}" for f in self.failures)
        lines += ["", "| component | mean | variance | stdev | min | max | n |", "|---|---|---|---|---|---|---|"]
        rows = dict(self.component_stats)
        if self.scalar_stats is not None:
            rows["scalar"] = self.scalar_stats
        for name, s in rows.items():
            flag = " (single sample)" if s.single_sample else ""
            lines.append(
                f"| {name} | {s.mean:.6g} | {s.variance:.6g} | {s.stdev:.6g} "
                f"| {s.min:.6g} | {s.max:.6g} | {s.n}{flag} |"
            )
        if self.card_agreement:
            lines += ["", "## Per-card verdict agreement (fraction agreeing with modal verdict)", ""]
            lines.extend(f"- `{fx}`: {frac:.3f}" for fx, frac in sorted(self.card_agreement.items()))
        lines.append("")
        return "\n".join(lines)


def component_stats(values: list[float]) -> ComponentStats | None:
    """Sample statistics (n-1 variance); ``None`` when no values are present."""
    if not values:
        return None
    single = len(values) < 2
    return ComponentStats(
        mean=statistics.fmean(values),
        variance=0.0 if single else statistics.variance(values),
        stdev=0.0 if single else statistics.stdev(values),
        min=min(values),
        max=max(values),
        n=len(values),
        single_sample=single,
    )


def aggregate(
    runs: list[tuple[str, ScoreObject, str]],
    *,
    requested_repeats: int,
    failures: list[RepeatFailure],
) -> NoiseReport:
    """Aggregate ``(config_fingerprint, score, score_ref)`` triples into a report."""
    fingerprints = {fp for fp, _, _ in runs}
    if len(fingerprints) > 1:
        msg = f"repeats span multiple config fingerprints {sorted(fingerprints)} — not one config's noise"
        raise NoiseError(msg)

    scores = [s for _, s, _ in runs]
    comp_stats: dict[str, ComponentStats] = {}
    for name in _NUMERIC_COMPONENTS:
        values = [
            float(v) for s in scores if (v := getattr(s.components, name)) is not None
        ]
        stats = component_stats(values)
        if stats is not None:
            comp_stats[name] = stats

    verdicts_by_fixture: dict[str, list[str]] = defaultdict(list)
    for s in scores:
        for card in s.cards:
            verdicts_by_fixture[card.fixture_id].append(card.category)
    agreement = {
        fx: Counter(cats).most_common(1)[0][1] / len(cats)
        for fx, cats in verdicts_by_fixture.items()
    }

    return NoiseReport(
        config_fingerprint=next(iter(fingerprints), ""),
        requested_repeats=requested_repeats,
        effective_repeats=len(runs),
        failures=failures,
        component_stats=comp_stats,
        scalar_stats=component_stats([s.scalar for s in scores if s.scalar is not None]),
        card_agreement=agreement,
        score_refs=[ref for _, _, ref in runs],
    )


async def run_repeats(
    fixtures: list[Fixture],
    repeats: int,
    session_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    human_login: str = "reviewer1",
    max_cycles: int | None = None,
    cost_per_million_tokens: float = 3.0,
    stub: bool = True,
    weights: Weights | None = None,
    judge: JudgeFn | None = None,
    judge_model: str | None = None,
) -> NoiseReport:
    """Run + score the same config ``repeats`` times; write and return the report."""
    session = Path(session_dir)
    runs: list[tuple[str, ScoreObject, str]] = []
    failures: list[RepeatFailure] = []
    for i in range(1, repeats + 1):
        repeat_dir = session / str(i)
        started = time.monotonic()
        try:
            artifact = await run_board(
                fixtures,
                repeat_dir,
                config_path=config_path,
                human_login=human_login,
                max_cycles=max_cycles,
                cost_per_million_tokens=cost_per_million_tokens,
                stub=stub,
            )
            score = score_run(
                artifact, fixtures, weights=weights, judge=judge, judge_model=judge_model
            )
            score.write(repeat_dir)
            runs.append((artifact.config_fingerprint.hash, score, f"{i}/score.json"))
            logger.info(
                "bench.noise_repeat_done",
                repeat=i,
                scalar=score.scalar,
                seconds=round(time.monotonic() - started, 3),
            )
        except Exception as exc:  # a failed repeat is data, not a crash
            failures.append(RepeatFailure(index=i, error=f"{type(exc).__name__}: {exc}"))
            logger.warning("bench.noise_repeat_failed", repeat=i, error=str(exc))

    report = aggregate(runs, requested_repeats=repeats, failures=failures)
    report.write(session)
    return report
