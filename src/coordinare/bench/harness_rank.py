"""Spec 161 — score and rank backend harnesses per role.

The harness-comparison view (FR-010): harness defects count AGAINST the harness. That is
the opposite of the config-comparison view, which quarantines them as noise (spec-135
FR-005), and the inversion is deliberate — when the harness is the thing under test, its
defects are the signal. The two live side by side rather than one replacing the other, so
fixed-harness config sweeps keep the semantics they always had (FR-011).

Three rules keep the output honest rather than merely decisive:

* **Absent is not zero.** A pair with no conclusive dispatches gets ``scalar = None``, and
  a pair below the evidence floor is reported as insufficient rather than ranked.
* **A tie is a tie.** Candidates within the noise band are reported as tied instead of
  ordered by a difference the data cannot support.
* **No least-bad winner.** When every candidate fails, the verdict says no viable harness
  rather than crowning the least broken one.

The ranking is **advisory output only** (FR-023): it writes a file and never touches live
deployment configuration.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from coordinare.bench.artifact import RunArtifact, estimate_cost_usd
from coordinare.bench.harness_rollup import HarnessRollup, RoleHarnessRow, roll_up
from coordinare.bench.noise import component_stats
from coordinare.bench.score import ScoringView, Weights

RANK_SCHEMA_VERSION = 1

#: Minimum conclusive dispatches before a (role, backend) pair is rankable (FR-021).
#: Three because a rate over one or two dispatches is not meaningfully distinguishable
#: from noise, and presenting it as a ranking would overstate what was measured.
DEFAULT_MIN_CONCLUSIVE = 3

#: Token-rate used to convert tokens into the cost term's units. Mirrors the sweep's own
#: ``cost_per_million_tokens`` default.
DEFAULT_COST_PER_MILLION_TOKENS = 0.0

Verdict = Literal["ranked", "tie", "insufficient_evidence", "no_viable_harness"]


class HarnessScore(BaseModel):
    """One ``(role, backend)`` pair's harness-comparison score (FR-010, FR-024)."""

    role: str
    backend: str
    view: ScoringView = ScoringView.HARNESS_COMPARISON
    weights: Weights = Weights()
    credit_rate: float | None = None
    seconds: float = 0.0
    tokens: int | None = None
    scalar: float | None = None
    cost_component_missing: bool = False
    evidence: RoleHarnessRow


class HarnessRanking(BaseModel):
    """The advisory per-role result (FR-019 through FR-023, FR-027)."""

    role: str
    ordered: list[HarnessScore] = Field(default_factory=list)
    #: Pairs of candidates that cannot be distinguished, NOT transitive groups — see
    #: _ties_within_band for why merging a tie chain would overstate the finding.
    ties: list[list[str]] = Field(default_factory=list)
    insufficient_evidence: list[str] = Field(default_factory=list)
    verdict: Verdict = "insufficient_evidence"
    min_conclusive: int = DEFAULT_MIN_CONCLUSIVE
    #: True when no candidate had more than one contributing run, so no run-to-run spread
    #: was measured and the tie band is unmeasured rather than narrow (FR-020).
    single_run: bool = False


class HarnessRankingReport(BaseModel):
    schema_version: int = RANK_SCHEMA_VERSION
    generated_at: datetime
    run_ids: list[str] = Field(default_factory=list)
    view: ScoringView = ScoringView.HARNESS_COMPARISON
    weights: Weights = Weights()
    rankings: list[HarnessRanking] = Field(default_factory=list)

    def to_validated_json(self) -> str:
        text = self.model_dump_json(indent=2)
        HarnessRankingReport.model_validate_json(text)  # round-trip guard
        return text

    def write(self, out_dir: str | Path) -> Path:
        """Write ``harness_rank.json`` at the sweep root (FR-026).

        Advisory only: this is the ONLY thing the ranking writes. It never edits live
        deployment configuration (FR-023).
        """
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "harness_rank.json"
        path.write_text(self.to_validated_json())
        return path

    @classmethod
    def load(cls, path: str | Path) -> HarnessRankingReport:
        return cls.model_validate_json(Path(path).read_text())


