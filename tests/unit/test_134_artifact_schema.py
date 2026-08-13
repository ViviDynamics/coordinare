"""Spec 134 — run artifact schema validates and round-trips (US1 / FR-011)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from coordinare.bench.artifact import (
    SCHEMA_VERSION,
    CardOutcome,
    ConfigFingerprint,
    Cost,
    RunArtifact,
    estimate_cost_usd,
)


def _artifact() -> RunArtifact:
    now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
    return RunArtifact(
        run_id="20260723-abc123",
        started_at=now,
        finished_at=now,
        wall_clock_seconds=12.5,
        config_fingerprint=ConfigFingerprint(hash="abc123", source_path="/cfg.yaml"),
        fixture_manifest="manifest.yaml",
        cards=[
            CardOutcome(card_id="PVTI_1", final_state="merged", cost=Cost(tokens_processed=1_000_000)),
            CardOutcome(card_id="PVTI_2", final_state="blocked"),
        ],
    )


def test_artifact_round_trips() -> None:
    art = _artifact()
    text = art.to_validated_json()
    reloaded = RunArtifact.model_validate_json(text)
    assert reloaded == art
    assert reloaded.schema_version == SCHEMA_VERSION


def test_final_state_literal_is_enforced() -> None:
    with pytest.raises(ValidationError):
        CardOutcome(card_id="x", final_state="totally-merged")  # type: ignore[arg-type]


def test_cost_is_flagged_as_estimate_by_default() -> None:
    assert Cost().cost_estimated is True
    assert CardOutcome(card_id="x", final_state="error").cost.cost_estimated is True


def test_compute_totals_aggregates_cards() -> None:
    art = _artifact()
    art.cards[0].cost.cost_usd = 3.0
    totals = art.compute_totals()
    assert totals.cards_total == 2
    assert totals.cards_merged == 1
    assert totals.cards_terminal_nonmerge == 1
    assert totals.tokens_processed == 1_000_000
    assert totals.cost_usd == 3.0
    assert totals.cost_estimated is True


def test_estimate_cost_usd() -> None:
    assert estimate_cost_usd(None, 3.0) is None
    assert estimate_cost_usd(1_000_000, 3.0) == 3.0
    assert estimate_cost_usd(500_000, 3.0) == 1.5


def test_write_and_load(tmp_path: Path) -> None:
    art = _artifact()
    path = art.write(tmp_path / "run")
    assert path.name == "run.json"
    assert RunArtifact.load(path) == art
