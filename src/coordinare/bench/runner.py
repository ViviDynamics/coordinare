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

import asyncio
import contextlib
import hashlib
import math
import shutil
import tempfile
import time
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
from coordinare.bench.recording_performer import RecordingPerformer, is_recorder
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.state import _set_current_card
from coordinare.services import performer_lifecycle
from coordinare.services.fake_github import FakeGitHubService

if TYPE_CHECKING:
    from coordinare.bench.fixtures import Fixture
    from coordinare.config import CoordinareConfiguration
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_TERMINAL_COLUMNS = {"DONE": "merged", "BLOCKED": "blocked"}


async def _no_sleep(_seconds: float) -> None:
    return None


def _real_mode_max_cycles(wall_clock_budget_seconds: float, poll_interval: float) -> int:
    """Cycles that fit in the wall-clock budget at ``poll_interval`` spacing.

    A SECONDARY ceiling only — it does NOT bound run time. A daemon cycle costs
    (graph work + poll), and real graph work can block for minutes (wait_ready
    allows 120s, plus the backend npm install), so these cycles can take far
    longer than ``budget``. ``run_board`` enforces the budget itself with an
    ``asyncio.wait_for`` deadline around ``daemon.start()``.
    """
    return max(1, math.ceil(wall_clock_budget_seconds / max(poll_interval, 0.001)))


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


def _config_fingerprint(
    config_path: str | Path | None, config: CoordinareConfiguration | None = None,
) -> ConfigFingerprint:
    if config is not None:
        # Spec 136: an injected (materialized) config is fingerprinted by its own
        # canonical content — two distinct sweep points must never share a hash
        # (incl. configs differing only in SecretStr values, which a plain
        # model_dump_json would mask into collision).
        from coordinare.bench.space import config_fingerprint as _space_fingerprint

        return ConfigFingerprint(hash=_space_fingerprint(config), source_path="<materialized>")
    if config_path and Path(config_path).exists():
        digest = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()[:16]
        return ConfigFingerprint(hash=digest, source_path=str(config_path))
    return ConfigFingerprint(hash="none", source_path=str(config_path or ""))


# 151: the whole-lifecycle stage sequence a real run drives (assessor → closer).
# Mirrors __main__._ROLE_TO_STAGE order; the config's enabled gate stages make
# gates_green's IN_REVIEW proxy valid (US4). advocate is intake-only, excluded.
_FULL_LIFECYCLE_SEQUENCE = [
    "assessing", "architecting", "implementing", "reviewing",
    "security", "qa", "documenting", "closing_review",
]


def _bench_endpoint_ids(config: Any) -> list[str]:
    """Endpoint ids declared by the bench config — the sweep scope for teardown."""
    return [
        ep.id
        for ep in (getattr(config, "performer_endpoints", None) or [])
        if getattr(ep, "mode", "") == "ephemeral" and getattr(ep, "id", "")
    ]


def _head_ref_index(fixtures_by_card: dict[str, Fixture]) -> dict[str, str]:
    """head branch → seeded card id, so POST /pulls resolves the pushed branch back
    to the card ``open_pr()`` needs (D3, T025).

    Must be the COORDINARE's canonical branch (``make_branch_name`` from card id +
    title), not ``fx.branch()`` — that is the stub's solution-branch name, not the
    PR head a real performer opens against the fake REST ``/pulls``. Single source
    of truth: this used to be duplicated in run_board and _setup_real_mode, which
    meant the T025 regression had two places to reappear.
    """
    from coordinare.workspace import make_branch_name

    return {make_branch_name(cid, fx.title): cid for cid, fx in fixtures_by_card.items()}


def _build_bench_performer_services(config: Any) -> dict[str, Any]:
    """Register one RecordingPerformer(HTTPPerformerService) per lifecycle stage.

    ponytail: a single-card bench needs no slot pools — each ephemeral endpoint
    spins its own container per dispatch, so one shared service per stage is
    enough (vs. __main__'s full pool/slot bootstrap). Ephemeral endpoints stay
    BRIDGED and must declare (see examples/bench-real.yaml)
    ``extra_hosts: ["host.docker.internal:host-gateway"]`` plus
    ``env.ALLOW_INSECURE_REPO_URL: "1"`` and ``env.ALLOW_HOST_GATEWAY_GITHUB: "1"``,
    so the container reaches the harness fakes over host.docker.internal.
    """
    from coordinare.__main__ import _ROLE_TO_STAGE  # type: ignore[attr-defined]
    from coordinare.services.http_performer_service import HTTPPerformerService

    services: dict[str, Any] = {}
    for ep in getattr(config, "performer_endpoints", []):
        if ep.mode == "subprocess":
            continue
        svc = RecordingPerformer(HTTPPerformerService(ep))
        for role in ep.roles:
            stage = _ROLE_TO_STAGE.get(role, role)
            services.setdefault(stage, svc)
    return services


