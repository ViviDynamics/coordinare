"""Spec 134 — the board-simulation runner.

``run_board`` injects a ``FakeGitHubService`` into the real ``CoordinareDaemon``,
drives every seeded card to a terminal state under a hard cycle/wall-clock budget,
and emits one schema-validated run artifact.

Two modes:

* **stub** (default, deterministic, no model cost) — the model-touching graph nodes
  (``dispatch_card`` / ``assess_card`` / ``classify_scope``) are overridden with a
  stub that does what the implementer performer would do: apply the fixture's
  ``solution_files`` to a real branch, open a PR on the fake, and jump the card to
  ``monitoring_pr``. The real ``monitor_pr`` → ``merge_pr`` path then drives it to
  DONE through the fake's real CI + gates_green approval + real local merge.
* **real** (opt-in via the CLI) — no overrides; real performers dispatch real
  models. (PR auto-discovery from pushed branches is a follow-on; see the
  ``# ponytail`` note.)
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import structlog

from coordinare.bench.approver import gates_green
from coordinare.bench.artifact import (
    CardOutcome,
    CIResult,
    ConfigFingerprint,
    Cost,
    GateDecision,
    Merge,
    PersonaDispatch,
    RunArtifact,
    Timing,
    estimate_cost_usd,
)
from coordinare.bench.fixtures import apply_solution_branch, materialize_repo, seed_board
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.state import _set_current_card
from coordinare.services.fake_github import FakeGitHubService

if TYPE_CHECKING:
    from coordinare.bench.fixtures import Fixture
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_TERMINAL_COLUMNS = {"DONE": "merged", "BLOCKED": "blocked"}


async def _no_sleep(_seconds: float) -> None:
    return None


def _make_stub_dispatch(
    fake: FakeGitHubService,
    fixtures_by_card: dict[str, Fixture],
    work_dir: Path,
    dispatch_log: list[dict[str, Any]],
) -> Any:
    """Override for the model-touching nodes: implement + open PR, jump to review."""

    async def _stub(state: dict[str, Any]) -> dict[str, Any]:
        card = dict(state.get("current_card") or {})
        card_id = str(card.get("id", ""))
        started = datetime.now(UTC)
        fx = fixtures_by_card.get(card_id)
        if fx is not None:
            branch = fx.branch()
            files = fx.solution_files or fx.base_files
            try:
                apply_solution_branch(fake._bare_repo, branch, files, work_dir)
                pr_id = fake.open_pr(issue_item_id=card_id, head_ref=branch)
                pr = fake._prs[pr_id]
                card["pr_url"] = pr["url"]
                card["pr_node_id"] = pr_id
                card["pr_number"] = pr["pr_number"]
            except Exception as exc:  # never abort the loop
                logger.warning("bench.stub_dispatch_failed", card_id=card_id, error=str(exc))
        card["previous_status"] = card.get("status", "TODO")
        card["status"] = "IN_REVIEW"
        _set_current_card(cast("CoordinareState", state), card)
        gh = state.get("github_service")
        if gh is not None:
            await gh.move_card(card_id, "IN_REVIEW")
        state["phase"] = "monitoring_pr"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        dispatch_log.append({
            "stage": "implementing",
            "role": "implementer",
            "card_id": card_id,
            "status": "succeeded",
            "started_at": started,
            "finished_at": datetime.now(UTC),
        })
        return state

    return _stub


async def _identity(state: dict[str, Any]) -> dict[str, Any]:
    return state


def _config_fingerprint(config_path: str | Path | None) -> ConfigFingerprint:
    if config_path and Path(config_path).exists():
        digest = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()[:16]
        return ConfigFingerprint(hash=digest, source_path=str(config_path))
    return ConfigFingerprint(hash="none", source_path=str(config_path or ""))


async def run_board(
    fixtures: list[Fixture],
    run_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    human_login: str = "reviewer1",
    max_cycles: int | None = None,
    cost_per_million_tokens: float = 3.0,
    stub: bool = True,
) -> RunArtifact:
    """Run the daemon over a simulated board and return a validated RunArtifact."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="bench-run-"))
    started_at = datetime.now(UTC)
    dispatch_log: list[dict[str, Any]] = []

    # ponytail: all fixtures share one bare repo (coordinare operates on a single
    # repo). From-scratch fixtures each plant a FAILING acceptance test on main, so
    # they cross-contaminate CI when combined — run whole-lifecycle fixtures one per
    # call (a clean base each). Give each fixture its own repo+daemon if multi-card
    # whole-lifecycle runs are ever needed.
    bare = materialize_repo(fixtures, scratch / "repos")
    fake = FakeGitHubService(
        bare_repo_path=bare,
        human_reviewers=[human_login],
        approver=gates_green,
        work_dir=scratch / "ci",
    )
    seed_board(fake, fixtures)
    card_ids = [f"PVTI_{i}" for i in range(1, len(fixtures) + 1)]
    fixtures_by_card = dict(zip(card_ids, fixtures, strict=False))

    overrides: dict[str, Any] = {}
    if stub:
        stub_node = _make_stub_dispatch(fake, fixtures_by_card, scratch / "sol", dispatch_log)
        overrides = {"dispatch_card": stub_node, "assess_card": stub_node, "classify_scope": _identity}
    graph = CoordinareGraphBuilder(node_overrides=overrides).build()

    if max_cycles is None:
        max_cycles = 3 * len(fixtures) + 6
    daemon = CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        max_cycles=max_cycles,
        sleep_func=_no_sleep,
        state_store=None,
    )
    daemon.state["github_service"] = fake
    daemon.state["human_reviewers"] = [human_login]
    daemon.state["lifecycle_sequence"] = ["assessing"]

    run_error: str | None = None
    try:
        await daemon.start()
    except Exception as exc:  # a fake/graph failure must still yield an artifact
        run_error = str(exc)
        logger.warning("bench.daemon_run_failed", error=run_error)

    finished_at = datetime.now(UTC)
    board = await fake.poll_board()
    artifact = _build_artifact(
        fake=fake,
        fixtures_by_card=fixtures_by_card,
        board=board,
        dispatch_log=dispatch_log,
        started_at=started_at,
        finished_at=finished_at,
        config_path=config_path,
        cost_rate=cost_per_million_tokens,
        run_error=run_error,
    )
    artifact.write(run_dir)

    await fake.aclose()
    shutil.rmtree(scratch, ignore_errors=True)
    return artifact


