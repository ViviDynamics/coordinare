"""Spec 151 — regressions for the PR-review fixes.

Deterministic and free: no Docker, no model, no git daemon. Each test pins one
defect found reviewing the 151 branch, so the fix cannot silently regress.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from coordinare.bench.fake_github_server import FakeGitHubServer
from coordinare.bench.fixtures import materialize_repo, seed_board, tiny_fixture
from coordinare.bench.recording_performer import RecordingPerformer, is_recorder
from coordinare.bench.runner import _no_sleep, run_board
from coordinare.config import ProjectConfiguration
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.fake_github import FakeGitHubService


async def _fake_with_pr(tmp_path: Path):
    fx = tiny_fixture()
    bare = materialize_repo([fx], tmp_path / "repos")
    fake = FakeGitHubService(
        bare_repo_path=bare, human_reviewers=["reviewer1"], work_dir=tmp_path / "ci"
    )
    seed_board(fake, [fx])
    pr_id = fake.open_pr(issue_item_id="PVTI_1", head_ref="main")
    return fake, fake._prs[pr_id], bare


# ---------------------------------------------------------------------------
# Finding 2 — the AI reviewer's own APPROVE must not satisfy review_decision
# ---------------------------------------------------------------------------


async def test_bot_approve_does_not_count_as_approved(tmp_path: Path) -> None:
    fake, pr, _bare = await _fake_with_pr(tmp_path)
    pr["reviews"].append({"author_login": "coordinare-reviewer", "state": "APPROVED"})
    # A bot approve must NOT mark the PR approved: it would make _maybe_approve
    # early-return forever, so the human approval is never appended while
    # check_mergeability simultaneously reports mergeable=True.
    assert fake._review_decision(pr) == "REVIEW_REQUIRED"

    pr["reviews"].append({"author_login": "reviewer1", "state": "APPROVED"})
    assert fake._review_decision(pr) == "APPROVED"


async def test_bot_changes_requested_still_blocks(tmp_path: Path) -> None:
    # Only the APPROVED determination is human-gated — a reviewer performer must
    # still be able to block its own PR.
    fake, pr, _bare = await _fake_with_pr(tmp_path)
    pr["reviews"].append({"author_login": "coordinare-reviewer", "state": "CHANGES_REQUESTED"})
    assert fake._review_decision(pr) == "CHANGES_REQUESTED"


async def test_bot_approve_does_not_disable_the_human_approver(tmp_path: Path) -> None:
    fake, pr, _bare = await _fake_with_pr(tmp_path)
    fake._approver = lambda _state: True  # unconditional approver
    pr["reviews"].append({"author_login": "coordinare-reviewer", "state": "APPROVED"})

    await fake._maybe_approve(pr)

    humans = [r for r in pr["reviews"] if r["author_login"] == "reviewer1"]
    assert humans and humans[0]["state"] == "APPROVED"


# ---------------------------------------------------------------------------
# Finding 3 — an unresolvable ref must not read as green CI
# ---------------------------------------------------------------------------


async def test_unresolvable_ref_reads_pending_not_pass(tmp_path: Path) -> None:
    fake, _pr, bare = await _fake_with_pr(tmp_path)
    server = FakeGitHubServer(fake, bare_repo=bare, head_ref_index={}, scratch=tmp_path)
    async with TestClient(TestServer(server._build_app())) as client:
        resp = await client.get("/repos/bench-org/bench-repo/commits/deadbeef/check-runs")
        runs = (await resp.json())["check_runs"]

    # An EMPTY list means "pass" to summarise_check_runs, so it must not be empty.
    assert runs, "empty check_runs is a silent false-green CI gate"

    from performer.github import summarise_check_runs

    verdict, _failed = summarise_check_runs(runs)
    assert verdict == "pending"


# ---------------------------------------------------------------------------
# Finding 9 — comments must be readable, not write-only
# ---------------------------------------------------------------------------


async def test_posted_comment_is_readable_by_both_views(tmp_path: Path) -> None:
    fake, pr, bare = await _fake_with_pr(tmp_path)
    number = pr["pr_number"]
    server = FakeGitHubServer(fake, bare_repo=bare, head_ref_index={}, scratch=tmp_path)

    async with TestClient(TestServer(server._build_app())) as client:
        posted = await client.post(
            f"/repos/bench-org/bench-repo/issues/{number}/comments",
            json={"body": "advisory finding A"},
        )
        assert posted.status == 201
        # Performer view: raw GitHub wire shape (user.login), and it can SEE the
        # comment it just posted — otherwise the security dedup path reposts forever.
        wire = await (
            await client.get(f"/repos/bench-org/bench-repo/issues/{number}/comments")
        ).json()

    assert [c["body"] for c in wire] == ["advisory finding A"]
    assert wire[0]["user"]["login"]

    # Coordinare view: normalised id/author/body, and since_id watermarking works.
    normalised = await fake.get_issue_comments(number)
    assert [c["body"] for c in normalised] == ["advisory finding A"]
    assert await fake.get_issue_comments(number, since_id=normalised[0]["id"]) == []


# ---------------------------------------------------------------------------
# Findings 7 + 8 — the recorder must be isinstance-transparent and record failures
# ---------------------------------------------------------------------------


def test_recorder_is_transparent_to_isinstance() -> None:
    from coordinare.services.http_performer_service import HTTPPerformerService

    delegate = HTTPPerformerService(
        PerformerEndpointConfig(id="p1", mode="ephemeral", roles=["performer"], image="img")
    )
    svc = RecordingPerformer(delegate)

    # The graph gates env-cache volumes and devenv_root behind this isinstance.
    assert isinstance(svc, HTTPPerformerService)
    # ...but the runner must still be able to find its recorders.
    assert is_recorder(svc)
    assert not is_recorder(delegate)


async def test_failed_dispatch_is_still_recorded() -> None:
    class _Boom:
        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            raise RuntimeError("container start failed")

    svc = RecordingPerformer(_Boom())
    with pytest.raises(RuntimeError, match="container start failed"):
        await svc.dispatch_card({"stage": "implementing", "role": "implementing", "id": "PVTI_1"})

    # coordinare swallows dispatch exceptions, so without this the artifact would
    # report zero dispatches instead of one failed one.
    assert len(svc.dispatch_records) == 1
    rec = svc.dispatch_records[0]
    assert rec["status"] == "error"
    assert "container start failed" in rec["error"]
    assert rec["stage"] == "implementing"


# ---------------------------------------------------------------------------
# Findings 4 + 5 — real-mode teardown closes services; the budget is a real deadline
# ---------------------------------------------------------------------------


class _StubServer:
    def __init__(self) -> None:
        self.head_ref_index: dict[str, str] = {}
        self.stopped = False
        self.git_base_url = "git://127.0.0.1:9418"
        self.rest_base_url = "http://127.0.0.1:5599"
        self.graphql_url = "http://127.0.0.1:5599/graphql"
        self.performer_git_base_url = "git://host.docker.internal:9418"
        self.performer_rest_base_url = "http://host.docker.internal:5599"
        self.performer_graphql_url = "http://host.docker.internal:5599/graphql"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


def _config() -> ProjectConfiguration:
    return ProjectConfiguration(
        github_org="bench-org", project_name="bench-repo",
        human_reviewers=["reviewer1"], github_token="fake-token",
        agent_transport="kubernetes",
        # An ephemeral endpoint so teardown exercises the label sweep scope.
        performer_endpoints=[
            {"id": "bench-real", "mode": "ephemeral", "roles": ["performer"], "image": "img"}
        ],
    )


async def test_real_mode_acloses_the_services_it_built(tmp_path: Path, monkeypatch) -> None:
    closed: list[str] = []

    class _Svc:
        async def check_health(self) -> dict[str, Any]:
            return {"status": "ready"}

        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            return {"session_id": "s1", "job_id": "j1", "status": "dispatched"}

        async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
            return {"status": "working"}

        async def aclose(self) -> None:
            closed.append("yes")

    monkeypatch.setattr(
        "coordinare.bench.runner._build_bench_performer_services",
        lambda _config: {"assessing": RecordingPerformer(_Svc())},
    )
    # Also assert the label sweep backstop fires, scoped to the config's endpoint —
    # aclose() alone cannot see a container still inside wait_ready.
    swept: list[str | None] = []

    async def fake_sweep(performer_id: str | None = None) -> int:
        swept.append(performer_id)
        return 0

    monkeypatch.setattr(
        "coordinare.services.performer_lifecycle.cleanup_orphaned_containers", fake_sweep
    )

    await run_board(
        [tiny_fixture()], tmp_path / "run", stub=False, max_cycles=1,
        real_config=_config(), server=_StubServer(), sleep_func=_no_sleep,
    )

    # Without this the ephemeral container outlives board_bench.py, still billing.
    assert closed == ["yes"]
    assert swept == ["bench-real"]  # scoped, not a blanket sweep


async def test_wall_clock_budget_is_a_real_deadline(tmp_path: Path) -> None:
    import asyncio

    class _Hang:
        async def check_health(self) -> dict[str, Any]:
            return {"status": "ready"}

        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            await asyncio.sleep(30)  # a cycle far longer than the budget
            return {"session_id": "s1"}

        async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
            return {"status": "working"}

    server = _StubServer()
    # max_cycles alone cannot stop this: the budget must cancel the run itself.
    artifact = await run_board(
        [tiny_fixture()], tmp_path / "run", stub=False, max_cycles=10_000,
        real_config=_config(), server=server,
        performer_services={"assessing": RecordingPerformer(_Hang())},
        wall_clock_budget_seconds=0.5, sleep_func=_no_sleep,
    )

    # run_error is surfaced as final_state="error" (the artifact has no error field).
    assert artifact.cards[0].final_state == "error"
    assert server.stopped is True
    assert (tmp_path / "run" / "run.json").exists()


# ---------------------------------------------------------------------------
# Finding 4 (residual) — aclose() cannot see a container still in wait_ready,
# so teardown also sweeps by label, SCOPED to the bench's own endpoint ids.
# ---------------------------------------------------------------------------


async def test_orphan_sweep_can_be_scoped_to_one_performer_id(monkeypatch) -> None:
    from coordinare.services import performer_lifecycle

    seen: list[tuple[str, ...]] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        seen.append(args)
        return (0, "c1\n", "") if args[0] == "ps" else (0, "", "")

    monkeypatch.setattr(performer_lifecycle, "_run_docker", fake_run_docker)

    assert await performer_lifecycle.cleanup_orphaned_containers("bench-real") == 1
    assert "label=coordinare.performer.id=bench-real" in seen[0]

    # Unscoped (production startup sweep) keeps the broad label — unchanged.
    seen.clear()
    await performer_lifecycle.cleanup_orphaned_containers()
    assert "label=coordinare.performer.id" in seen[0]


def test_bench_sweep_scope_is_the_configs_ephemeral_endpoints() -> None:
    from coordinare.bench.runner import _bench_endpoint_ids

    cfg = ProjectConfiguration(
        github_org="o", project_name="p", github_token="t", human_reviewers=["r1"],
        performer_endpoints=[
            {"id": "bench-real", "mode": "ephemeral", "roles": ["performer"], "image": "img"},
            {"id": "persistent-one", "mode": "persistent", "roles": ["performer"],
             "image": "img", "endpoint": "http://127.0.0.1:9999"},
        ],
    )
    # Only ephemeral endpoints start containers, so only those are swept — and a
    # production coordinare using other endpoint ids is never touched.
    assert _bench_endpoint_ids(cfg) == ["bench-real"]


# ---------------------------------------------------------------------------
# PR #206 review (Jason733i) — a host/container git-base split is announced.
# The reviewer read performer_git_base_url-set-alone as "silently ignored"; it is
# actually honoured, which is the subtler problem: the coordinare and the
# performer then clone from different hosts with nothing saying so.
# ---------------------------------------------------------------------------


def test_split_git_base_url_is_logged() -> None:
    from structlog.testing import capture_logs

    from coordinare.workspace import WorkspaceManager

    cfg = ProjectConfiguration(
        github_org="o", project_name="p", github_token="t", human_reviewers=["r1"],
        performer_git_base_url="git://host.docker.internal:9418",
    )
    with capture_logs() as logs:
        WorkspaceManager(cfg, github_service=None)
    assert any(e.get("event") == "workspace.split_git_base_url" for e in logs)


def test_matching_git_base_urls_are_not_logged() -> None:
    from structlog.testing import capture_logs

    from coordinare.workspace import WorkspaceManager

    cfg = ProjectConfiguration(
        github_org="o", project_name="p", github_token="t", human_reviewers=["r1"],
    )
    with capture_logs() as logs:
        WorkspaceManager(cfg, github_service=None)
    assert not any(e.get("event") == "workspace.split_git_base_url" for e in logs)
