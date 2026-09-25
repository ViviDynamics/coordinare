"""Spec 173 US2: the curator end to end against a fake board."""
from __future__ import annotations

from pathlib import Path

import pytest
from performer.workflows.curator import CuratorWorkflow

from tests.unit.workflows.curator._fakes import (
    FakeBoard,
    FakeToolkit,
    batch,
    issue,
    judgement,
    score,
    stand,
)


async def _run(issues, board: FakeBoard, tk: FakeToolkit, tmp_path: Path, **env):
    async def lister():
        return issues

    workflow = CuratorWorkflow(lister=lister, board=board)
    result = await workflow.run(stand(tmp_path), score(workflow_env=env), tk)
    return result.report["curation"]


@pytest.mark.asyncio
async def test_a_qualifying_issue_reaches_the_backlog_with_a_reason(tmp_path: Path) -> None:
    board = FakeBoard()
    rec = await _run([issue(1)], board, FakeToolkit([batch(judgement())]), tmp_path)
    assert rec["verdict"] == "curation_complete"
    assert [o["action"] for o in rec["outcomes"]] == ["added"]
    assert board.added == ["I_1"] and board.labels == [("I_1", "curator-proposed")]
    assert "testable acceptance criterion" in board.comments[0][1]


@pytest.mark.asyncio
async def test_the_curator_never_moves_a_card_into_the_dispatch_column(tmp_path: Path) -> None:
    """SC-004. The curator proposes; a human promotes."""
    board = FakeBoard()
    rec = await _run([issue(1)], board, FakeToolkit([batch(judgement())]), tmp_path)
    assert board.moved == [], "the curator must not move a card between columns"
    assert rec["outcomes"][0]["column"] == "Backlog"


@pytest.mark.asyncio
async def test_an_issue_that_does_not_qualify_is_left_alone(tmp_path: Path) -> None:
    board = FakeBoard()
    rec = await _run([issue(1)], board,
                     FakeToolkit([batch(judgement(qualifies=False, quote=""))]), tmp_path)
    assert [o["action"] for o in rec["outcomes"]] == ["skipped"]
    assert board.added == [] and board.comments == []


@pytest.mark.asyncio
async def test_a_reason_quoting_text_absent_from_the_issue_is_rejected(tmp_path: Path) -> None:
    board = FakeBoard()
    rec = await _run([issue(1)], board,
                     FakeToolkit([batch(judgement(quote="ships with a CLI"))]), tmp_path)
    assert rec["outcomes"] == [] and board.added == []
    assert "not in the issue" in rec["rejected_judgements"][0]["reason"]


@pytest.mark.asyncio
async def test_an_issue_already_on_the_board_is_not_added_twice(tmp_path: Path) -> None:
    board = FakeBoard(on_board={"I_1"})
    rec = await _run([issue(1)], board, FakeToolkit([batch(judgement())]), tmp_path)
    assert rec["candidates_seen"] == 0 and board.added == [] and board.comments == []


@pytest.mark.asyncio
async def test_an_issue_already_proposed_is_not_proposed_again(tmp_path: Path) -> None:
    board = FakeBoard()
    rec = await _run([issue(1, labels=["curator-proposed"])], board,
                     FakeToolkit([batch(judgement())]), tmp_path)
    assert rec["candidates_seen"] == 0 and board.added == []


@pytest.mark.asyncio
async def test_a_judgement_about_an_unsent_issue_is_rejected(tmp_path: Path) -> None:
    board = FakeBoard()
    rec = await _run([issue(1)], board,
                     FakeToolkit([batch(judgement("I_99"))]), tmp_path)
    assert board.added == []
    assert "was not in this batch" in rec["rejected_judgements"][0]["reason"]


@pytest.mark.asyncio
async def test_a_missing_board_id_is_reported_not_silently_skipped(tmp_path: Path) -> None:
    async def lister():
        return [issue(1)]

    tk = FakeToolkit([batch(judgement())])
    workflow = CuratorWorkflow(lister=lister, board=None)
    result = await workflow.run(stand(tmp_path), score(project_id=""), tk)
    rec = result.report["curation"]
    assert rec["verdict"] == "env_blocked" and "no project board id" in (rec["error"] or "")


@pytest.mark.asyncio
async def test_a_board_failure_is_recorded_rather_than_claimed_as_added(tmp_path: Path) -> None:
    board = FakeBoard(fail_add=True)
    rec = await _run([issue(1)], board, FakeToolkit([batch(judgement())]), tmp_path)
    assert rec["outcomes"][0]["action"] == "rejected"
    assert "could not add" in rec["outcomes"][0]["reason"]


@pytest.mark.asyncio
async def test_the_run_is_bounded_by_max_per_run(tmp_path: Path) -> None:
    board = FakeBoard()
    issues = [issue(n) for n in range(1, 6)]
    rec = await _run(issues, board, FakeToolkit([batch(judgement(f"I_{n}") for n in range(1, 3))]),
                     tmp_path, CURATOR_MAX_PER_RUN="2")
    assert rec["candidates_seen"] == 2


@pytest.mark.asyncio
async def test_an_empty_repository_costs_no_model_call(tmp_path: Path) -> None:
    tk = FakeToolkit()
    rec = await _run([], FakeBoard(), tk, tmp_path)
    assert rec["candidates_seen"] == 0 and tk.metrics.model_calls == 0


@pytest.mark.asyncio
async def test_the_run_writes_nothing(tmp_path: Path) -> None:
    tk = FakeToolkit([batch(judgement())])
    rec = await _run([issue(1)], FakeBoard(), tk, tmp_path)
    assert "git status --porcelain" in tk.commands and rec["write_free_check"] == ""


@pytest.mark.asyncio
async def test_a_qualifying_judgement_with_no_quote_is_rejected(tmp_path: Path) -> None:
    """A promotion with nothing quoted gives the human reviewing the backlog
    nothing to check the judgement against, so it is not a judgement."""
    board = FakeBoard()
    rec = await _run([issue(1)], board,
                     FakeToolkit([batch(judgement(quote=""))]), tmp_path)
    assert board.added == [] and rec["outcomes"] == []
    assert "not in the issue" in rec["rejected_judgements"][0]["reason"]


@pytest.mark.asyncio
async def test_a_whitespace_only_quote_is_rejected(tmp_path: Path) -> None:
    board = FakeBoard()
    rec = await _run([issue(1)], board,
                     FakeToolkit([batch(judgement(quote="   \n  "))]), tmp_path)
    assert board.added == [] and rec["rejected_judgements"]


@pytest.mark.asyncio
async def test_a_promotion_carries_its_quote_on_the_record(tmp_path: Path) -> None:
    """The justification must be checkable without reading GitHub."""
    rec = await _run([issue(1)], FakeBoard(),
                     FakeToolkit([batch(judgement())]), tmp_path)
    assert rec["outcomes"][0]["quote"] == "GET /health returns 200."