def score_row(
    row: RoleHarnessRow,
    weights: Weights | None = None,
    *,
    cost_per_million_tokens: float = DEFAULT_COST_PER_MILLION_TOKENS,
) -> HarnessScore:
    """Score one row under the harness-comparison objective (FR-010, FR-024).

    Mirrors ``score.compute_scalar``'s shape, substituting a harness credit rate for card
    correctness::

        w_correctness * credit_rate
          - w_cost * (estimated_cost_usd / cost_budget_usd)
          - w_time * (seconds / time_budget_seconds)

    The cost term divides **USD by USD**. Tokens are converted with the existing
    estimator first; dividing a raw token count by a currency budget is a unit error.
    When no token count is available the cost term is omitted and the omission recorded,
    never treated as zero cost.
    """
    w = weights or Weights()
    score = HarnessScore(
        role=row.role,
        backend=row.backend,
        weights=w,
        credit_rate=row.credit_rate,
        seconds=row.seconds_total,
        tokens=row.tokens_total,
        evidence=row,
    )

    if row.credit_rate is None:
        # No conclusive evidence: withhold the scalar rather than emit a zero that would
        # read as a measured (and terrible) result.
        score.cost_component_missing = row.tokens_total is None or row.tokens_partial
        return score

    scalar = w.w_correctness * row.credit_rate

    # A partial token sum is a LOWER BOUND, so costing from it would undercharge and
    # flatter a harness whose token reporting happens to be patchy. Treat it exactly like
    # no tokens at all: omit the term and record that cost is unknown.
    cost = (
        None
        if row.tokens_partial
        else estimate_cost_usd(row.tokens_total, cost_per_million_tokens)
    )
    if cost is None:
        score.cost_component_missing = True
    else:
        scalar -= w.w_cost * (cost / w.cost_budget_usd)

    scalar -= w.w_time * (row.seconds_total / w.time_budget_seconds)
    score.scalar = round(scalar, 6)
    return score


def _spread(row: RoleHarnessRow) -> tuple[float, float, int]:
    """``(mean, stdev, n)`` over the row's PER-RUN scalars.

    Deliberately the per-run values, not the pooled row: pooling collapses the sample to
    a single number, and a tie rule fed a single number silently degenerates into
    exact-equality comparison.
    """
    stats = component_stats(row.per_run_scalars)
    if stats is None:
        return (0.0, 0.0, 0)
    return (stats.mean, stats.stdev, stats.n)


def _ties_within_band(scores: list[HarnessScore]) -> list[list[str]]:
    """Every PAIR of candidates that cannot be distinguished (FR-020).

    ``|mean_a - mean_b| <= stdev_a + stdev_b``, over the per-run scalars. Reuses the
    existing repeat statistics rather than introducing a second significance test.

    Returns **pairs**, deliberately not groups. The tie relation is not transitive:
    measured directly, ``x~y`` and ``y~z`` can both hold while ``x~z`` does not (a narrow
    pair either side of one wide candidate). Merging a chain into a single group — the
    obvious "fix" for the overlapping groups an earlier version emitted — would assert an
    indistinguishability the data denies, which is worse than being verbose. A pair claims
    exactly what was measured about those two candidates and nothing about any third.
    """
    pairs: list[list[str]] = []
    for i, a in enumerate(scores):
        mean_a, sd_a, _ = _spread(a.evidence)
        for b in scores[i + 1 :]:
            mean_b, sd_b, _ = _spread(b.evidence)
            if abs(mean_a - mean_b) <= sd_a + sd_b:
                pairs.append([a.backend, b.backend])
    return pairs


