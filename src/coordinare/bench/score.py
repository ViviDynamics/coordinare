"""Spec 135 — the score object (the documented result of grading one benchmark run).

Pydantic models serialized to ``score.json`` next to the ``run.json`` it scores.
The score is the objective surface specs 136/137 consume: per-card verdicts +
evidence, a component vector, and a weighted scalar with the weights embedded so
scalars are only compared across matching weight fingerprints.

Contract: ``specs/135-board-bench-scoring/contracts/score-object.md``.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic model building
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

SCORE_SCHEMA_VERSION = 1

# Artifact schema versions this scorer knows how to grade (spec-135 FR-001).
KNOWN_ARTIFACT_VERSIONS = frozenset({1})

VerdictCategory = Literal["PASS", "FAIL_MODEL", "FAIL_HARNESS", "ERROR"]
ExpectedFinalState = Literal["merged", "blocked"]


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
    cost_usd: float | None = None
    cost_estimated: bool = True
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


def compute_scalar(components: ComponentVector, weights: Weights) -> tuple[float | None, bool]:
    """The weighted objective (spec Clarifications):

    ``w_c*correctness_rate - w_cost*(cost/cost_budget) - w_time*(time/time_budget)``

    Returns ``(scalar, cost_component_missing)``. A run with zero gradeable cards
    (``correctness_rate is None``) has no scalar — the score object is still
    written, explicitly unrankable. Unknown cost omits the cost term (never
    treated as free)."""
    if components.correctness_rate is None:
        return None, components.cost_usd is None
    scalar = weights.w_correctness * components.correctness_rate
    cost_missing = components.cost_usd is None
    if not cost_missing:
        scalar -= weights.w_cost * (float(components.cost_usd or 0.0) / weights.cost_budget_usd)
    scalar -= weights.w_time * (components.wall_clock_seconds / weights.time_budget_seconds)
    return round(scalar, 6), cost_missing
