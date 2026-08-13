"""Spec 134 — end-to-end: the real daemon drives a seeded card to a terminal
state under a stubbed performer, and the run artifact validates (US1 / SC-006).

Deterministic and free — no model calls. The stub applies the fixture's
solution_files to a real branch, so the fake's real-pytest CI, gates_green
approval, and real local squash-merge all run.
"""

from __future__ import annotations

from pathlib import Path

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.runner import run_board


async def test_loop_closes_and_artifact_validates(tmp_path: Path) -> None:
    artifact = await run_board([tiny_fixture()], tmp_path / "run", stub=True)

    # The run emitted a schema-valid artifact (write() re-parses it).
    run_json = tmp_path / "run" / "run.json"
    assert run_json.exists()
    reloaded = RunArtifact.load(run_json)
    assert reloaded == artifact
    assert reloaded.totals.cards_total == 1

    card = reloaded.cards[0]
    assert card.final_state in {"merged", "blocked", "abandoned", "error"}


async def test_deterministic_stub_reaches_merged(tmp_path: Path) -> None:
    artifact = await run_board([tiny_fixture()], tmp_path / "run", stub=True)
    card = artifact.cards[0]
    assert card.final_state == "merged", f"expected merged, got {card.final_state}"
    assert card.merge.merged is True
    assert card.merge.approved_by == "reviewer1"
    assert artifact.totals.cards_merged == 1
    # CI genuinely ran pytest against the solution branch.
    assert any(c.conclusion == "success" for c in card.ci_results)
