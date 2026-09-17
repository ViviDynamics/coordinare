"""Spec 137 — end-to-end: a stub-substrate search over a real space through the
real evaluator, the CLI's enforced real-mode precondition, and recommendation
reproducibility.

Deterministic and free — stubbed performers, judging disabled.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import yaml
from scripts.board_optimize import main as optimize_main

from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.optimizer import OptimizerArtifact, real_evaluator, run_optimizer
from coordinare.bench.space import config_fingerprint, load_space, materialize
from tests.unit.test_136_space import BASELINE

# Two dimensions -> 4 grid combos: small enough for a fast full search.
SMALL_SPACE = {
    "name": "small",
    "baseline_config": "baseline.yaml",
    "dimensions": [
        {
            "name": "ci-gate",
            "path": "symphonies.bench.persona_scope.ci_gate.enabled",
            "choices": [False, True],
        },
        {"name": "concurrency", "path": "global_config.max_concurrent_cards", "choices": [1, 2]},
    ],
    "candidates": [
        {"name": "parallel", "overrides": {"global_config.max_concurrent_cards": 2}},
    ],
}


def _space(tmp_path: Path):
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump(BASELINE))
    path = tmp_path / "space.yaml"
    path.write_text(yaml.safe_dump(SMALL_SPACE))
    return load_space(path)


class TestOptimizerEndToEnd:
    async def test_stub_search_reconciles_and_reports(self, tmp_path: Path, monkeypatch) -> None:
        fetch = AsyncMock(side_effect=AssertionError("stub benchmark attempted live git access"))
        monkeypatch.setattr("coordinare.daemon.fetch_main_sha", fetch)
        monkeypatch.setattr("coordinare.services.rebase.fetch_main_sha", fetch)
        loaded = _space(tmp_path)
        session = tmp_path / "session"
        evaluate = real_evaluator(loaded, [tiny_fixture()], session)
        artifact = await run_optimizer(
            loaded, evaluate, seed=1, max_evaluations=6, session_dir=session,
            evaluator_kind="stub",
        )

        fetch.assert_not_awaited()
        assert (session / "optimizer.json").exists()
        assert (session / "optimizer-report.md").exists()
        assert OptimizerArtifact.load(session / "optimizer.json") == artifact

        # accounting reconciles (SC-003)
        assert len(artifact.trace) == (
            artifact.budget.charged_evaluations + artifact.budget.cache_hits
        )
        assert artifact.budget.charged_evaluations <= 6
        assert artifact.distinct_evaluated == len(
            {r.fingerprint for r in artifact.trace if not r.cache_hit},
        )
        # baseline + candidate in the head-to-head; stub caveat recorded
        labels = [row.label for row in artifact.recommendation.comparison]
        assert "baseline" in labels and "candidate:parallel" in labels
        assert any("stub" in n for n in artifact.notes)
        # every real evaluation left its per-repeat scores on disk
        for record in artifact.trace:
            if not record.cache_hit and not record.failed:
                for ref in record.score_refs:
                    assert (session / ref).exists()

    async def test_recommendation_overrides_rematerialize_to_fingerprint(self, tmp_path: Path) -> None:
        loaded = _space(tmp_path)
        session = tmp_path / "session"
        evaluate = real_evaluator(loaded, [tiny_fixture()], session)
        artifact = await run_optimizer(
            loaded, evaluate, seed=1, max_evaluations=6, session_dir=session,
            evaluator_kind="stub",
        )
        rec = artifact.recommendation
        assert rec is not None
        cfg = materialize(loaded.baseline_dump, rec.overrides)
        assert config_fingerprint(cfg) == rec.fingerprint


class TestRealModePrecondition:
    def test_real_search_without_noise_report_is_refused(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "baseline.yaml").write_text(yaml.safe_dump(BASELINE))
        space_path = tmp_path / "space.yaml"
        space_path.write_text(yaml.safe_dump(SMALL_SPACE))
        try:
            optimize_main(["--space", str(space_path), "--real", "--run-dir", str(tmp_path / "runs")])
        except SystemExit as exc:
            assert exc.code == 2
        else:  # pragma: no cover - the refusal must exit
            raise AssertionError("expected SystemExit(2)")
        err = capsys.readouterr().err
        assert "noise report" in err and "board_score.py noise" in err