def _card_column(board: dict[str, Any], card_id: str) -> str:
    for column, ids in board.get("snapshot", {}).items():
        if card_id in ids:
            return str(column)
    return "UNKNOWN"


def _build_artifact(
    *,
    fake: FakeGitHubService,
    fixtures_by_card: dict[str, Fixture],
    board: dict[str, Any],
    dispatch_log: list[dict[str, Any]],
    started_at: datetime,
    finished_at: datetime,
    config_path: str | Path | None,
    cost_rate: float,
    run_error: str | None,
) -> RunArtifact:
    run_id = started_at.strftime("%Y%m%d-%H%M%S")
    cards: list[CardOutcome] = []
    for card_id, fx in fixtures_by_card.items():
        column = _card_column(board, card_id)
        if run_error is not None and column not in _TERMINAL_COLUMNS:
            final_state: str = "error"
        else:
            final_state = _TERMINAL_COLUMNS.get(column, "abandoned")

        pr = next((p for p in fake._prs.values() if p["issue_item_id"] == card_id), None)
        merged = bool(pr and pr["merged"])
        merge = Merge(
            merged=merged,
            merge_commit=(pr["merge_commit"] or {}).get("oid") if merged and pr else None,
            approved_by=next(
                (r["author_login"] for r in (pr["reviews"] if pr else []) if r.get("state") == "APPROVED"),
                None,
            ),
        )
        ci_results = [
            CIResult(
                head_sha=str(e.get("head_sha", "")),
                required_checks=["pytest"],
                conclusion="success" if e.get("pytest_exit") == 0 else "failure",
                pytest_exit=e.get("pytest_exit"),
            )
            for e in fake.events
            if e.get("kind") == "ci_run"
        ]
        gate_decisions = [
            GateDecision(stage="ci", verdict="pass" if c.conclusion == "success" else "bounce", head_sha=c.head_sha)
            for c in ci_results
        ]
        dispatches = [
            PersonaDispatch(
                stage=d["stage"], role=d["role"], status=d["status"],
                started_at=d["started_at"], finished_at=d["finished_at"],
            )
            for d in dispatch_log if d["card_id"] == card_id
        ]
        cards.append(CardOutcome(
            card_id=card_id,
            issue_ref=fake._cards.get(card_id, {}).get("issue_url", ""),
            title=fx.title,
            fixture_id=fx.id,
            final_state=final_state,  # type: ignore[arg-type]
            reached_stages=["implementing"] if dispatches else [],
            dispatches=dispatches,
            gate_decisions=gate_decisions,
            ci_results=ci_results,
            merge=merge,
            timing=Timing(
                first_dispatch_at=dispatches[0].started_at if dispatches else None,
                terminal_at=finished_at,
            ),
            cost=Cost(cost_usd=estimate_cost_usd(None, cost_rate)),
        ))

    artifact = RunArtifact(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        wall_clock_seconds=(finished_at - started_at).total_seconds(),
        config_fingerprint=_config_fingerprint(config_path),
        approver_policy="gates_green",
        fixture_manifest=",".join(fx.id for fx in fixtures_by_card.values()),
        cards=cards,
    )
    artifact.totals = artifact.compute_totals()
    return artifact
