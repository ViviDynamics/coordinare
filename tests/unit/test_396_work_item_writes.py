"""Issue #396: work item writes go through the provider.

A comment on a pull request is a code-host write and stays on the GitHub
service. A comment on a work item, or closing one, is a work item write: on a
second tracker it must reach that tracker, not a GitHub issue.

The guard below fails on any new issue write a graph node or a performer role
sends to GitHub directly. The sites still listed each say why: a pull request
comment, or a write an existing test pins to the GitHub service, which #396
leaves for a follow-up rather than edit the test that holds it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.services.board_provider import MoveOutcome
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

#: Calls that write to an issue or a project item: a work item, on GitHub Projects.
#: Coordinare's service methods and the performer's GitHub helpers.
ISSUE_WRITES = {
    "add_comment", "close_issue", "add_labels", "ensure_labels_exist", "post_issue_comment",
    "ensure_label", "add_item_to_project", "set_project_item_field", "link_to_project", "move_card",
}

_PR_COMMENT = "a pull request comment: the code host, not the work item"
_PINNED = (
    "pinned to the GitHub service by an existing test; moving it is the open part of #396"
)

#: "<path>::<function>::<method>" -> why that call may stay on the GitHub service.
ALLOWED: dict[str, str] = {
    "src/coordinare/graph/nodes/notify.py::_emit_ci_gate_rollup::add_comment": _PR_COMMENT,
    "src/coordinare/graph/nodes/notify.py::_emit_persona_scope_rollup::add_comment": _PR_COMMENT,
    "src/coordinare/graph/nodes/monitor/repair.py::_post_repair_comment::add_comment": _PR_COMMENT,
    "src/coordinare/services/orphan_pr.py::close_superseded_prs::add_comment": _PR_COMMENT,
    # tests/unit/test_410_decline_outcomes.py asserts the decline comment and the
    # close land on the GitHub service passed in, beside a separate board.
    "src/coordinare/graph/nodes/monitor/ui.py::_apply_assessor_decline::add_comment": _PINNED,
    "src/coordinare/graph/nodes/monitor/ui.py::_apply_assessor_decline::close_issue": _PINNED,
    # agent/performer/tests/unit/test_main.py asserts the QA performer posts the
    # evidence comment on the issue itself.
    "agent/performer/src/performer/qa_postprocess.py::finalize_qa::post_issue_comment": _PINNED,
}

#: The repository root, so the guard checks the same files from any directory.
ROOT = Path(__file__).resolve().parents[2]

#: Files that are the GitHub layer itself, or the seam's adapter over it.
NOT_SCANNED = {
    "src/coordinare/services/github.py",
    "src/coordinare/services/fake_github.py",
    "src/coordinare/services/board_provider.py",
    "agent/performer/src/performer/github.py",
}

#: The advocate and curator work on a repository's issues, not on work items.
PERFORMER_EXEMPT = {("workflows", "advocate"), ("workflows", "curator")}


def _enclosing(tree: ast.AST, line: int) -> str:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        inside = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.lineno <= line <= (node.end_lineno or node.lineno)
        )
        if inside and (best is None or node.lineno > best.lineno):
            best = node
    return best.name if best is not None else ""


def _issue_writes(paths: list[Path], root: Path = ROOT) -> list[str]:
    """Every issue write in *paths* not sent to ``board_provider``, as path::function::method."""
    found: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text())
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name not in ISSUE_WRITES:
                continue
            if isinstance(func, ast.Attribute) and ast.unparse(func.value) == "board_provider":
                continue
            found.append(f"{rel}::{_enclosing(tree, node.lineno)}::{name}")
    return found


def _scanned(directory: str) -> list[Path]:
    return [
        p for p in sorted((ROOT / directory).rglob("*.py"))
        if p.relative_to(ROOT).as_posix() not in NOT_SCANNED
    ]


def _coordinare_files() -> list[Path]:
    return [*_scanned("src/coordinare/graph"), *_scanned("src/coordinare/services")]


def _performer_role_files() -> list[Path]:
    package = ROOT / "agent/performer/src/performer"
    return [
        p for p in _scanned("agent/performer/src/performer")
        if tuple(p.relative_to(package).parts[:2]) not in PERFORMER_EXEMPT
    ]


def test_no_graph_node_or_service_writes_a_work_item_on_github_directly() -> None:
    offenders = sorted(set(_issue_writes(_coordinare_files())) - set(ALLOWED))
    assert not offenders, f"issue writes that skip the provider: {offenders}"


def test_no_performer_role_writes_a_work_item_on_github_directly() -> None:
    offenders = sorted(set(_issue_writes(_performer_role_files())) - set(ALLOWED))
    assert not offenders, f"performer issue writes outside the advocate and curator: {offenders}"


def test_every_allowance_is_still_a_real_site() -> None:
    """A stale allowance would let a new write hide behind an old name."""
    sites = set(_issue_writes(_coordinare_files())) | set(_issue_writes(_performer_role_files()))
    stale = sorted(set(ALLOWED) - sites)
    assert not stale, f"allowances with no call site left: {stale}"


def test_the_guard_sees_a_direct_write(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("async def f(github):\n    await github.add_comment('I_1', 'x')\n")
    assert _issue_writes([probe], root=tmp_path) == ["probe.py::f::add_comment"]


def test_the_guard_scans_real_files() -> None:
    """Run from any directory, it still finds the sites it allows."""
    assert len(_coordinare_files()) > 50
    assert len(_performer_role_files()) > 20


# --- the output token limit notice ------------------------------------------------------------


class _RecordingBoard:
    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    async def poll_board(self) -> dict[str, Any]:
        return {"snapshot": {}}

    async def move_card(self, card_id: str, status: str) -> MoveOutcome:
        return MoveOutcome.ok()

    async def get_card(self, card_id: str) -> dict[str, Any]:
        return {}

    async def add_card_comment(self, card_id: str, body: str) -> dict[str, Any]:
        self.comments.append((card_id, body))
        return {}

    async def get_card_comments(self, card_id: str, since_id: Any = None) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_the_token_limit_notice_goes_to_the_configured_board() -> None:
    github = AsyncMock()
    board = _RecordingBoard()
    state = _make_state(
        service=_Performer({"status": "token_limit"}), stage="architecting",
        card={"id": "PAY-7", "issue_id": "PAY-7", "status": "IN_PROGRESS"}, github=github,
    )
    state["board_provider"] = board

    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    assert [card for card, _ in board.comments] == ["PAY-7"]
    assert "output token limit reached" in board.comments[0][1]
    github.add_comment.assert_not_called()
