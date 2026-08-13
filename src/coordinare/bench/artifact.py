"""Spec 134 — the run artifact (the single structured output of one benchmark run).

Pydantic models serialized to ``runs/<ts>-<confighash>/run.json`` (+ a ``raw/``
subdir referenced by the ``*_ref`` fields). The artifact is deliberately raw
material for Phase 2 scoring (spec 135) — it records what happened, it does not
judge correctness.

Contract: ``specs/134-board-sim-benchmark/contracts/run-artifact.md``.
"""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TC003 — needed at runtime for pydantic model building
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

SCHEMA_VERSION = 1

FinalState = Literal["merged", "blocked", "abandoned", "error"]
DispatchStatus = Literal["succeeded", "failed", "cancelled", "error"]
GateVerdict = Literal["pass", "hold", "bounce", "escalate"]
CIConclusion = Literal["success", "failure"]


class Cost(BaseModel):
    """Per-card / per-dispatch cost. A token-rate estimate, never authoritative
    proxy USD (spec-134 FR-012) — both numbers may be None if no tokens were
    reported."""

    tokens_processed: int | None = None
    cost_usd: float | None = None
    cost_estimated: bool = True


class Timing(BaseModel):
    first_dispatch_at: datetime | None = None
    terminal_at: datetime | None = None
    seconds: float | None = None


class Merge(BaseModel):
    merged: bool = False
    merge_commit: str | None = None
    approved_by: str | None = None


class PersonaDispatch(BaseModel):
    stage: str
    role: str = ""
    model: str = ""
    backend: str = ""
    job_id: str | None = None
    session_id: str | None = None
    container_id: str | None = None
    status: DispatchStatus = "succeeded"
    terminal_marker: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    seconds: float | None = None
    tokens_processed: int | None = None
    raw_summary_ref: str | None = None


class GateDecision(BaseModel):
    stage: str
    verdict: GateVerdict
    head_sha: str = ""
    required_checks: list[str] = []
    failed_checks: list[str] = []
    decided_at: datetime | None = None


class CIResult(BaseModel):
    head_sha: str
    required_checks: list[str] = []
    conclusion: CIConclusion
    pytest_exit: int | None = None
    summary_ref: str | None = None


class CardOutcome(BaseModel):
    card_id: str
    issue_ref: str = ""
    title: str = ""
    fixture_id: str | None = None
    final_state: FinalState
    reached_stages: list[str] = []
    dispatches: list[PersonaDispatch] = []
    gate_decisions: list[GateDecision] = []
    ci_results: list[CIResult] = []
    merge: Merge = Merge()
    timing: Timing = Timing()
    cost: Cost = Cost()


class ConfigFingerprint(BaseModel):
    hash: str
    source_path: str


class RunTotals(BaseModel):
    tokens_processed: int | None = None
    cost_usd: float | None = None
    cost_estimated: bool = True
    cards_total: int = 0
    cards_merged: int = 0
    cards_terminal_nonmerge: int = 0


class RunArtifact(BaseModel):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    started_at: datetime
    finished_at: datetime
    wall_clock_seconds: float
    config_fingerprint: ConfigFingerprint
    approver_policy: str = "gates_green"
    fixture_manifest: str = ""
    cards: list[CardOutcome] = []
    totals: RunTotals = RunTotals()

    def to_validated_json(self) -> str:
        """Serialize to JSON and re-parse it, proving the artifact round-trips
        against its own schema (spec-134 FR-011). Raises if it does not.
        Returns the JSON text to write to ``run.json``."""
        text = self.model_dump_json(indent=2)
        RunArtifact.model_validate_json(text)  # round-trip guard
        return text

    def write(self, run_dir: str | Path) -> Path:
        """Write ``run.json`` into ``run_dir`` after validating the round-trip."""
        d = Path(run_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / "run.json"
        path.write_text(self.to_validated_json())
        return path

    @classmethod
    def load(cls, path: str | Path) -> RunArtifact:
        return cls.model_validate_json(Path(path).read_text())

    def compute_totals(self) -> RunTotals:
        """Derive run-level totals from the per-card outcomes."""
        tokens = [
            c.cost.tokens_processed for c in self.cards if c.cost.tokens_processed is not None
        ]
        costs = [c.cost.cost_usd for c in self.cards if c.cost.cost_usd is not None]
        return RunTotals(
            tokens_processed=sum(tokens) if tokens else None,
            cost_usd=round(sum(costs), 6) if costs else None,
            cost_estimated=True,
            cards_total=len(self.cards),
            cards_merged=sum(1 for c in self.cards if c.final_state == "merged"),
            cards_terminal_nonmerge=sum(1 for c in self.cards if c.final_state != "merged"),
        )


def estimate_cost_usd(tokens_processed: int | None, cost_per_million_tokens: float) -> float | None:
    """Token-rate cost estimate mirroring coordinare's own card_cost_estimate
    (monitor_performer.py). Returns None when no token count is available."""
    if tokens_processed is None:
        return None
    return round(tokens_processed / 1_000_000 * cost_per_million_tokens, 6)


def _reexport_json() -> str:  # pragma: no cover - convenience for schema dumps
    return json.dumps(RunArtifact.model_json_schema(), indent=2)
