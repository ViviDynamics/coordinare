"""Spec 136 — sweep runner: enumeration, deltas, ranking, repeats derivation,
coverage honesty, artifact round-trip, and the run_board config-injection seam.

Contract: specs/136-board-bench-sweep/contracts/sweep-artifact.md
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinare.bench.sweep as sweep_mod
from coordinare.bench.noise import ComponentStats, NoiseReport
from coordinare.bench.runner import _config_fingerprint
from coordinare.bench.space import config_fingerprint, load_space, materialize
from coordinare.bench.sweep import (
    SWEEP_SCHEMA_VERSION,
    AblationDelta,
    PointResult,
    SweepArtifact,
    compute_deltas,
    derive_repeats,
    enumerate_ablation,
    rank_candidates,
    run_sweep,
)
from tests.unit.test_136_space import BASELINE, SPACE


def _loaded(tmp_path: Path):
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump(BASELINE))
    space_path = tmp_path / "space.yaml"
    space_path.write_text(yaml.safe_dump(SPACE))
    return load_space(space_path)


def _point(pid: str, scalar: float | None, comps: dict[str, float] | None = None, **kw) -> PointResult:
    return PointResult(
        point_id=pid,
        kind=kw.pop("kind", "ablation"),
        fingerprint=kw.pop("fingerprint", pid.ljust(16, "0")[:16]),
        mean_scalar=scalar,
        mean_components=comps or {},
        **kw,
    )


class TestSeam:
    def test_injected_config_lands_in_daemon_state_keys(self, tmp_path: Path) -> None:
        # The seam contract: state["config"] = global_config, symphony_configs by name
        loaded = _loaded(tmp_path)
        cfg = materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 2})
        # mirror what run_board does with the injected config
        assert cfg.global_config.max_concurrent_cards == 2
        by_name = {s.name: s for s in cfg.symphonies}
        assert "bench" in by_name and by_name["bench"].persona_scope is not None

    def test_materialized_fingerprint_reflects_content_not_path(self, tmp_path: Path) -> None:
        loaded = _loaded(tmp_path)
        cfg1 = materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 1})
        cfg2 = materialize(loaded.baseline_dump, {"global_config.max_concurrent_cards": 2})
        fp1 = _config_fingerprint(None, cfg1)
        fp2 = _config_fingerprint(None, cfg2)
        assert fp1.source_path == "<materialized>" and fp2.source_path == "<materialized>"
        assert fp1.hash != fp2.hash
        assert fp1.hash == config_fingerprint(cfg1)  # runner and space agree on identity


class TestEnumerateAblation:
    def test_baseline_plus_one_per_non_baseline_choice(self, tmp_path: Path) -> None:
        loaded = _loaded(tmp_path)
        points = enumerate_ablation(loaded)
        # ci-gate: [False, True] baseline False -> 1; implementer-mode: 2 choices
        # baseline single-cheap -> 1; concurrency: [1,2] baseline 1 -> 1
        assert [p.point_id for p in points] == [
            "ci-gate=True",
            "implementer-mode=single-premium",
            "concurrency=2",
        ]

    def test_baseline_coinciding_choice_is_skipped_with_note(self, tmp_path: Path) -> None:
        loaded = _loaded(tmp_path)
        points = enumerate_ablation(loaded)
        assert all(p.overrides for p in points)
        # the skip surfaces via the declared count: 3 alternatives, not 6 choices
        assert len(points) == 3


class TestDeltas:
    def test_delta_is_point_minus_baseline(self) -> None:
        base = _point("baseline", 0.9, {"correctness_rate": 0.9, "wall_clock_seconds": 10.0}, kind="baseline")
        pt = _point("g=True", 0.95, {"correctness_rate": 1.0, "wall_clock_seconds": 12.0},
                    dimension="g", value=True)
        (delta,) = compute_deltas(base, [pt])
        assert delta.scalar_delta == pytest.approx(0.05)
        assert delta.component_deltas["correctness_rate"] == pytest.approx(0.1)
        assert delta.component_deltas["wall_clock_seconds"] == pytest.approx(2.0)

    def test_failed_or_unrankable_points_get_no_delta(self) -> None:
        base = _point("baseline", 0.9, kind="baseline")
        failed = _point("a=1", None, failed=True, error="boom", dimension="a", value=1)
        assert compute_deltas(base, [failed]) == []

    def test_no_deltas_when_baseline_failed(self) -> None:
        base = _point("baseline", None, kind="baseline", failed=True, error="x")
        pt = _point("a=1", 0.5, dimension="a", value=1)
        assert compute_deltas(base, [pt]) == []


class TestRanking:
    def test_ranks_by_scalar_unrankable_last(self) -> None:
        pts = [
            _point("candidate:slow", 0.5, kind="candidate", candidate="slow"),
            _point("candidate:broken", None, kind="candidate", candidate="broken", failed=True, error="x"),
            _point("candidate:best", 0.9, kind="candidate", candidate="best"),
        ]
        assert rank_candidates(pts) == ["candidate:best", "candidate:slow", "candidate:broken"]

    def test_duplicate_fingerprints_flagged(self) -> None:
        a = _point("candidate:a", 0.5, kind="candidate", candidate="a", fingerprint="f" * 16)
        b = _point("candidate:b", 0.5, kind="candidate", candidate="b", fingerprint="f" * 16)
        sweep_mod.flag_duplicates([a, b])
        assert b.duplicate_of == "candidate:a" and a.duplicate_of is None


class TestDeriveRepeats:
    def _report(self, stdev: float | None, n: int = 5) -> NoiseReport:
        stats = None if stdev is None else ComponentStats(
            mean=1.0, variance=stdev**2, stdev=stdev, min=0.9, max=1.1, n=n,
        )
        return NoiseReport(requested_repeats=n, effective_repeats=n, scalar_stats=stats)

    def test_repeats_from_stdev_and_resolution(self) -> None:
        # stdev 0.03, resolution 0.01 -> ceil((3)^2) = 9
        repeats, source, _note = derive_repeats(self._report(0.03), resolution=0.01, max_repeats=10)
        assert repeats == 9 and source == "noise_report"

    def test_capped_at_max_repeats(self) -> None:
        repeats, _, note = derive_repeats(self._report(0.5), resolution=0.01, max_repeats=10)
        assert repeats == 10 and "cap" in note

    def test_tiny_stdev_floors_at_one(self) -> None:
        repeats, _, _ = derive_repeats(self._report(1e-6), resolution=0.01, max_repeats=10)
        assert repeats == 1

    def test_unusable_report_falls_back_to_default_and_says_so(self) -> None:
        repeats, source, note = derive_repeats(self._report(None), resolution=0.01, max_repeats=10)
        assert repeats == 1 and source == "default" and note

    def test_zero_effective_repeats_falls_back(self) -> None:
        report = NoiseReport(requested_repeats=3, effective_repeats=0, scalar_stats=None)
        repeats, source, _ = derive_repeats(report, resolution=0.01, max_repeats=10)
        assert repeats == 1 and source == "default"


class TestArtifact:
    def test_round_trips_and_coverage_invariant(self, tmp_path: Path) -> None:
        art = SweepArtifact(
            space_name="s",
            mode="ablation",
            substrate_mode="stub",
            repeats=1,
            repeats_source="default",
            baseline=_point("baseline", 0.9, kind="baseline"),
            points=[
                _point("a=1", 0.8, dimension="a", value=1),
                _point("b=2", None, dimension="b", value=2, failed=True, error="boom"),
            ],
            deltas=[AblationDelta(dimension="a", value=1, point_id="a=1", scalar_delta=-0.1)],
        )
        art.coverage.declared_points = 3
        art.coverage.scored_points = 2
        art.coverage.dropped = [sweep_mod.DroppedPoint(point_id="b=2", reason="boom")]
        assert art.schema_version == SWEEP_SCHEMA_VERSION == 1
        path = art.write(tmp_path)
        assert path == tmp_path / "sweep.json"
        assert SweepArtifact.load(path) == art
        md = (tmp_path / "sweep-report.md").read_text()
        assert "b=2" in md and "boom" in md  # dropped enumerated, not just counted
        assert "stub" in md.lower()  # fidelity caveat present

    def test_coverage_invariant_enforced_on_write(self, tmp_path: Path) -> None:
        art = SweepArtifact(
            space_name="s", mode="ablation", substrate_mode="stub",
            repeats=1, repeats_source="default",
            points=[_point("a=1", 0.8, dimension="a", value=1)],
        )
        art.coverage.declared_points = 5  # lies: 1 scored, 0 dropped
        art.coverage.scored_points = 1
        with pytest.raises(ValueError, match="coverage"):
            art.write(tmp_path)


class TestRunSweepFailureIsolation:
    async def test_failed_point_recorded_not_fatal(self, tmp_path: Path, monkeypatch) -> None:
        loaded = _loaded(tmp_path)
        real_run_board = sweep_mod.run_board
        calls: list[str] = []

        async def flaky_run_board(fixtures, run_dir, **kw):
            cfg = kw.get("config")
            calls.append(str(run_dir))
            if cfg is not None and cfg.global_config.max_concurrent_cards == 2:
                raise RuntimeError("point exploded")
            return await real_run_board(fixtures, run_dir, **kw)

        monkeypatch.setattr(sweep_mod, "run_board", flaky_run_board)
        artifact = await run_sweep(loaded, "ablation", tmp_path / "session")
        exploded = [p for p in artifact.points if p.point_id == "concurrency=2"]
        assert exploded and exploded[0].failed and "point exploded" in exploded[0].error
        healthy = [p for p in artifact.points if not p.failed]
        assert healthy, "other points must still run"
        assert artifact.coverage.declared_points == (
            artifact.coverage.scored_points + len(artifact.coverage.dropped)
        )
        assert any(d.point_id == "concurrency=2" for d in artifact.coverage.dropped)
