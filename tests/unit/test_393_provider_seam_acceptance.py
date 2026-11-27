"""Epic #393: the acceptance checks for the work item provider seam.

Three of the epic's acceptance criteria are checked here, each against code that
already exists on main:

- No graph node, daemon path or dashboard serialiser reads a GitHub-shaped field
  from the work item listing. The guard below walks every module outside the
  provider layer. #395's guard covered eight named callers and five keys; this one
  covers the whole package and every key the GitHub Projects listing carries.
- An in-memory provider with string ids, string comment cursors and no issue
  numbers drives one item through the full lifecycle. #400's bench test reaches
  Done with a stub that skips straight to the pull request; this one runs the real
  graph with no node overrides, so every stage is dispatched and judged by the
  code production runs.
- A symphony with no provider configured runs on GitHub Projects with no
  configuration change, starting from a config file that has no provider key.

What counts as a work item read. The card dict still carries ``issue_id``,
``issue_number`` and ``issue_url`` (#395 left them). They are not reads of the
listing: ``check_board`` fills them from the typed record in one place
(``_issue_fields``), from ``WorkItem.issue`` (the code host's join key, set only by
the GitHub Projects translation) and ``WorkItem.url``. On another provider they
are the item's own id, ``0`` and the item's URL, and the lifecycle test below
shows every stage runs with exactly that.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

from coordinare.bench.approver import gates_green
from coordinare.bench.fixtures import apply_solution_branch, materialize_repo, tiny_fixture
from coordinare.config import CoordinareConfiguration, ProjectConfiguration
from coordinare.config_validation import (
    _load_raw_yaml,
    coerce_multi_symphony_raw,
    wrap_legacy_config,
)
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.services.board_provider import GitHubProjectsBoardProvider
from coordinare.services.fake_github import FakeGitHubService
from coordinare.services.in_memory_board import InMemoryBoardProvider
from coordinare.services.security_floor import SCAN_TOOLS, SCAN_VERSION
from coordinare.services.work_item_providers import build_board_provider
from coordinare.services.work_items import listed_text, work_items_of
from coordinare.workspace import make_branch_name

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "coordinare"

# --- no GitHub-shaped listing field read outside the provider layer -------------------------

#: Every per-item key the GitHub Projects listing carries (``GitHubService.poll_board``),
#: plus the ones a provider adds while listing.
LISTING_KEYS = frozenset({
    "titles", "descriptions", "issue_numbers", "issue_urls", "item_labels", "item_assignees",
    "content_node_ids", "pr_urls", "native_blockers", "status_updated_at", "issue_states",
    "item_field_values", "field_values", "blocker_states",
})

#: The provider layer: the modules that produce the GitHub Projects listing or
#: translate it into typed records. Everything else reads ``work_items_of``.
PROVIDER_LAYER = {
    "services/github.py": "the GitHub service: produces the listing",
    "services/fake_github.py": "the bench's GitHub service: produces the same listing",
    "dry_run.py": "the dry-run GitHub service: produces an empty listing",
    "services/work_items.py": "the translation from the GitHub listing to typed records",
    "services/board_provider.py": "the GitHub Projects adapter",
    "services/in_memory_board.py": "the in-memory provider's own listing",
}


_READ_METHODS = frozenset({"get", "pop", "setdefault"})


def _key_read(node: ast.AST) -> str | None:
    """The listing key *node* reads, if any: ``x["key"]``, ``x.get("key")`` or ``"key" in x``.

    A dict literal that builds a listing (a fake service, a dashboard payload of its
    own) is not a read, so it is not matched.
    """
    key: object = None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        key = node.slice.value
    elif (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in _READ_METHODS and node.args and isinstance(node.args[0], ast.Constant)
    ):
        key = node.args[0].value
    elif (
        isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
        and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
    ):
        key = node.left.value
    return key if isinstance(key, str) and key in LISTING_KEYS else None


def _listing_reads(path: Path, root: Path = PACKAGE) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        key = _key_read(node)
        if key is not None:
            found.append(f"{path.relative_to(root).as_posix()}:{getattr(node, 'lineno', 0)} {key!r}")
    return found


def test_nothing_outside_the_provider_layer_reads_a_listing_key() -> None:
    offenders: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.relative_to(PACKAGE).as_posix() in PROVIDER_LAYER:
            continue
        offenders.extend(_listing_reads(path))
    assert not offenders, f"GitHub listing keys read outside the provider layer: {offenders}"


def test_every_provider_layer_module_still_exists() -> None:
    missing = [name for name in PROVIDER_LAYER if not (PACKAGE / name).is_file()]
    assert not missing, f"allowances with no module left: {missing}"


def test_the_guard_sees_a_direct_read(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "def f(board):\n"
        "    a = board.get('item_field_values', {})\n"
        "    b = board['issue_numbers']\n"
        "    return a, b, 'native_blockers' in board, {'titles': {}}\n",
    )
    reads = sorted(line.split()[-1] for line in _listing_reads(probe, tmp_path))
    # Three reads; the dict literal that builds a listing is not one.
    assert reads == ["'issue_numbers'", "'item_field_values'", "'native_blockers'"], reads


class TestTheRecordCarriesWhatCheckBoardUsedToRead:
    def test_github_field_values_reach_the_record(self) -> None:
        """The key the listing fakes carry. GitHubService.poll_board does not emit it
        yet, so on GitHub Projects the record's fields are empty, as the sort's input was."""
        board = {
            "snapshot": {"TODO": ["I1"]},
            "issue_numbers": {"I1": 3},
            "item_field_values": {"I1": {"Priority": "P1", "Points": 3, "Owner": None}},
        }
        fields = work_items_of(board)["I1"].fields
        assert fields == {"Priority": "P1", "Points": "3"}
        with pytest.raises(TypeError):
            cast("dict[str, str]", fields)["Priority"] = "P0"

    def test_a_github_listing_without_field_values_gives_empty_fields(self) -> None:
        board = {"snapshot": {"TODO": ["I1"]}, "issue_numbers": {"I1": 3}}
        assert work_items_of(board)["I1"].fields == {}

    @pytest.mark.asyncio

    async def test_the_in_memory_provider_lists_its_fields(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        key = board.add_item("Pay", fields={"Priority": "P0"})
        listing = await board.poll_board()
        assert work_items_of(listing)[key].fields == {"Priority": "P0"}

    def test_listed_text_keeps_a_github_title_it_did_not_supply_absent(self) -> None:
        titles, bodies = listed_text({"snapshot": {"TODO": ["I1", "I2"]}, "titles": {"I1": "One"}})
        assert titles == {"I1": "One"}
        assert bodies == {}

    @pytest.mark.asyncio

    async def test_listed_text_reads_typed_records(self) -> None:
        board = InMemoryBoardProvider(project_key="PAY")
        key = board.add_item("Pay", body="the body")
        titles, bodies = listed_text(await board.poll_board())
        assert titles == {key: "Pay"}
        assert bodies == {key: "the body"}

    @pytest.mark.asyncio

    async def test_priority_sorts_items_from_a_provider_that_is_not_github(self) -> None:
        from coordinare.graph.nodes.check_board import _select_eligible_todo
        from coordinare.services.card_ownership import ownership_policy

        board = InMemoryBoardProvider(project_key="PAY")
        low = board.add_item("Low", fields={"Priority": "P2"})
        high = board.add_item("High", fields={"Priority": "P0"})
        listing = await board.poll_board()
        config = ProjectConfiguration(
            github_org="acme", project_name="demo", human_reviewers=["reviewer1"], github_token="fake-token",
            priority={"field_name": "Priority", "priority_order": ["P0", "P1", "P2"]},
        )
        state: Any = {"config": config}

        eligible = _select_eligible_todo(state, listing, [low, high], ownership_policy(config), {})

        assert eligible == [high, low]


# --- one item from To Do to Done through the real graph -------------------------------------

#: The whole lifecycle, as the bench's real mode drives it (``bench/runner.py``).
LIFECYCLE = [
    "assessing", "architecting", "implementing", "reviewing",
    "security", "qa", "documenting", "closing_review",
]

#: What a performer reports when its stage passes.
PASSED = {
    "assessing": "assessment_complete",
    "architecting": "plan_committed",
    "implementing": "pr_opened",
    "reviewing": "approved",
    "security": "security_passed",
    "qa": "qa_passed",
    "documenting": "docs_committed",
    "closing_review": "approved",
}


class _ScriptedPerformer:
    """Every stage passes. The implementer pushes the fixture's solution and opens
    the pull request on the code host, as a real implementer does."""

    def __init__(self, code_host: FakeGitHubService, solution: dict[str, str], work_dir: Path) -> None:
        self._code_host = code_host
        self._solution = solution
        self._work_dir = work_dir
        self.dispatches: list[dict[str, Any]] = []
        self._stage_of: dict[str, str] = {}

    async def check_health(self) -> dict[str, Any]:
        return {"status": "ready"}

    async def dispatch_card(self, card_context: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        session = f"s{len(self.dispatches) + 1}"
        self.dispatches.append(dict(card_context))
        self._stage_of[session] = str(card_context["role"])
        return {"session_id": session, "job_id": session, "status": "dispatched"}

    async def check_status(self, session: str, **_kw: Any) -> dict[str, Any]:
        stage = self._stage_of[session]
        context = next(c for c in reversed(self.dispatches) if c["role"] == stage)
        status: dict[str, Any] = {"status": PASSED[stage], "session_id": session}
        if stage == "implementing":
            branch = make_branch_name(context["id"], context["title"])
            apply_solution_branch(self._code_host._bare_repo, branch, self._solution, self._work_dir)
            pr_id = self._code_host.open_pr(
                issue_item_id=context["id"], head_ref=branch, body=context.get("work_item_pr_line", ""),
            )
            pr = self._code_host._prs[pr_id]
            status.update(pr_url=pr["url"], pr_number=pr["pr_number"], pr_node_id=pr_id, branch=branch)
        if stage == "security":
            status["report"] = {"security_floor": {
                "version": SCAN_VERSION, "complete": True, "tools": sorted(SCAN_TOOLS), "findings": [],
            }}
        if stage == "qa":
            # 396: the QA performer hands its evidence comment back for coordinare to post.
            status["work_item_comment"] = "QA evidence: test_multiply passes."
        return status

    def stages(self) -> list[str]:
        return [str(c["role"]) for c in self.dispatches]


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio


async def test_one_in_memory_item_goes_through_every_stage_to_done(tmp_path: Path) -> None:
    scratch = tmp_path
    fixture = tiny_fixture()
    code_host = FakeGitHubService(
        bare_repo_path=materialize_repo([fixture], scratch / "repos"),
        git_auth_enabled=False,
        human_reviewers=["reviewer1"],
        approver=gates_green,
        work_dir=scratch / "ci",
    )
    work_items = InMemoryBoardProvider(project_key="PAY")
    item = work_items.add_item(fixture.title, body=fixture.body)
    # The approver reads the item's state from the tracker, not from a GitHub project.
    code_host.card_status_lookup = work_items.status_of
    performer = _ScriptedPerformer(code_host, fixture.solution_files, scratch / "solution")

    daemon = CoordinareDaemon(
        CoordinareGraphBuilder().build(),  # no node overrides: the real graph
        poll_interval_seconds=1, max_cycles=40, sleep_func=_no_sleep, state_store=None,
    )
    daemon.state["config"] = ProjectConfiguration(
        github_org=code_host.org, project_name=code_host.project_name,
        human_reviewers=["reviewer1"], github_token="fake-token",
    )
    daemon.state["github_service"] = code_host
    daemon.state["board_provider"] = work_items
    daemon.state["human_reviewers"] = ["reviewer1"]
    daemon.state["performer_services"] = dict.fromkeys(LIFECYCLE, performer)
    daemon.state["lifecycle_sequence"] = LIFECYCLE
    daemon.state["performer_stage"] = LIFECYCLE[0]

    await daemon.start()

    # The item went from To Do to Done, every move coordinare's, through the provider.
    assert work_items.status_of(item) == "DONE"
    history = work_items.item(item).history
    assert [(before, after) for before, after, _ in history] == [
        ("To Do", "Doing"), ("Doing", "Review"), ("Review", "Done"),
    ]
    assert {actor for _, _, actor in history} == {"coordinare"}

    # Every stage ran, in order. Documenting is dispatched only for documentation
    # changes, and this fixture changes code alone, so the graph skips it.
    assert performer.stages() == [
        "assessing", "architecting", "implementing", "reviewing", "security", "qa", "closing_review",
    ]

    # Nothing GitHub-shaped about the item: its id is the tracker's key, there is no
    # issue number, and the code host names it by the provider's reference.
    for context in performer.dispatches:
        assert context["id"] == item == "PAY-1"
        assert context["issue_id"] == item
        assert context["issue_number"] == 0
        assert context["issue_url"] == "memory://work-items/PAY-1"
        assert context["work_item_ref"] == "PAY-1"

    # The pull request was opened, approved by the human reviewer and merged on the
    # code host, from a branch named by the key, linking the item instead of an issue.
    (pr,) = code_host._prs.values()
    assert pr["merged"] is True
    assert pr["head_ref"].startswith("coordinare/PAY-1/")
    assert "memory://work-items/PAY-1" in pr["body"]
    assert "Closes #" not in pr["body"]
    assert any(r["state"] == "APPROVED" and r["author_login"] == "reviewer1" for r in pr["reviews"])

    # The QA evidence comment reached the item through the provider, with the
    # tracker's own opaque comment id, and no work item write reached GitHub.
    (comment,) = work_items.item(item).comments
    assert comment["body"] == "QA evidence: test_multiply passes."
    assert isinstance(comment["id"], str)
    assert not comment["id"].isdigit()
    kinds = {event["kind"] for event in code_host.events}
    assert kinds.isdisjoint({"move_card", "add_comment", "close_issue", "add_labels", "seed_card"})


# --- no provider configured: GitHub Projects, unchanged -------------------------------------

_MULTI = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["reviewer1"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}
_LEGACY = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["reviewer1"],
    "project_name": "demo",
    "github_project_number": 100,
}


def _load_like_startup(path: Path) -> CoordinareConfiguration:
    raw = _load_raw_yaml(path)
    shaped = coerce_multi_symphony_raw(raw) if "symphonies" in raw else wrap_legacy_config(raw)
    return CoordinareConfiguration(**shaped)


@pytest.mark.parametrize("raw", [_MULTI, _LEGACY], ids=["symphonies", "legacy"])
def test_a_config_file_with_no_provider_key_reads_github_projects(tmp_path: Path, raw: dict[str, Any]) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    assert "work_items" not in path.read_text()

    root = _load_like_startup(path)

    (symphony,) = root.symphonies
    assert symphony.work_items is None
    assert symphony.work_item_provider == "github_projects"
    github = MagicMock(name="github")
    board = build_board_provider(symphony.work_items, github=github)
    assert isinstance(board, GitHubProjectsBoardProvider)
    assert board.github is github


def test_the_daemon_reads_a_symphony_with_no_provider_key_on_github_projects(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_MULTI, sort_keys=False))
    root = _load_like_startup(path)
    daemon = CoordinareDaemon(MagicMock(), poll_interval_seconds=1, max_cycles=1, sleep_func=_no_sleep)
    startup_github, symphony_github = MagicMock(name="startup"), MagicMock(name="demo")
    daemon._state["github_service"] = startup_github
    daemon._state["board_provider"] = build_board_provider(None, github=startup_github)
    daemon._state["symphony_github_services"] = {"demo": symphony_github}
    daemon._state["symphony_configs"] = {s.name: s for s in root.symphonies}

    swapped = daemon._swap_symphony_state_in("demo", None, {}, None)
    during = daemon._state["board_provider"]
    daemon._restore_symphony_swaps("demo", *swapped)

    assert isinstance(during, GitHubProjectsBoardProvider)
    assert during.github is symphony_github


@pytest.mark.asyncio


async def test_a_card_runs_to_merged_on_github_projects_from_a_config_with_no_provider_key(
    tmp_path: Path,
) -> None:
    from coordinare.bench.runner import run_board

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_MULTI, sort_keys=False))

    artifact = await run_board([tiny_fixture()], tmp_path / "run", stub=True, config=_load_like_startup(path))

    (card,) = artifact.cards
    assert card.card_id == "PVTI_1"  # a GitHub Projects item
    assert card.final_state == "merged"
