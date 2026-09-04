"""Spec 161 — aggregate dispatch evidence by ``(role, backend)``.

Answers "which harness is failing which role" from runs that were **already recorded**:
every field it needs is present on ``PersonaDispatch``, so no new instrumentation and no
live inference host is required.

Two rules carry most of the weight here:

* **Rates are computed over conclusive dispatches only** (credit + harness defect).
  Environment failures and inconclusive dispatches are excluded from both numerator and
  denominator, so infrastructure noise never becomes signal.
* **Absent is not zero.** With no conclusive dispatches the rates are ``None``, never
  ``0.0``. A zero defect rate reads as a flawless harness; ``None`` says nothing is known.
  The same applies to tokens.

Contract: ``specs/161-board-bench-harness/contracts/harness-rollup.md``.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.harness_outcome import OutcomeClass, classify, is_unknown_marker
from coordinare.bench.score import KNOWN_ARTIFACT_VERSIONS

if TYPE_CHECKING:
    from coordinare.bench.artifact import PersonaDispatch

ROLLUP_SCHEMA_VERSION = 1


class RollupError(ValueError):
    """Raised when a rollup cannot be produced honestly (e.g. an unknown schema)."""


class RoleHarnessRow(BaseModel):
    """One ``(role, backend)`` pair's aggregated evidence (spec-161 FR-008)."""

    role: str
    backend: str
    dispatches: int = 0
    credit: int = 0
    harness_defect: int = 0
    environment: int = 0
    inconclusive: int = 0
    seconds_total: float = 0.0
    tokens_total: int | None = None
    #: True when SOME dispatches reported tokens and others did not, so `tokens_total` is
    #: a lower bound rather than the total. Kept separate from `None` (nothing reported at
    #: all), because a partial sum looks like a complete one and would quietly understate
    #: cost — the same absent-vs-zero trap in a different place.
    tokens_partial: bool = False
    runs_contributing: int = 0
    #: One credit rate per contributing run that had conclusive evidence (FR-025).
    #: Retained because pooled counts alone collapse the sample to a single value, which
    #: would leave the FR-020 tie rule with no standard deviation and silently degrade it
    #: to exact-equality comparison.
    per_run_scalars: list[float] = Field(default_factory=list)
    unknown_markers: list[str] = Field(default_factory=list)

    @property
    def conclusive(self) -> int:
        return self.credit + self.harness_defect

    @property
    def defect_rate(self) -> float | None:
        """Harness defects over conclusive dispatches, or ``None`` when nothing is known.

        Deliberately ``None`` rather than ``0.0``: see the module docstring.
        """
        if self.conclusive == 0:
            return None
        return self.harness_defect / self.conclusive

    @property
    def credit_rate(self) -> float | None:
        if self.conclusive == 0:
            return None
        return self.credit / self.conclusive