def rank_role(
    role: str,
    rows: list[RoleHarnessRow],
    weights: Weights | None = None,
    *,
    min_conclusive: int = DEFAULT_MIN_CONCLUSIVE,
    cost_per_million_tokens: float = DEFAULT_COST_PER_MILLION_TOKENS,
) -> HarnessRanking:
    """Rank the harnesses observed for one role (FR-019 through FR-022, FR-027)."""
    w = weights or Weights()
    ranking = HarnessRanking(role=role, min_conclusive=min_conclusive)

    rankable: list[HarnessScore] = []
    for row in rows:
        score = score_row(row, w, cost_per_million_tokens=cost_per_million_tokens)
        if score.scalar is None or row.conclusive < min_conclusive:
            # Below the evidence floor, or no conclusive dispatches at all.
            ranking.insufficient_evidence.append(row.backend)
            continue
        rankable.append(score)

    ranking.single_run = all(r.evidence.runs_contributing <= 1 for r in rankable) if rankable else False

    if len(rankable) < 2:
        # One candidate cannot be "best": there is nothing to be better than.
        ranking.ordered = sorted(rankable, key=lambda s: s.scalar or 0.0, reverse=True)
        ranking.verdict = "insufficient_evidence"
        return ranking

    if all((s.credit_rate or 0.0) == 0.0 for s in rankable):
        # Every candidate failed outright. Ranking the least-bad would imply one works.
        ranking.ordered = rankable
        ranking.verdict = "no_viable_harness"
        return ranking

    ranking.ordered = sorted(rankable, key=lambda s: s.scalar or 0.0, reverse=True)
    ranking.ties = _ties_within_band(ranking.ordered)

    top = ranking.ordered[0].backend
    if any(top in group for group in ranking.ties):
        ranking.verdict = "tie"
    else:
        ranking.verdict = "ranked"
    return ranking


def rank(
    rollup: HarnessRollup,
    weights: Weights | None = None,
    *,
    min_conclusive: int = DEFAULT_MIN_CONCLUSIVE,
    cost_per_million_tokens: float = DEFAULT_COST_PER_MILLION_TOKENS,
) -> HarnessRankingReport:
    """Rank every role present in *rollup* (FR-019)."""
    w = weights or Weights()
    by_role: dict[str, list[RoleHarnessRow]] = {}
    for row in rollup.rows:
        by_role.setdefault(row.role, []).append(row)

    return HarnessRankingReport(
        generated_at=datetime.now(UTC),
        run_ids=list(rollup.run_ids),
        weights=w,
        rankings=[
            rank_role(
                role,
                rows,
                w,
                min_conclusive=min_conclusive,
                cost_per_million_tokens=cost_per_million_tokens,
            )
            for role, rows in sorted(by_role.items())
        ],
    )


def format_report(report: HarnessRankingReport) -> str:
    """Operator-facing rendering. Every line states its evidence (FR-022)."""
    lines: list[str] = []
    for r in report.rankings:
        lines.append(f"{r.role}: {r.verdict.upper()}")
        if r.single_run and r.verdict in {"ranked", "tie"}:
            lines.append("  (single run per candidate — no run-to-run spread measured)")
        for i, s in enumerate(r.ordered, 1):
            ev = s.evidence
            lines.append(
                f"  {i}. {s.backend:<14} scalar={s.scalar:.4f}  "
                f"credit={ev.credit}/{ev.conclusive} conclusive  "
                f"defects={ev.harness_defect}  runs={ev.runs_contributing}"
            )
        for pair in r.ties:
            lines.append(
                f"  indistinguishable (within noise band): {pair[0]} vs {pair[1]}"
            )
        if r.insufficient_evidence:
            lines.append(
                f"  insufficient evidence (<{r.min_conclusive} conclusive): "
                f"{', '.join(sorted(r.insufficient_evidence))}"
            )
        lines.append("")
    if not report.rankings:
        lines.append("no roles present in the rollup — nothing to rank")
    lines.append("advisory only: no live configuration was modified")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m coordinare.bench.harness_rank <run.json> [<run.json> ...]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        print("usage: python -m coordinare.bench.harness_rank <run.json> [...]")
        return 2

    rollup = roll_up([RunArtifact.load(p) for p in args])
    report = rank(rollup)
    print(format_report(report))
    out = report.write(Path(args[0]).parent)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
