"""Issue #400 — the board bench drives a work item that has no GitHub issue.

The conformance suite checks each operation. This checks the whole lifecycle: the
real daemon and graph take ``BENCH-1`` from the in-memory provider's To Do to its
Done, while pull requests, CI and the merge stay on the bench's GitHub fake, which
is the split the epic keeps (work items from any provider, code on GitHub).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.runner import run_board
from coordinare.services.in_memory_board import InMemoryBoardProvider

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_one_item_goes_from_to_do_to_done_through_the_in_memory_provider(tmp_path: Path) -> None:
    board = InMemoryBoardProvider(project_key="BENCH")

    artifact = await run_board([tiny_fixture()], tmp_path / "run", stub=True, work_items=board)

    (card,) = artifact.cards
    assert card.card_id == "BENCH-1"
    assert card.final_state == "merged"
    assert card.merge.merged is True
    assert board.status_of("BENCH-1") == "DONE"
    # Every transition was coordinare's, through the provider, ending in Done.
    history = board.item("BENCH-1").history
    assert history, "coordinare never moved the item"
    assert {actor for _, _, actor in history} == {"coordinare"}
    assert history[0][0] == "To Do"
    assert history[-1][1] == "Done"
    # The item reference in the artifact is the tracker's, not a GitHub issue URL.
    assert card.issue_ref == "memory://work-items/BENCH-1"


@pytest.mark.asyncio
async def test_a_real_run_refuses_an_in_memory_board(tmp_path: Path) -> None:
    """Real performers read the issue from the fake server, which has no such item."""
    with pytest.raises(ValueError, match="stub=True"):
        await run_board([tiny_fixture()], tmp_path / "run", stub=False, work_items=InMemoryBoardProvider())


@pytest.mark.asyncio
async def test_without_a_provider_the_bench_runs_on_github_projects_as_before(tmp_path: Path) -> None:
    artifact = await run_board([tiny_fixture()], tmp_path / "run", stub=True)

    (card,) = artifact.cards
    assert card.card_id == "PVTI_1"
    assert card.final_state == "merged"