class HarnessRollup(BaseModel):
    """The rollup document, written beside the run it describes (FR-026)."""

    schema_version: int = ROLLUP_SCHEMA_VERSION
    run_ids: list[str] = Field(default_factory=list)
    rows: list[RoleHarnessRow] = Field(default_factory=list)
    generated_at: datetime
    artifact_schema_versions: list[int] = Field(default_factory=list)

    def to_validated_json(self) -> str:
        """Serialize and re-parse, proving the rollup round-trips against its own schema
        before being declared written. Mirrors ``RunArtifact``/``ScoreObject``."""
        text = self.model_dump_json(indent=2)
        HarnessRollup.model_validate_json(text)  # round-trip guard
        return text

    def write(self, run_dir: str | Path) -> Path:
        """Write ``harness_rollup.json`` into *run_dir*, beside its ``run.json``."""
        d = Path(run_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "harness_rollup.json"
        path.write_text(self.to_validated_json())
        return path

    @classmethod
    def load(cls, path: str | Path) -> HarnessRollup:
        return cls.model_validate_json(Path(path).read_text())


def _unique_dispatches(artifact: RunArtifact) -> list[PersonaDispatch]:
    """Dispatches with multi-stage registrations collapsed, keyed by session.

    A performer is usually registered under several stages, so the same session can appear
    more than once; those are one dispatch and are counted once per ``(role, backend)``.

    Crucially, dedup applies **only when a session id is present**. Without one there is
    no evidence that two rows describe the same dispatch, and merging them would silently
    discard real attempts (two independent failures would read as one). So an absent
    session id makes a row distinct, which is the conservative direction: it can never
    under-count a harness's failures.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[PersonaDispatch] = []
    for card in artifact.cards:
        for d in card.dispatches:
            if not d.session_id:
                out.append(d)
                continue
            key = (d.role, d.backend, d.session_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(d)
    return out


def roll_up(artifacts: list[RunArtifact]) -> HarnessRollup:
    """Aggregate one or more run artifacts into per-``(role, backend)`` rows.

    Pooling (FR-025): counts sum across artifacts and rates are computed over the pooled
    total, while ``per_run_scalars`` retains each run's own credit rate so run-to-run
    spread stays available to the ranking's tie rule.
    """
    if not artifacts:
        msg = "no artifacts supplied — nothing to roll up"
        raise RollupError(msg)

    for a in artifacts:
        if a.schema_version not in KNOWN_ARTIFACT_VERSIONS:
            msg = (
                f"unknown artifact schema version {a.schema_version} for run {a.run_id!r}; "
                f"known: {sorted(KNOWN_ARTIFACT_VERSIONS)}. Refusing to grade "
                "rather than partially interpret it."
            )
            raise RollupError(msg)

    rows: dict[tuple[str, str], RoleHarnessRow] = {}
    # Per-run credit rates, kept separate so pooling cannot destroy the spread.
    per_run: dict[tuple[str, str], list[float]] = defaultdict(list)
    unknown: dict[tuple[str, str], set[str]] = defaultdict(set)
    token_seen: dict[tuple[str, str], bool] = defaultdict(bool)
    token_missing: dict[tuple[str, str], bool] = defaultdict(bool)

    for artifact in artifacts:
        run_counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
        # Pairs this run touched AT ALL. Tracked separately from run_counts because a run
        # can contribute dispatches while producing no conclusive ones (all environment or
        # inconclusive); counting only conclusive runs understated the evidence base.
        pairs_this_run: set[tuple[str, str]] = set()
        for d in _unique_dispatches(artifact):
            key = (d.role, d.backend)
            pairs_this_run.add(key)
            row = rows.get(key)
            if row is None:
                row = RoleHarnessRow(role=d.role, backend=d.backend)
                rows[key] = row

            cls = classify(d)
            row.dispatches += 1
            if cls is OutcomeClass.CREDIT:
                row.credit += 1
                run_counts[key][0] += 1
            elif cls is OutcomeClass.HARNESS_DEFECT:
                row.harness_defect += 1
                run_counts[key][1] += 1
            elif cls is OutcomeClass.ENVIRONMENT:
                row.environment += 1
            else:
                row.inconclusive += 1

            if is_unknown_marker(d.terminal_marker):
                unknown[key].add(str(d.terminal_marker))

            row.seconds_total += d.seconds or 0.0
            if d.tokens_processed is None:
                token_missing[key] = True
            else:
                token_seen[key] = True
                row.tokens_total = (row.tokens_total or 0) + d.tokens_processed

        # One scalar per run per pair, but only where that run had conclusive evidence:
        # a run with none has no rate, and inventing 0.0 would fabricate spread.
        for key, (credit, defect) in run_counts.items():
            conclusive = credit + defect
            if conclusive:
                per_run[key].append(credit / conclusive)
        # Contribution is about participation, not about producing a rate (FR-025).
        for key in pairs_this_run:
            rows[key].runs_contributing += 1

    for key, row in rows.items():
        row.per_run_scalars = per_run[key]
        row.unknown_markers = sorted(unknown[key])
        if not token_seen[key]:
            row.tokens_total = None  # nothing reported: absent, never 0
        else:
            # Some reported and some did not: the sum is a lower bound, so say so.
            row.tokens_partial = token_missing[key]

    return HarnessRollup(
        run_ids=[a.run_id for a in artifacts],
        rows=[rows[k] for k in sorted(rows)],
        generated_at=datetime.now(UTC),
        artifact_schema_versions=sorted({a.schema_version for a in artifacts}),
    )


def format_table(rollup: HarnessRollup) -> str:
    """The operator table from the quickstart.

    A blank ``defect_rate`` means no conclusive dispatches, which is "nothing known" and
    deliberately distinct from ``0.000`` meaning "measured flawless".
    """
    header = (
        f"{'role':<16}{'backend':<14}{'disp':>5}{'credit':>8}{'defect':>8}"
        f"{'env':>5}{'incon':>7}{'defect_rate':>13}"
    )
    lines = [header, "-" * len(header)]
    for r in rollup.rows:
        rate = "" if r.defect_rate is None else f"{r.defect_rate:.3f}"
        lines.append(
            f"{r.role:<16}{r.backend:<14}{r.dispatches:>5}{r.credit:>8}"
            f"{r.harness_defect:>8}{r.environment:>5}{r.inconclusive:>7}{rate:>13}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """``python -m coordinare.bench.harness_rollup <run.json> [<run.json> ...]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        print("usage: python -m coordinare.bench.harness_rollup <run.json> [...]")
        return 2

    artifacts = [RunArtifact.load(p) for p in args]
    rollup = roll_up(artifacts)
    print(format_table(rollup))
    out = rollup.write(Path(args[0]).parent)
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