def _unique_recorders(services: dict[str, Any]) -> list[Any]:
    """The distinct RecordingPerformers behind the stage→service map (one service
    is usually registered under several stages)."""
    out: list[Any] = []
    seen: set[int] = set()
    for svc in services.values():
        if is_recorder(svc) and id(svc) not in seen:
            seen.add(id(svc))
            out.append(svc)
    return out


def _dispatch_status(marker: str) -> str:
    """Map the performer's terminal marker onto the artifact's DispatchStatus."""
    from coordinare.graph.nodes.monitor_performer import TERMINAL_SUCCESS_STATES

    if marker in TERMINAL_SUCCESS_STATES or marker in {"succeeded", "ok"}:
        return "succeeded"
    if marker in {"error", "cancelled"}:
        return marker
    if not marker:
        return "cancelled"  # never reached a terminal status (budget/teardown cut it off)
    return "failed"  # changes_requested, qa_failed, security_failed, blocked, ...


def _records_to_dispatch_log(services: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten each RecordingPerformer's records into artifact dispatch rows, joining
    every dispatch to its terminal status by session — the real finish time (hence
    seconds), the true terminal marker and tokens, not a run-end stamp."""
    terminal_by_session: dict[str, dict[str, Any]] = {}
    # 306: the LAST poll that raised, per session. Used only as a fallback when
    # no terminal status was ever observed — a poll can raise transiently (a
    # deadline exceeded during a long build) while the job runs on to a real
    # verdict, and that verdict must win.
    poll_error_by_session: dict[str, dict[str, Any]] = {}
    for svc in _unique_recorders(services):
        for sr in svc.status_records:
            sid = sr.get("session_id")
            if sid:
                terminal_by_session.setdefault(sid, sr)  # first terminal wins
        for er in getattr(svc, "status_error_records", []):
            sid = er.get("session_id")
            if sid:
                poll_error_by_session[sid] = er  # last failure wins
    log: list[dict[str, Any]] = []
    for svc in _unique_recorders(services):
        for dr in svc.dispatch_records:
            sid = dr.get("session_id")
            term = terminal_by_session.get(sid) or {}
            marker = str(term.get("status") or "")
            # Only surface a poll failure when nothing terminal was seen for the
            # session: otherwise the dispatch DID reach a verdict and a healed
            # transient poll is noise.
            failed_poll = {} if term else (poll_error_by_session.get(sid) or {})
            poll_error = failed_poll.get("error")
            started = dr.get("started_at")
            ended = term.get("at") or failed_poll.get("at")
            # A dispatch the performer never accepted has no session to join.
            if dr.get("status") == "error":
                status = "error"
            elif poll_error:
                # The terminal state was never observed because polling itself
                # failed. That is not a budget cutoff, and reporting it as one
                # made a real executor failure read as "nothing happened".
                status = "error"
            else:
                status = _dispatch_status(marker)
            log.append({
                "poll_error": poll_error,
                "stage": dr.get("stage", ""),
                "role": dr.get("role", ""),
                "card_id": dr.get("card_id", ""),
                "model": dr.get("model", ""),
                "backend": dr.get("backend", ""),
                "status": status,
                "terminal_marker": marker or None,
                "job_id": dr.get("job_id"),
                "session_id": dr.get("session_id"),
                "container_id": dr.get("container_id"),
                "tokens_processed": term.get("tokens_processed"),
                "started_at": started,
                "finished_at": ended,
                "seconds": (
                    (ended - started).total_seconds()
                    if isinstance(started, datetime) and isinstance(ended, datetime)
                    else None
                ),
            })
    return log


async def run_board(
    fixtures: list[Fixture],
    run_dir: str | Path,
    *,
    config_path: str | Path | None = None,
    human_login: str = "reviewer1",
    max_cycles: int | None = None,
    cost_per_million_tokens: float = 3.0,
    stub: bool = True,
    config: CoordinareConfiguration | None = None,
    real_config: Any = None,
    performer_services: dict[str, Any] | None = None,
    server: Any = None,
    real_poll_interval_seconds: float = 5.0,
    wall_clock_budget_seconds: float = 1200.0,
    sleep_func: Any = None,
) -> RunArtifact:
    """Run the daemon over a simulated board and return a validated RunArtifact.

    ``config`` (spec 136) injects a materialized root configuration at the same
    seams the production daemon uses: ``state["config"]`` = the global
    ProjectConfiguration (``__main__.py:841``) and ``state["symphony_configs"]``
    keyed by symphony name (``__main__.py:976``), so global knobs and
    per-symphony gates govern the run. ``None`` keeps prior behavior.

    Real mode (``stub=False``, spec 151) stands up the ``FakeGitHubServer``
    boundary, points the coordinare's git/REST/graphql at it, and dispatches real
    performers over the Docker bridge
    (``--add-host=host.docker.internal:host-gateway`` + ``ALLOW_INSECURE_REPO_URL``
    + ``ALLOW_HOST_GATEWAY_GITHUB``) — portable across Docker Desktop and native
    Linux. ``real_config`` / ``performer_services`` / ``server`` are
    bench-internal injection points (the real run builds them from
    ``config_path``; tests inject fakes). ``real_config`` is a single
    ProjectConfiguration and is deliberately NOT spec 136's ``config``: that one
    is a root CoordinareConfiguration for sweep points. Distinct types, distinct
    jobs, so they stay distinct parameters.

    Stub mode drains the board with a tiny no-sleep cycle budget. Real mode blocks
    on real per-stage model calls, so it sleeps ``real_poll_interval_seconds``
    between polls and is bounded by ``wall_clock_budget_seconds`` (converted to a
    ``max_cycles`` ceiling). ``sleep_func`` overrides the sleep (tests pass
    ``_no_sleep`` to keep the real-mode loop instant).
    """
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
        # Stub dispatch uses the local bare repository directly. Exposing an auth
        # token enables live ls-remote preflight against the configured GitHub host.
        git_auth_enabled=not stub,
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

    # Stub mode completes each cycle's work synchronously, so no-sleep + a tiny
    # cycle budget drains the board instantly (deterministic, free). Real mode
    # blocks on real model calls per stage, so it MUST sleep between polls and be
    # budgeted by wall clock — the daemon has no per-run wall-clock stop, so we
    # express the budget as max_cycles = budget / poll_interval.
    if stub:
        poll_interval: float = 1
        cycle_sleep = _no_sleep if sleep_func is None else sleep_func
        if max_cycles is None:
            max_cycles = 3 * len(fixtures) + 6
    else:
        poll_interval = real_poll_interval_seconds
        cycle_sleep = asyncio.sleep if sleep_func is None else sleep_func
        if max_cycles is None:
            max_cycles = _real_mode_max_cycles(wall_clock_budget_seconds, poll_interval)
    daemon = CoordinareDaemon(
        graph,
        poll_interval_seconds=poll_interval,
        max_cycles=max_cycles,
        sleep_func=cycle_sleep,
        state_store=None,
    )
    daemon.state["github_service"] = fake
    daemon.state["human_reviewers"] = [human_login]
    daemon.state["lifecycle_sequence"] = ["assessing"]
    if config is not None:
        daemon.state["config"] = config.global_config
        daemon.state["symphony_configs"] = {s.name: s for s in config.symphonies}
        from coordinare.graph.state import SymphonyRuntimeState
        daemon.state["symphony_states"] = {
            s.name: SymphonyRuntimeState(name=s.name) for s in config.symphonies
        }

    # Build the fake server up front (real mode) so the finally can always tear it
    # down — even if start()/wiring raises partway (FR-009, D9).
    real_server = None
    if not stub:
        from coordinare.bench.fake_github_server import FakeGitHubServer
        real_server = server or FakeGitHubServer(
            fake, bare_repo=bare, head_ref_index=_head_ref_index(fixtures_by_card), scratch=scratch,
            org=fake.org, project=fake.project_name,
            # Bind all interfaces so a bridged container reaches in over host-gateway;
            # advertise 127.0.0.1 to the host and host.docker.internal to the container.
            bind_host="0.0.0.0",
        )

    real_services: dict[str, Any] | None = None
    run_error: str | None = None
    try:
        if not stub:
            real_services = await _setup_real_mode(
                real_server,
                fake=fake,
                fixtures_by_card=fixtures_by_card,
                daemon=daemon,
                human_login=human_login,
                config=real_config,
                config_path=config_path,
                performer_services=performer_services,
            )
        if stub:
            await daemon.start()
        else:
            # 151 review fix: max_cycles is NOT a wall-clock bound. The daemon runs
            # a full graph pass and only THEN sleeps poll_interval (daemon.py:3255),
            # and a pass can block for minutes (wait_ready alone allows 120s, plus
            # the backend npm install), so `ceil(budget/poll)` cycles can run for
            # hours on a paid lane advertised as a 20-minute budget. Enforce the
            # budget with a real deadline; max_cycles stays as a secondary ceiling.
            _t0 = time.monotonic()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(daemon.start(), timeout=wall_clock_budget_seconds)
            # Elapsed time — not a raised TimeoutError — is the source of truth:
            # asyncio.wait_for RETURNS NORMALLY when the awaited coroutine swallows
            # the cancellation, and the daemon loop does exactly that
            # (`except asyncio.CancelledError: ... break`, daemon.py:3256). Keying
            # off the exception alone would cut the run off but record no error, so
            # a truncated run would look like a clean finish.
            if time.monotonic() - _t0 >= wall_clock_budget_seconds:
                run_error = f"wall-clock budget of {wall_clock_budget_seconds}s exhausted"
                logger.warning("bench.wall_clock_budget_exhausted", error=run_error)
                daemon.stop()
    except Exception as exc:  # a fake/graph/setup failure must still yield an artifact
        run_error = str(exc)
        logger.warning("bench.daemon_run_failed", error=run_error)
    finally:
        # Teardown-safe (FR-009, D9): stop the fake server even on failure.
        if real_server is not None:
            try:
                await real_server.stop()
            except Exception as exc:  # pragma: no cover - stop() already swallows
                logger.warning("bench.server_stop_failed", error=str(exc))
        # 151 review fix: aclose() the performer services too. The EXPECTED end of a
        # real run is the budget cutting off mid-dispatch, and aclose() is what
        # cancels the log-poll tasks and reaps _active_jobs — without it a
        # `docker run -d` performer container outlives board_bench.py and keeps
        # burning model tokens. Only close services this function built; injected
        # ones belong to the caller.
        if real_services is not None and performer_services is None:
            for svc in _unique_recorders(real_services):
                closer = getattr(svc, "aclose", None)
                if closer is None:
                    continue
                try:
                    await closer()
                except Exception as exc:
                    logger.warning("bench.performer_aclose_failed", error=str(exc))
            # aclose() only reaps containers already in _active_jobs, and a job is
            # registered there AFTER wait_ready (up to 120s) — so a container still
            # starting when the budget expires is invisible to it and survives the
            # run (observed). Sweep by label as the backstop, scoped to THIS bench's
            # endpoint ids so a production coordinare on the same host is untouched.
            for ep_id in _bench_endpoint_ids(daemon.state.get("_bench_resolved_config") or real_config):
                try:
                    swept = await performer_lifecycle.cleanup_orphaned_containers(ep_id)
                    if swept:
                        logger.info("bench.swept_orphan_containers", performer_id=ep_id, count=swept)
                except Exception as exc:
                    logger.warning("bench.container_sweep_failed", performer_id=ep_id, error=str(exc))

    if not stub and real_services is not None:
        dispatch_log = _records_to_dispatch_log(real_services)

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
        config=config,
        cost_rate=cost_per_million_tokens,
        run_error=run_error,
    )
    artifact.write(run_dir)

    await fake.aclose()
    shutil.rmtree(scratch, ignore_errors=True)
    return artifact


async def _setup_real_mode(
    real_server: Any,
    *,
    fake: FakeGitHubService,
    fixtures_by_card: dict[str, Fixture],
    daemon: Any,
    human_login: str,
    config: Any,
    config_path: str | Path | None,
    performer_services: dict[str, Any] | None,
) -> dict[str, Any]:
    """Wire the daemon for a real-performer run: start the fake boundary, point the
    coordinare's git/REST/graphql at loopback, and register real performer services."""
    from coordinare.workspace import WorkspaceManager

    # Seed the index on an INJECTED server too (run_board only passes it to the one
    # it constructs itself).
    if getattr(real_server, "head_ref_index", None) is not None:
        real_server.head_ref_index.update(_head_ref_index(fixtures_by_card))
    await real_server.start()

    if config is None:
        if not config_path:
            raise ValueError("real mode requires a config (or config_path) — none provided")
        from coordinare.config import ProjectConfiguration
        from coordinare.config_validation import _load_raw_yaml
        config = ProjectConfiguration(**_load_raw_yaml(Path(config_path)))

    # Point every git-host injection point at the fake (defaults stay production; a
    # run is "real bench mode" only because we set these here). Two views: the host
    # (coordinare clone + rebase) uses 127.0.0.1; the container (performer clone +
    # REST/GraphQL) uses host.docker.internal — the coordinare never calls the REST/
    # GraphQL fakes over HTTP (it uses the in-process FakeGitHubService), so those
    # two are purely container-facing.
    config = config.model_copy(update={
        "git_base_url": real_server.git_base_url,
        "performer_git_base_url": real_server.performer_git_base_url,
        "github_api_url": real_server.performer_rest_base_url,
        "github_graphql_url": real_server.performer_graphql_url,
    })

    services = performer_services or _build_bench_performer_services(config)

    daemon.state["config"] = config
    daemon.state["config_path"] = str(config_path) if config_path else None
    daemon.state["workspace_manager"] = WorkspaceManager(config, github_service=fake)
    daemon.state["performer_services"] = services
    daemon.state["_bench_resolved_config"] = config  # teardown reads endpoint ids off this
    daemon.state["human_reviewers"] = list(getattr(config, "human_reviewers", [human_login]) or [human_login])
    daemon.state["lifecycle_sequence"] = _FULL_LIFECYCLE_SEQUENCE
    daemon.state["performer_stage"] = _FULL_LIFECYCLE_SEQUENCE[0]
    return services


def _human_logins(fake: FakeGitHubService) -> set[str]:
    return {r.strip().lower() for r in getattr(fake, "_human_reviewers", [])}


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
    config: CoordinareConfiguration | None = None,
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
            # 151 review fix: attribute the merge to the HUMAN approver. The
            # reviewer performer's own APPROVE is also recorded on the PR (via
            # FakeGitHubServer._h_create_review), so taking the first APPROVED
            # review reported the bot as the approver.
            approved_by=next(
                (
                    r["author_login"]
                    for r in (pr["reviews"] if pr else [])
                    if r.get("state") == "APPROVED"
                    and str(r.get("author_login", "")).strip().lower() in _human_logins(fake)
                ),
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
                stage=d["stage"], role=d.get("role", ""), status=d["status"],
                model=d.get("model", ""), backend=d.get("backend", ""),
                terminal_marker=d.get("terminal_marker"),
                job_id=d.get("job_id"), session_id=d.get("session_id"),
                container_id=d.get("container_id"),
                tokens_processed=d.get("tokens_processed"),
                started_at=d["started_at"], finished_at=d["finished_at"],
                seconds=d.get("seconds"), poll_error=d.get("poll_error"),
            )
            for d in dispatch_log if d["card_id"] == card_id
        ]
        # Distinct stages in dispatch order (real mode records many; stub, one).
        reached_stages: list[str] = []
        for d in dispatches:
            if d.stage and d.stage not in reached_stages:
                reached_stages.append(d.stage)
        tokens = [d.tokens_processed for d in dispatches if d.tokens_processed is not None]
        card_tokens = sum(tokens) if tokens else None
        cards.append(CardOutcome(
            card_id=card_id,
            issue_ref=fake._cards.get(card_id, {}).get("issue_url", ""),
            title=fx.title,
            fixture_id=fx.id,
            final_state=final_state,  # type: ignore[arg-type]
            reached_stages=reached_stages,
            dispatches=dispatches,
            gate_decisions=gate_decisions,
            ci_results=ci_results,
            merge=merge,
            timing=Timing(
                first_dispatch_at=dispatches[0].started_at if dispatches else None,
                terminal_at=finished_at,
            ),
            cost=Cost(tokens_processed=card_tokens, cost_usd=estimate_cost_usd(card_tokens, cost_rate)),
        ))

    artifact = RunArtifact(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        wall_clock_seconds=(finished_at - started_at).total_seconds(),
        config_fingerprint=_config_fingerprint(config_path, config),
        approver_policy="gates_green",
        fixture_manifest=",".join(fx.id for fx in fixtures_by_card.values()),
        cards=cards,
    )
    artifact.totals = artifact.compute_totals()
    return artifact
