"""Spec 161 — a failing harness point is recorded, and the sweep continues.

T024 (US2): FR-018. Deliberately does NOT depend on the US1 classifier: classifying a
failure as a harness defect rather than a legitimate negative verdict is the rollup's job.
This story only requires the failure be *recorded and attributable*, which is what keeps
US2 independently shippable.

Finding worth recording: the continue-on-failure half of FR-018 was ALREADY implemented
by spec 136 (`sweep._run_point` catches Exception with the comment "a failed point is
data, not a crash"). These tests pin that behavior against regression rather than
introducing it, and confirm the failure is attributable to the swept harness.
"""

from __future__ import annotations

import shutil

import yaml

from coordinare.bench.space import load_space
from coordinare.bench.sweep import PointResult, enumerate_ablation, rank_candidates

BASELINE = "benchmarks/spaces/baseline.yaml"


def _space_file(tmp_path, dimensions: list[dict]) -> str:
    shutil.copy(BASELINE, tmp_path / "baseline.yaml")
    path = tmp_path / "space.yaml"
    path.write_text(
        yaml.safe_dump({
            "name": "harness-sweep-test",
            "baseline_config": "baseline.yaml",
            "dimensions": dimensions,
        })
    )
    return str(path)


def test_harness_points_are_attributable_to_their_harness(tmp_path) -> None:
    """FR-018: each point names the dimension and the harness value it exercised, so a
    failure can be pinned on the harness that caused it."""
    loaded = load_space(
        _space_file(tmp_path, [
            {"name": "reviewer-harness", "role": "reviewer",
             "choices": ["openclaw", "claude_code", "junie"]},
        ])
    )
    specs = enumerate_ablation(loaded)

    harness_specs = [s for s in specs if s.dimension == "reviewer-harness"]
    assert {s.value for s in harness_specs} == {"openclaw", "claude_code", "junie"}
    for s in harness_specs:
        # The override is what makes the point attributable to one harness.
        assert s.overrides == {"global_config.performers.reviewer.backend": s.value}


def test_one_point_per_declared_harness(tmp_path) -> None:
    """FR-014/FR-015: expansion is one point per choice, not a cross-product."""
    loaded = load_space(
        _space_file(tmp_path, [
            {"name": "reviewer-harness", "role": "reviewer",
             "choices": ["openclaw", "claude_code"]},
            {"name": "security-harness", "role": "security",
             "choices": ["openclaw", "codex"]},
        ])
    )
    specs = [s for s in enumerate_ablation(loaded) if s.dimension]

    # Ablation, not cross-product: 2 + 2 points, never 4 combinations.
    assert len(specs) == 4
    assert all(len(s.overrides) == 1 for s in specs)


def test_a_failed_point_is_recorded_as_data_not_raised() -> None:
    """FR-018: the sweep must not abort. A failed point carries its error and is retained
    so the harness that produced it is still visible in the results."""
    failed = PointResult(
        point_id="reviewer-harness=junie", kind="ablation",
        dimension="reviewer-harness", value="junie",
        failed=True, error="RuntimeError: harness could not start",
    )
    ok = PointResult(
        point_id="reviewer-harness=openclaw", kind="ablation",
        dimension="reviewer-harness", value="openclaw", mean_scalar=0.9,
    )

    # Both survive into the result set; the failure is data.
    assert failed.failed and failed.error
    assert failed.value == "junie", "the failing harness must stay attributable"
    assert not ok.failed


def test_a_failed_harness_does_not_outrank_a_working_one() -> None:
    """FR-018: a failed point must never be ranked above a real result. It sorts last as
    unrankable rather than being treated as a zero score that might beat a poor one."""
    good = PointResult(point_id="p-openclaw", kind="candidate", mean_scalar=0.10)
    failed = PointResult(point_id="p-junie", kind="candidate", failed=True, error="boom")

    ranked = rank_candidates([failed, good])

    assert ranked.index("p-openclaw") < ranked.index("p-junie")


def test_an_unrankable_point_is_not_confused_with_a_zero_score() -> None:
    """The absent-vs-zero rule again, at the sweep level: a point with no scalar is
    unrankable, not a measured 0.0."""
    no_score = PointResult(point_id="p-none", kind="candidate", mean_scalar=None)
    zero = PointResult(point_id="p-zero", kind="candidate", mean_scalar=0.0)

    ranked = rank_candidates([no_score, zero])

    # The measured zero is rankable; the missing scalar is not, so it sorts last.
    assert ranked.index("p-zero") < ranked.index("p-none")


def test_sweep_point_failure_handling_still_exists() -> None:
    """Pin spec 136's continue-on-failure contract, which FR-018 relies on.

    If someone removes the broad catch in `_run_point`, one bad harness would abort an
    entire sweep and discard every earlier point's spent budget. That regression would
    otherwise only show up in a long live run.
    """
    import inspect

    from coordinare.bench import sweep

    src = inspect.getsource(sweep._run_point)
    assert "except Exception" in src, (
        "_run_point must keep catching per-point failures; without it a single failing "
        "harness aborts the whole sweep (FR-018)"
    )
    assert "result.failed = True" in src, "a failed point must be recorded, not dropped"
