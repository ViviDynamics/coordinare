"""Spec 151 (US1, T031) — DETERMINISTIC (free, no model/Docker) proof of the
real-runner resilience guarantees the paid lane (T018) also asserts: on a failing
real dispatch OR an unreachable fake server, run_board(stub=False) still tears the
server down (FR-009) AND emits a schema-valid artifact with a terminal non-merge
outcome (FR-008/FR-010).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinare.bench.artifact import RunArtifact
from coordinare.bench.fixtures import tiny_fixture
from coordinare.bench.runner import _no_sleep, _real_mode_max_cycles, run_board
from coordinare.config import ProjectConfiguration


class _StubServer:
    """A fake FakeGitHubServer (no git daemon / no aiohttp) for injection."""

    def __init__(self, *, start_raises: bool = False) -> None:
        self.head_ref_index: dict[str, str] = {}
        self.started = False
        self.stopped = False
        self._start_raises = start_raises
        self.git_base_url = "git://127.0.0.1:9418"
        self.rest_base_url = "http://127.0.0.1:5599"
        self.graphql_url = "http://127.0.0.1:5599/graphql"
        # Container-facing views the runner injects into the config (bridge + host-gateway).
        self.performer_git_base_url = "git://host.docker.internal:9418"
        self.performer_rest_base_url = "http://host.docker.internal:5599"
        self.performer_graphql_url = "http://host.docker.internal:5599/graphql"

    async def start(self) -> None:
        if self._start_raises:
            raise RuntimeError("fake git/REST server never bound its port")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _RaisingService:
    """A performer service whose dispatch raises (simulates PR-create 5xx / crash)."""

    async def check_health(self) -> dict[str, Any]:
        return {"status": "ready"}

    async def dispatch_card(self, card_context: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("dispatch exploded (simulated PR-create 5xx)")

    async def check_status(self, _session_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {}


def _config() -> ProjectConfiguration:
    return ProjectConfiguration(
        github_org="bench-org", project_name="bench-repo",
        human_reviewers=["reviewer1"], github_token="fake-token",
        agent_transport="kubernetes",  # WorkspaceManager.prepare skips git clone
    )


_STAGES = [
    "assessing", "architecting", "implementing", "reviewing",
    "security", "qa", "documenting", "closing_review",
]


async def _assert_terminal_artifact(run_dir: Path, artifact: RunArtifact) -> None:
    assert (run_dir / "run.json").exists()
    reloaded = RunArtifact.load(run_dir / "run.json")  # re-validates the schema
    assert reloaded.totals.cards_total == 1
    card = reloaded.cards[0]
    assert card.final_state != "merged"  # terminal non-merge (FR-008)
    assert card.merge.merged is False


async def test_dispatch_failure_still_tears_down_and_emits_artifact(tmp_path: Path) -> None:
    server = _StubServer()
    services = {stage: _RaisingService() for stage in _STAGES}
    run_dir = tmp_path / "run"
    artifact = await run_board(
        [tiny_fixture()], run_dir, stub=False, max_cycles=2,
        real_config=_config(), server=server, performer_services=services,
        sleep_func=_no_sleep,  # keep the real-mode budget loop instant in tests
    )
    assert server.started is True
    assert server.stopped is True   # teardown-safe finally ran (FR-009)
    await _assert_terminal_artifact(run_dir, artifact)


def test_records_to_dispatch_log_joins_model_backend_tokens() -> None:
    """SC-002/T020: the record→dispatch join populates model/backend and matches
    best-effort tokens_processed to its dispatch by session, deduping a service
    shared across stages."""
    from coordinare.bench.recording_performer import RecordingPerformer
    from coordinare.bench.runner import _records_to_dispatch_log

    svc = RecordingPerformer(object())
    svc.dispatch_records.append({
        "stage": "implementing", "role": "implementer", "card_id": "PVTI_1",
        "model": "haiku", "backend": "opencode", "status": "succeeded",
        "session_id": "s1", "started_at": "2020-01-01T00:00:00+00:00",
    })
    svc.status_records.append({"session_id": "s1", "status": "succeeded", "tokens_processed": 1234})

    # Same service registered under two stages must yield ONE row (dedup by id).
    log = _records_to_dispatch_log({"implementing": svc, "reviewing": svc})
    assert len(log) == 1
    row = log[0]
    assert row["model"] == "haiku"
    assert row["backend"] == "opencode"
    assert row["tokens_processed"] == 1234
    assert row["card_id"] == "PVTI_1"
    assert row["status"] == "succeeded"


async def test_recorder_joins_terminal_marker_time_tokens_and_ids() -> None:
    """The artifact's per-dispatch seconds/tokens/status come from the TERMINAL
    check_status return (the parsed PerformerResponse), not a run-end stamp: a
    `working` poll records nothing, and a non-success marker must not read as
    succeeded."""
    from datetime import UTC, datetime, timedelta

    from coordinare.bench.recording_performer import RecordingPerformer
    from coordinare.bench.runner import _records_to_dispatch_log

    class _Svc:
        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            return {"status": "ok", "session_id": "s1", "job_id": "j1", "container_id": "c1"}

        async def check_status(self, _sid: str, **_kw: Any) -> dict[str, Any]:
            # working first (must NOT be recorded), then the terminal response.
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls == 1:
                return {"status": "working", "job_id": "j1"}
            return {"status": "changes_requested", "metrics": {"tokens_processed": 4242}}

    svc = RecordingPerformer(_Svc())
    await svc.dispatch_card({"stage": "closing_review", "role": "closer", "item_id": "PVTI_1"})
    await svc.check_status("s1")
    await svc.check_status("s1")
    assert len(svc.status_records) == 1  # the `working` poll is not terminal

    row = _records_to_dispatch_log({"closing_review": svc})[0]
    assert row["terminal_marker"] == "changes_requested"
    assert row["status"] == "failed"          # not silently "succeeded"
    assert row["tokens_processed"] == 4242
    assert (row["job_id"], row["session_id"], row["container_id"]) == ("j1", "s1", "c1")
    assert 0 <= row["seconds"] < 5            # real duration, not a run-end stamp
    assert row["finished_at"] - row["started_at"] < timedelta(seconds=5)
    assert datetime.now(UTC) >= row["finished_at"]


async def test_dispatch_without_terminal_status_is_not_reported_succeeded() -> None:
    """A dispatch the budget cut off (no terminal poll) must not be logged as a
    success — the old code coerced every accepted dispatch to `succeeded`."""
    from coordinare.bench.recording_performer import RecordingPerformer
    from coordinare.bench.runner import _records_to_dispatch_log

    class _Svc:
        async def dispatch_card(self, _ctx: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            return {"status": "ok", "session_id": "s9"}

    svc = RecordingPerformer(_Svc())
    await svc.dispatch_card({"stage": "implementing", "item_id": "PVTI_1"})
    row = _records_to_dispatch_log({"implementing": svc})[0]
    assert row["status"] == "cancelled"
    assert row["terminal_marker"] is None
    assert row["seconds"] is None


def test_real_mode_cycle_budget_derives_from_wall_clock() -> None:
    """The real-mode budget must give a card enough cycles to outlast real model
    latency: default 1200s / 5s poll = 240 cycles (the 9-cycle stub budget is what
    abandoned the assessing card mid-call)."""
    assert _real_mode_max_cycles(1200.0, 5.0) == 240
    assert _real_mode_max_cycles(12.0, 5.0) == 3   # ceil, not floor
    assert _real_mode_max_cycles(1.0, 5.0) == 1    # never zero


async def test_unreachable_server_fails_fast_and_tears_down(tmp_path: Path) -> None:
    server = _StubServer(start_raises=True)
    services = {stage: _RaisingService() for stage in _STAGES}
    run_dir = tmp_path / "run"
    artifact = await run_board(
        [tiny_fixture()], run_dir, stub=False, max_cycles=2,
        real_config=_config(), server=server, performer_services=services,
        sleep_func=_no_sleep,  # keep the real-mode budget loop instant in tests
    )
    # start() raised, but the server was created before the try — stop() still ran.
    assert server.stopped is True
    await _assert_terminal_artifact(run_dir, artifact)
