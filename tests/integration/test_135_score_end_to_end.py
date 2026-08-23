"""Spec 135 — end-to-end: stub run → score → correct verdict (US1/SC-001), and a
2-repeat noise session → aggregate report (US3/SC-003).

Deterministic and free — no model calls, judging disabled (deterministic-only,
labeled). Builds on the same stubbed 134 substrate as test_134_end_to_end.py.
"""

from __future__ import annotations

from pathlib import Path

from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.grader import score_run
from coordinare.bench.noise import NoiseReport, run_repeats
from coordinare.bench.runner import run_board
from coordinare.bench.score import ScoreObject


async def test_stub_run_scores_pass_and_score_json_round_trips(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    artifact = await run_board([tiny_fixture()], run_dir, stub=True)

    score = score_run(artifact, [tiny_fixture()])
    path = score.write(run_dir)

    assert path == run_dir / "score.json"
    reloaded = ScoreObject.load(path)
    assert reloaded == score
    assert score.deterministic_only is True
    (verdict,) = score.cards
    assert verdict.category == "PASS", verdict.deterministic_detail
    assert score.components.correctness_rate == 1.0
    assert score.components.harness_failure_rate == 0.0
    assert score.scalar is not None
    # Cost is unlabeled in stub runs — omitted from the scalar, never zero-cost.
    assert score.cost_component_missing is True


async def test_two_repeat_noise_session_aggregates(tmp_path: Path) -> None:
    session = tmp_path / "noise"
    report = await run_repeats([tiny_fixture()], 2, session)

    assert report.requested_repeats == 2
    assert report.effective_repeats == 2
    assert report.failures == []
    # Both repeats produced run.json + score.json in their own dirs.
    for i in (1, 2):
        assert (session / str(i) / "run.json").exists()
        assert (session / str(i) / "score.json").exists()
    # The report itself round-trips and renders.
    assert NoiseReport.load(session / "noise-report.json") == report
    assert (session / "noise-report.md").exists()
    assert report.card_agreement == {"tiny-multiply": 1.0}
    assert report.scalar_stats is not None and report.scalar_stats.n == 2
    assert report.component_stats["correctness_rate"].mean == 1.0
