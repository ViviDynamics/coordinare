"""Spec 135 — the score object (the documented result of grading one benchmark run).

Pydantic models serialized to ``score.json`` next to the ``run.json`` it scores.
The score is the objective surface specs 136/137 consume: per-card verdicts +
evidence, a component vector, and a weighted scalar with the weights embedded so
scalars are only compared across matching weight fingerprints.

Contract: ``specs/135-board-bench-scoring/contracts/score-object.md``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic model building
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

#: Bumped 1 → 2 by spec 161, which adds the `view` discriminator below.
#:
#: The addition is behaviour-preserving but NOT byte-preserving: a new field changes the
#: serialized document, so the version moves and `view` defaults to CONFIG_COMPARISON.
#: What spec-161 FR-011 guarantees is that the computed `scalar` and component VALUES are
#: numerically unchanged for a fixed-harness sweep — a value guarantee, not byte identity,
#: which would be impossible alongside a new field.
SCORE_SCHEMA_VERSION = 2

# Artifact schema versions this scorer knows how to grade (spec-135 FR-001).
KNOWN_ARTIFACT_VERSIONS = frozenset({1})

VerdictCategory = Literal["PASS", "FAIL_MODEL", "FAIL_HARNESS", "ERROR"]
ExpectedFinalState = Literal["merged", "blocked"]


class ScoringView(StrEnum):
    """Which objective produced a score (spec-161 FR-012).

    The two views treat harness failure in OPPOSITE ways, so a scalar from one is
    meaningless against a scalar from the other:

    * ``CONFIG_COMPARISON`` — harness failure is quarantined out of ``correctness_rate``
      and surfaced separately, so optimization is never steered by infrastructure noise
      (spec-135 FR-005). This is the pre-existing behaviour and the default.
    * ``HARNESS_COMPARISON`` — harness failure counts AGAINST the harness, because when
      the harness is the thing under test its defects are the signal, not noise
      (spec-161 FR-010).

    Carried alongside the embedded ``Weights``, extending the existing rule that scalars
    compare only within matching weights to also require a matching view.
    """

    CONFIG_COMPARISON = "config_comparison"
    HARNESS_COMPARISON = "harness_comparison"


class Weights(BaseModel):
    """The scalar-objective weights + normalization budgets (spec Clarifications).

    Embedded in every score object — two scalars are comparable only when their
    weights match."""

    w_correctness: float = Field(default=1.0, ge=0)
    w_cost: float = Field(default=0.1, ge=0)
    w_time: float = Field(default=0.1, ge=0)
    # Budgets are divisors — zero would make the objective undefined, so they
    # must be strictly positive (fail loudly at construction, never mid-scoring).
    cost_budget_usd: float = Field(default=1.0, gt=0)
    time_budget_seconds: float = Field(default=600.0, gt=0)


class CardVerdict(BaseModel):
    """One card's grade: 077's agree-to-PASS lifted to whole-lifecycle."""

    card_id: str
    fixture_id: str = ""
    expected_final_state: ExpectedFinalState = "merged"
    observed_final_state: str = ""
    category: VerdictCategory
    correct: bool = False
    deterministic_ok: bool = False
    deterministic_detail: str = ""
    judge_correct: bool | None = None
    judge_quality: int | None = None
    judge_reason: str = ""


class ComponentVector(BaseModel):
    """The per-run score components (spec-135 FR-002b, research R4).

    ``correctness_rate`` is over gradeable (non-harness-failed) cards;
    infrastructure noise is surfaced separately via ``harness_failure_rate`` so
    downstream optimization is never steered by it (FR-005)."""

    correctness_rate: float | None = None
    harness_failure_rate: float = 0.0
    tokens_processed: int | None = None
    #: 206: the run's priced cost. Sums only the priced cards when ``cost_partial`` is set.
    cost_usd: float | None = None
    cost_estimated: bool = True
    #: 206: True when any card had no price, so ``cost_usd`` is a partial sum.
    cost_partial: bool = False
    #: 206: why the cost is unknown or partial. None on scores written before spec 206.
    cost_unknown_reason: str | None = None
    wall_clock_seconds: float = 0.0


class ScoreObject(BaseModel):
    schema_version: int = SCORE_SCHEMA_VERSION
    run_id: str
    run_ref: str = "run.json"
    artifact_schema_version: int
    scored_at: datetime
    judged: bool = False
    judge_model: str | None = None
    deterministic_only: bool = True
    #: Defaults to CONFIG_COMPARISON so every score written before spec 161 — and every
    #: fixed-harness config sweep after it — keeps exactly the meaning it always had.
    view: ScoringView = ScoringView.CONFIG_COMPARISON
    weights: Weights = Weights()
    cards: list[CardVerdict] = []
    components: ComponentVector = ComponentVector()
    scalar: float | None = None
    cost_component_missing: bool = False

    def to_validated_json(self) -> str:
        """Serialize and re-parse, proving the score round-trips against its own
        schema before being declared written (spec-135 FR-006). Raises if not."""
        text = self.model_dump_json(indent=2)
        ScoreObject.model_validate_json(text)  # round-trip guard
        return text

    def write(self, run_dir: str | Path) -> Path:
        """Write ``score.json`` into ``run_dir`` (next to the scored run.json)."""
        d = Path(run_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "score.json"
        path.write_text(self.to_validated_json())
        return path

    @classmethod
    def load(cls, path: str | Path) -> ScoreObject:
        return cls.model_validate_json(Path(path).read_text())


def cost_is_known(components: ComponentVector) -> bool:
    """True when the run's cost is a complete priced figure (spec 206)."""
    return components.cost_usd is not None and not components.cost_partial


def numeric_component(components: ComponentVector, name: str) -> float | None:
    """A component as a number, or None when it is unknown.

    An unpriced or partial cost is unknown, never a number: averaging or comparing it would
    treat the unpriced part as free (spec 206).
    """
    if name == "cost_usd" and not cost_is_known(components):
        return None
    value = getattr(components, name)
    return None if value is None else float(value)


def compute_scalar(components: ComponentVector, weights: Weights) -> tuple[float | None, bool]:
    """The weighted objective (spec Clarifications):

    ``w_c*correctness_rate - w_cost*(cost/cost_budget) - w_time*(time/time_budget)``

    Returns ``(scalar, cost_component_missing)``. A run with zero gradeable cards
    (``correctness_rate is None``) has no scalar. Spec 206: when the cost is unknown
    (no price, no token count) or partial, and the cost term carries weight, the scalar is
    also None. Dropping the term would score the unpriced run as free and rank it above
    priced runs, so the run is unrankable instead. With ``w_cost == 0`` cost cannot change
    the objective, so the scalar is computed and the cost flagged missing."""
    cost_missing = not cost_is_known(components)
    if components.correctness_rate is None:
        return None, cost_missing
    if cost_missing and weights.w_cost > 0:
        return None, True
    scalar = weights.w_correctness * components.correctness_rate
    if not cost_missing:
        scalar -= weights.w_cost * (float(components.cost_usd or 0.0) / weights.cost_budget_usd)
    scalar -= weights.w_time * (components.wall_clock_seconds / weights.time_budget_seconds)
    return round(scalar, 6), cost_missing
