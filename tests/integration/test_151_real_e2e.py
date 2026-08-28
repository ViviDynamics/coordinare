"""Spec 151 (US1, T018) — the single REAL-model end-to-end run. OPT-IN: skipped
unless RUN_REAL_BENCH is set (needs Docker + a cheap model endpoint), so the free
deterministic lane (SC-006) never pays for it. The deterministic resilience proofs
live in tests/unit/test_151_runner_failure.py (T031).

Run it with:
    RUN_REAL_BENCH=1 pytest tests/integration/test_151_real_e2e.py \
        --confhash ...   # (config below points at a cheap model)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.runner import run_board

pytestmark = pytest.mark.real

_CONFIG = Path(__file__).resolve().parents[2] / "specs" / "151-real-performers" / "examples" / "bench-real.yaml"


async def test_real_run_reaches_terminal_with_multiple_dispatches(
    tmp_path: Path, require_docker: None
) -> None:
    run_dir = tmp_path / "run"
    await run_board([tiny_fixture()], run_dir, stub=False, config_path=str(_CONFIG))

    # SC-001 (incl. scenario 3, "a satisfiable fixture can reach merged"): the card
    # reaches a terminal state and the artifact re-validates. `final_state` is not
    # asserted — FinalState is a 4-value Literal that pydantic already enforces, so
    # `in {all four}` is vacuously true; the real signal is that load() validates
    # and that the dispatch assertions below hold.
    reloaded = RunArtifact.load(run_dir / "run.json")
    assert reloaded.totals.cards_total == 1
    card = reloaded.cards[0]

    # SC-002: >1 REAL dispatch, each carrying model/backend (distinguishable from
    # the stub's single synthetic entry).
    assert len(card.dispatches) > 1, "expected multiple real per-persona dispatches"
    assert all(d.backend for d in card.dispatches), "each real dispatch records a backend"
    assert any(d.model for d in card.dispatches), "at least one dispatch records a model"
