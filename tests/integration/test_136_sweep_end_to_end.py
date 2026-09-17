"""Spec 136 — end-to-end: stub ablation + candidate sweeps over a real space,
and the SC-006 proof that an injected config point changes runtime behavior.

Deterministic and free — stubbed performers, judging disabled.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinare.bench.fixtures import Fixture
from coordinare.bench.runner import run_board
from coordinare.bench.space import load_space, materialize
from coordinare.bench.sweep import SweepArtifact, run_sweep
from tests.unit.test_136_space import BASELINE, SPACE


def _space(tmp_path: Path, space: dict | None = None):
    (tmp_path / "baseline.yaml").write_text(yaml.safe_dump(BASELINE))
    path = tmp_path / "space.yaml"
    path.write_text(yaml.safe_dump(space or SPACE))
    return load_space(path)


def _green_fixture(tag: str) -> Fixture:
    """A fixture whose acceptance test is green on main and whose solution is a
    real diff — safe to combine on one board (no CI cross-contamination)."""
    return Fixture(
        id=f"green-{tag}",
        title=f"Touch note_{tag}",
        body="benign",
        base_files={
            f"mod_{tag}.py": f"VALUE_{tag} = 1\n",
            f"test_{tag}.py": f"from mod_{tag} import VALUE_{tag}\n\n\ndef test_{tag}():\n    assert VALUE_{tag} == 1\n",
        },
        solution_files={f"note_{tag}.md": f"done {tag}\n"},
        ground_truth=f"note_{tag}.md exists; test_{tag} stays green",
    )


class TestSweepEndToEnd:
    async def test_stub_ablation_reconciles_and_fingerprints_differ(self, tmp_path: Path) -> None:
        small = {
            "name": "small",
            "baseline_config": "baseline.yaml",
            "dimensions": [
                {"name": "concurrency", "path": "global_config.max_concurrent_cards", "choices": [1, 2]},
                {
                    "name": "ci-gate",
                    "path": "symphonies.bench.persona_scope.ci_gate.enabled",
                    "choices": [False, True],
                },
            ],
        }
        loaded = _space(tmp_path, small)
        artifact = await run_sweep(loaded, "ablation", tmp_path / "session")

        assert (tmp_path / "session" / "sweep.json").exists()
        assert (tmp_path / "session" / "sweep-report.md").exists()
        reloaded = SweepArtifact.load(tmp_path / "session" / "sweep.json")
        assert reloaded == artifact
        # baseline + concurrency=2 + ci-gate=True
        assert artifact.coverage.declared_points == 3
        assert artifact.coverage.declared_points == (
            artifact.coverage.scored_points + len(artifact.coverage.dropped)
        )
        fingerprints = {artifact.baseline.fingerprint} | {p.fingerprint for p in artifact.points}
        assert len(fingerprints) == 3, "each point must carry its own materialized fingerprint"
        assert len(artifact.deltas) == len([p for p in artifact.points if not p.failed])
        # stub-fidelity caveat present in report + notes
        assert any("stub" in n for n in artifact.coverage.notes)

    async def test_candidates_rank_head_to_head(self, tmp_path: Path) -> None:
        loaded = _space(tmp_path)  # one candidate in SPACE; add a second inline
        two = {
            "name": "duo",
            "baseline_config": "baseline.yaml",
            "candidates": [
                {"name": "serial", "overrides": {"global_config.max_concurrent_cards": 1}},
                {"name": "parallel", "overrides": {"global_config.max_concurrent_cards": 2}},
            ],
        }
        loaded = _space(tmp_path, two)
        artifact = await run_sweep(loaded, "candidates", tmp_path / "session")
        assert set(artifact.ranking) == {"candidate:serial", "candidate:parallel"}
        assert len(artifact.ranking) == 2
        assert all(not p.failed for p in artifact.points)
        assert artifact.baseline is None


class TestInjectedConfigGovernsBehavior:
    """SC-006: a materialized config point changes what the run actually does.

    Lever: ``global_config.assignee_filter`` (spec 050) — read from
    ``state["config"]`` in check_board's pickup filter. The fake's cards carry
    no assignees, so a configured filter makes every card ineligible: the same
    fixture merges under the baseline and never dispatches under the filtered
    point. (``max_concurrent_cards`` is NOT observable here: the stub completes
    a card's whole lifecycle within one cycle, so pickup can never interleave —
    part of the recorded stub-fidelity caveat.)
    """

    async def test_assignee_filter_point_changes_run_outcome(self, tmp_path: Path) -> None:
        loaded = _space(tmp_path)
        baseline_cfg = materialize(loaded.baseline_dump, {})
        filtered_cfg = materialize(
            loaded.baseline_dump, {"global_config.assignee_filter": "ghost-login"},
        )
        fixtures = [_green_fixture("a")]

        merged = await run_board(fixtures, tmp_path / "run-baseline", config=baseline_cfg)
        held = await run_board(
            fixtures, tmp_path / "run-filtered", config=filtered_cfg, max_cycles=4,
        )

        assert merged.config_fingerprint.source_path == "<materialized>"
        assert merged.config_fingerprint.hash != held.config_fingerprint.hash
        assert merged.totals.cards_merged == 1, [c.final_state for c in merged.cards]
        assert held.totals.cards_merged == 0, [c.final_state for c in held.cards]
        assert held.cards[0].dispatches == []  # never picked up — config governed the run
