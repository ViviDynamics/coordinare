"""270: env bootstrap control.

Pausing gates the whole env-cache cycle; a hung bootstrap is reaped by the daemon
through its runtime at the idle and wall-clock limits; the example roles register
the bootstrap endpoint; an operator's manual trigger resets a tripped breaker.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from performer.backends.claude_code import ClaudeCodeBackend
from pydantic import SecretStr, ValidationError

import coordinare.daemon as daemon_mod
from coordinare.config import CoordinareConfiguration, SymphonyConfig
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.daemon import CoordinareDaemon
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.bootstrap_history import (
    BOOTSTRAP_CANCEL_COOLDOWN_S,
    AttemptStart,
    BootstrapHistory,
    attach_job,
    begin_attempt,
)
from coordinare.services.bootstrap_runs import (
    BootstrapRuntime,
    cancel_bootstrap,
    read_bootstrap_run,
)
from coordinare.services.bootstrap_status import symphony_view
from coordinare.services.env_bootstrap_control import BootstrapTools
from coordinare.services.env_cache import EnvCacheService
from tests.unit.test_060_env_cache import _make_daemon_for_env_cache
from tests.unit.test_dashboard import (
    _attach_env_cache,
    _make_mock_daemon,
    _make_mock_health,
    _make_mock_metrics,
)

TOKEN = "a-secret-operator-token-of-at-least-32-characters"
BASE = "http://127.0.0.1:8090"
SHA_OLD = "sha-old"
SHA_NEW = "sha-new"


POLL_SECONDS = 10
IDLE_SECONDS = 120
OPEN_TOOL_SECONDS = 2700
DEFAULT_IDLE_SECONDS = 1800
ANONYMOUS_TOOL_SECONDS = 1200
LONG_TOOL_SECONDS = 1500
STREAM_END_SECONDS = OPEN_TOOL_SECONDS + IDLE_SECONDS * 2
TOOL_CALL_ID = "call-install"


def _codex_delta(step: int) -> dict[str, Any]:
    return {
        "type": "tool_use", "text": f"installed package {step}", "detail": "shell",
        "is_delta": True, "stream_id": TOOL_CALL_ID,
        "timestamp": datetime.fromtimestamp(step * POLL_SECONDS, UTC).isoformat(),
    }


@pytest.mark.parametrize("start,result", [
    (
        {"type": "tool_use", "text": "Bash", "detail": "Bash", "tool": "Bash", "arg": "uv sync"},
        {"type": "tool_use", "text": "result: installed", "detail": TOOL_CALL_ID, "status": "ok"},
    ),
    (
        {"type": "tool_use", "text": "uv sync", "detail": "shell", "tool": "shell", "arg": "uv sync"},
        {"type": "tool_use", "text": "uv sync", "detail": "shell", "tool": "shell", "arg": "uv sync", "status": "ok"},
    ),
    (
        {"type": "tool_use", "text": "bash: uv sync", "detail": "bash", "call_id": TOOL_CALL_ID},
        {"type": "tool_use", "text": "bash → installed", "detail": "bash", "call_id": TOOL_CALL_ID, "status": "ok"},
    ),
], ids=["claude_code", "codex", "opencode"])
def test_backend_tool_start_and_result_pair(start, result) -> None:
    tools = BootstrapTools()
    assert not tools.observe([start], now=0)
    assert len(tools.open_since) == 1
    if start.get("detail") == "shell":
        tools.observe([_codex_delta(step) for step in range(IDLE_SECONDS // POLL_SECONDS)], now=POLL_SECONDS)
        assert len(tools.open_since) == 1
    assert tools.observe([result], now=IDLE_SECONDS)
    assert not tools.open_since
    assert not tools.observe([start, result], now=IDLE_SECONDS + POLL_SECONDS)
    assert not tools.open_since


def test_codex_deltas_without_a_start_do_not_open_tools() -> None:
    tools = BootstrapTools()
    assert tools.observe([_codex_delta(0)], now=0)
    assert not tools.open_since
    assert not tools.observe([_codex_delta(0)], now=POLL_SECONDS)
    assert tools.observe([_codex_delta(1)], now=POLL_SECONDS)
    assert not tools.open_since


def test_anonymous_tool_expires_even_when_its_start_is_replayed() -> None:
    tools = BootstrapTools()
    start = {"type": "tool_use", "text": "bash: uv sync", "detail": "bash"}
    tools.observe([start], now=0)
    tools.observe([], now=ANONYMOUS_TOOL_SECONDS - POLL_SECONDS)
    assert tools.open_since
    tools.observe([start], now=ANONYMOUS_TOOL_SECONDS)
    assert not tools.open_since


def _claude_code_start(*, keyed: bool = True) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_use", "name": "Bash", "input": {"command": "bundle install"}}
    if keyed:
        block["id"] = TOOL_CALL_ID
    backend = ClaudeCodeBackend()
    backend._handle_event({"type": "assistant", "message": {"content": [block]}})
    [start] = backend.drain_events()
    assert start.text == start.detail == "Bash"
    assert start.arg == "bundle install"
    return start.model_dump(mode="json")


def _claude_code_result() -> dict[str, Any]:
    backend = ClaudeCodeBackend()
    backend._handle_event({"type": "tool_result", "tool_use_id": TOOL_CALL_ID, "content": "installed"})
    [result] = backend.drain_events()
    assert result.text == "result: installed"
    assert result.detail == TOOL_CALL_ID
    return result.model_dump(mode="json")


def test_claude_code_keyed_tool_survives_25_minutes_and_closes_on_result() -> None:
    tools = BootstrapTools()
    start = _claude_code_start()
    assert start["call_id"] == TOOL_CALL_ID
    tools.observe([start], now=0)
    tools.observe([], now=LONG_TOOL_SECONDS)
    assert tools.open_since == {f"call:{TOOL_CALL_ID}": 0}

    result = _claude_code_result()
    assert result["call_id"] == TOOL_CALL_ID
    assert tools.observe([result], now=LONG_TOOL_SECONDS)
    assert not tools.open_since


def test_claude_code_unkeyed_tool_still_expires() -> None:
    tools = BootstrapTools()
    start = _claude_code_start(keyed=False)
    assert not start["call_id"]
    tools.observe([start], now=0)
    tools.observe([], now=ANONYMOUS_TOOL_SECONDS - POLL_SECONDS)
    assert tools.open_since
    tools.observe([start], now=ANONYMOUS_TOOL_SECONDS)
    assert not tools.open_since


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _symphony_cfg(*, enabled: bool = True, performer_id: str | None = "bp-1") -> MagicMock:
    cfg = MagicMock()
    cfg.name = "alpha"
    cfg.enabled = enabled
    cfg.env_bootstrap_performer_id = performer_id
    cfg.env_spec_files = ["README.md"]
    cfg.test_env = None
    cfg.effective_config = MagicMock(return_value=MagicMock(github_org="org", project_name="repo"))
    return cfg


def _cache_state(tmp_path: Any, **fields: Any) -> EnvCacheState:
    values: dict[str, Any] = {
        "symphony_name": "alpha",
        "sanitised_name": "alpha-abc",
        "cache_dir": tmp_path / "alpha",
        "readme_sha": SHA_OLD,
    }
    values.update(fields)
    return EnvCacheState(**values)


def _cycle_daemon(monkeypatch: pytest.MonkeyPatch, cache: EnvCacheState, env_svc: Any) -> CoordinareDaemon:
    """A daemon whose env-cache cycle runs over one symphony, with the rest stubbed."""
    monkeypatch.setattr(CoordinareDaemon, "_get_manifest_llm_chat", lambda _self: None)
    daemon = _make_daemon_for_env_cache()
    monkeypatch.setattr(daemon, "_maybe_dispatch_intake", AsyncMock())
    daemon._state["env_cache_service"] = env_svc
    daemon._state["env_cache"] = {"alpha": cache}
    daemon._state["symphony_github_services"] = {"alpha": MagicMock()}
    daemon._state["performer_services_by_id"] = {"bp-1": MagicMock()}
    return daemon


def _real_env_service(monkeypatch: pytest.MonkeyPatch, dispatch: AsyncMock) -> EnvCacheService:
    """The real check_and_trigger, with the spec SHA and the dispatch stubbed."""
    svc = EnvCacheService(MagicMock(global_config=MagicMock()))
    monkeypatch.setattr(EnvCacheService, "_current_manifest_sha", AsyncMock(return_value=SHA_NEW))
    monkeypatch.setattr(EnvCacheService, "_do_dispatch", dispatch)
    return svc


# ---------------------------------------------------------------------------
# 1. Pausing gates the env-cache cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paused_symphony_gets_no_bootstrap_at_boot(tmp_path, monkeypatch) -> None:
    cache = _cache_state(tmp_path, readme_sha=None)
    env_svc = MagicMock(check_and_trigger=AsyncMock())
    daemon = _cycle_daemon(monkeypatch, cache, env_svc)
    before = cache.model_dump()

    await daemon._run_env_cache_and_cardless_cycle({"alpha": _symphony_cfg(enabled=False)})

    env_svc.check_and_trigger.assert_not_awaited()
    assert cache.model_dump() == before  # persisted env-cache state untouched


@pytest.mark.asyncio
async def test_paused_symphony_gets_no_retry_and_state_is_untouched(tmp_path, monkeypatch) -> None:
    """A failed bootstrap whose cooldown has elapsed would be retried. While paused it is not."""
    cache = _cache_state(
        tmp_path,
        last_bootstrap_succeeded=False,
        last_bootstrap_error="earlier failure",
        bootstrap_attempts=1,
        last_bootstrap_at=datetime.now(UTC) - timedelta(days=1),
    )
    env_svc = MagicMock(check_and_trigger=AsyncMock())
    daemon = _cycle_daemon(monkeypatch, cache, env_svc)
    before = cache.model_dump()

    await daemon._run_env_cache_and_cardless_cycle({"alpha": _symphony_cfg(enabled=False)})

    env_svc.check_and_trigger.assert_not_awaited()
    assert cache.model_dump() == before


@pytest.mark.asyncio
async def test_resume_picks_up_the_retry_that_was_due(tmp_path, monkeypatch) -> None:
    """On resume the real cycle sees the same state and dispatches once, against the current SHA."""
    dispatch = AsyncMock()
    svc = _real_env_service(monkeypatch, dispatch)
    cache = _cache_state(tmp_path, last_bootstrap_succeeded=True)
    daemon = _cycle_daemon(monkeypatch, cache, svc)
    paused = {"alpha": _symphony_cfg(enabled=False)}
    before = cache.model_dump()

    await daemon._run_env_cache_and_cardless_cycle(paused)
    dispatch.assert_not_awaited()
    assert cache.model_dump() == before

    await daemon._run_env_cache_and_cardless_cycle({"alpha": _symphony_cfg(enabled=True)})
    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["current_sha"] == SHA_NEW
    assert cache.last_seen_spec_sha == SHA_NEW


@pytest.mark.asyncio
async def test_enabled_symphony_still_runs_its_cycle(tmp_path, monkeypatch) -> None:
    env_svc = MagicMock(check_and_trigger=AsyncMock())
    daemon = _cycle_daemon(monkeypatch, _cache_state(tmp_path), env_svc)

    await daemon._run_env_cache_and_cardless_cycle({"alpha": _symphony_cfg(enabled=True)})

    env_svc.check_and_trigger.assert_awaited_once()
    assert env_svc.check_and_trigger.await_args.kwargs["symphony_name"] == "alpha"


# ---------------------------------------------------------------------------
# 2. A hung bootstrap is reaped by the daemon, through the runtime
# ---------------------------------------------------------------------------


def _poll_daemon(monkeypatch: pytest.MonkeyPatch, *, wall_clock_s: int, idle_s: int) -> CoordinareDaemon:
    clock = _Clock()
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", clock.sleep)
    monkeypatch.setattr(daemon_mod, "monotonic", clock.time)
    monkeypatch.setattr(CoordinareDaemon, "_verify_bootstrap_in_clean_context", AsyncMock(return_value=(True, None, False)))
    monkeypatch.setattr(CoordinareDaemon, "_quiesce_bootstrap_container", AsyncMock(return_value=True))
    daemon = _make_daemon_for_env_cache()
    cfg = MagicMock()
    cfg.bootstrap_max_seconds = wall_clock_s
    cfg.bootstrap_idle_timeout_seconds = idle_s
    daemon._state["coordinare_config"] = cfg
    daemon._state["performer_services"] = {}
    return daemon


def _svc(statuses: Any) -> MagicMock:
    """A fake performer service. check_status follows ``statuses`` (a list is one
    status per poll, a dict is the same status every poll). stop_session_confirmed
    stops the job and confirms it, the way a runtime would."""
    svc = MagicMock()
    if isinstance(statuses, list):
        svc.check_status = AsyncMock(side_effect=statuses)
    else:
        svc.check_status = AsyncMock(return_value=statuses)
    svc.stop_session_confirmed = AsyncMock(return_value=True)
    return svc


def _ec_svc() -> MagicMock:
    return MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))


@pytest.mark.asyncio
async def test_job_that_never_progresses_is_reaped_at_the_idle_limit(monkeypatch) -> None:
    """A persistent-performer job has no container handle. It is still reaped, by session id."""
    daemon = _poll_daemon(monkeypatch, wall_clock_s=3600, idle_s=120)
    svc = _svc({"status": "working", "events": []})
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-hung", "alpha", ec_svc)

    # Progress is first seen at poll 0; reap after 120 seconds and a final read.
    assert svc.check_status.await_count == 14
    svc.stop_session_confirmed.assert_awaited_once_with("job-hung")
    ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)
    assert ec_svc.on_bootstrap_complete.call_args.kwargs["error"] == (
        "bootstrap hung: no progress for 2 minutes; job stopped"
    )


@pytest.mark.asyncio
async def test_progressing_job_past_the_wall_clock_is_reaped(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=120, idle_s=600)
    statuses = [
        {"status": "working", "events": [{"text": f"install step {n} finished. "}]}
        for n in range(20)
    ]
    svc = _svc(statuses)
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-slow", "alpha", ec_svc)

    assert svc.check_status.await_count == 12  # eleven regular polls and one final read
    svc.stop_session_confirmed.assert_awaited_once_with("job-slow")
    error = ec_svc.on_bootstrap_complete.call_args.kwargs["error"]
    assert error == "bootstrap failed: exceeded 2 minutes of wall clock; job stopped"
    ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)


@pytest.mark.asyncio
async def test_status_call_that_hangs_cannot_hold_the_job_past_the_wall_clock(monkeypatch) -> None:
    """The performer does not answer at all. The daemon's deadline still ends the job."""
    daemon = _poll_daemon(monkeypatch, wall_clock_s=1, idle_s=0)

    async def _never_answers(_job_id: str) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {}

    svc = _svc({"status": "working"})
    svc.check_status = AsyncMock(side_effect=_never_answers)
    ec_svc = _ec_svc()

    await asyncio.wait_for(daemon._poll_bootstrap_completion(svc, "job-mute", "alpha", ec_svc), timeout=10)

    svc.stop_session_confirmed.assert_awaited_once_with("job-mute")
    ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)


@pytest.mark.asyncio
async def test_progressing_job_inside_the_limits_is_untouched(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=3600, idle_s=120)
    statuses = [
        {"status": "working", "events": [{"text": f"step {n} done. "}]} for n in range(3)
    ] + [{"status": "env_bootstrap_complete"}]
    svc = _svc(statuses)
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-ok", "alpha", ec_svc)

    svc.stop_session_confirmed.assert_not_awaited()
    ec_svc.on_bootstrap_complete.assert_called_once()
    assert ec_svc.on_bootstrap_complete.call_args.args[1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["idle", "wall"])
@pytest.mark.parametrize("stop_error", [False, True])
async def test_unconfirmed_reap_holds_dispatch_until_next_poll(monkeypatch, tmp_path, limit, stop_error) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=IDLE_SECONDS, idle_s=POLL_SECONDS)
    state = daemon.state
    state["bootstrap_history"] = BootstrapHistory()
    cache = _cache_state(tmp_path, bootstrap_in_flight=True)
    state["env_cache"] = {"alpha": cache}
    begin_attempt(state, "alpha", AttemptStart(SHA_OLD, datetime.now(UTC), 1, "codex", "model", "perf"))
    attach_job(state, "alpha", job_id="job-x", pod=None)
    ec_svc = _ec_svc()
    svc = _svc({"status": "working", "events": []})
    stop_results = [RuntimeError("unreachable") if stop_error else False, True]
    svc.stop_session_confirmed = AsyncMock(side_effect=stop_results)
    if limit == "wall":
        assert not await daemon._bootstrap_budget_exhausted(svc, "job-x", "alpha", ec_svc, IDLE_SECONDS)
    else:
        poll = daemon_mod._BootstrapPollState(last_progress_at=-IDLE_SECONDS, last_progress="unchanged")
        assert not await daemon._bootstrap_idle_reap(
            svc, poll, {"status": "working", "events": []}, "job-x", "alpha", ec_svc, POLL_SECONDS,
        )
    ec_svc.on_bootstrap_complete.assert_not_called()
    assert cache.bootstrap_in_flight
    assert cache.bootstrap_attempts == 0
    detail = await read_bootstrap_run(state, "alpha", state["bootstrap_history"].open_attempt("alpha").run_id)
    assert detail["state"] == "stop_unconfirmed"
    assert symphony_view(state, "alpha", configured=True, now=datetime.now(UTC))["state"] == "stop_unconfirmed"
    await daemon._bootstrap_budget_exhausted(svc, "job-x", "alpha", ec_svc, IDLE_SECONDS)
    ec_svc.on_bootstrap_complete.assert_called_once()


@pytest.mark.asyncio
async def test_unconfirmed_cancel_is_resolved_by_terminal_poll(monkeypatch, tmp_path) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=IDLE_SECONDS, idle_s=POLL_SECONDS)
    state = daemon.state
    state["bootstrap_history"] = BootstrapHistory()
    state["env_cache"] = {"alpha": _cache_state(tmp_path, bootstrap_in_flight=True)}
    begin_attempt(state, "alpha", AttemptStart(SHA_OLD, datetime.now(UTC), 1, "codex", "model", "perf"))
    attach_job(state, "alpha", job_id="job-x", pod=None)
    ec_svc = _ec_svc()
    state["env_cache_service"] = ec_svc
    svc = _svc({"status": "failed"})
    svc.stop_session_confirmed = AsyncMock(return_value=False)
    task = asyncio.create_task(asyncio.Event().wait())
    state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(svc, "job-x", task)}
    try:
        assert (await cancel_bootstrap(state, "alpha", "Ada"))["status"] == "stop_unconfirmed"
        await daemon._poll_bootstrap_completion(svc, "job-x", "alpha", ec_svc)
        ec_svc.on_bootstrap_complete.assert_not_called()
        ec_svc.on_bootstrap_cancelled.assert_called_once_with("alpha", state, "Ada")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# 3. Config: the bootstrap endpoint must carry the literal env_bootstrap role
# ---------------------------------------------------------------------------


def _raw_with_roles(roles: list[str]) -> dict[str, Any]:
    return {
        "github_org": "myorg",
        "github_token": "ghp_test",
        "human_reviewers": ["reviewer"],
        "github_project_number": 1,
        "symphonies": [
            {"name": "my-symphony", "github_project_number": 1, "env_bootstrap_performer_id": "bootstrap-env"},
        ],
        "performer_endpoints": [
            {
                "id": "bootstrap-env",
                "mode": "ephemeral",
                "roles": roles,
                "image": "coordinare-performer:full",
                "endpoint": "http://localhost:8080",
            },
        ],
        "orchestra": {"mode": "shared_pool"},
    }


def test_bootstrap_endpoint_without_any_roles_is_rejected_with_the_fix() -> None:
    raw = coerce_multi_symphony_raw(_raw_with_roles([]))
    with pytest.raises(ValidationError, match="roles: \\[env_bootstrap\\]"):
        CoordinareConfiguration(**raw)


def test_bootstrap_endpoint_with_the_literal_role_loads() -> None:
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(_raw_with_roles(["env_bootstrap"])))
    assert cfg.symphonies[0].env_bootstrap_performer_id == "bootstrap-env"


def test_bootstrap_endpoint_with_other_roles_loads_with_a_warning() -> None:
    """Dispatch by id still works through the other role, so this warns rather than refuses."""
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(_raw_with_roles(["implementer"])))
    assert cfg.symphonies[0].env_bootstrap_performer_id == "bootstrap-env"


def test_example_config_registers_the_bootstrap_endpoint_with_the_literal_role() -> None:
    text = (Path(__file__).resolve().parents[2] / "config.example.yaml").read_text()
    assert "roles: [env_bootstrap]" in text
    assert "roles: []                          # env_bootstrap" not in text


# ---------------------------------------------------------------------------
# 4. The manual trigger resets a tripped breaker, respects pause, follows auth
# ---------------------------------------------------------------------------


def _trigger_daemon(*, enabled: bool = True, in_flight: bool = False) -> MagicMock:
    daemon = _make_mock_daemon()
    daemon.state["symphony_configs"] = {
        "alpha": SymphonyConfig(name="alpha", github_project_number=10, enabled=enabled),
    }
    _attach_env_cache(daemon, "alpha", in_flight=in_flight)
    return daemon


def _tripped(daemon: MagicMock) -> EnvCacheState:
    cache = daemon.state["env_cache"]["alpha"]
    cache.bootstrap_exhausted = True
    cache.bootstrap_attempts = 3
    cache.infra_failures = 2
    cache.last_bootstrap_succeeded = False
    cache.last_bootstrap_error = "bootstrap failed: exit 1"
    return cache


def _client(daemon: MagicMock, store: DashboardStore, *, token: str | None = None) -> TestClient:
    app = create_dashboard_app(
        store, daemon, _make_mock_metrics(), _make_mock_health(),
        auth_token=SecretStr(token) if token is not None else None,
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE)


def test_manual_trigger_resets_a_tripped_breaker_and_records_who_did_it() -> None:
    daemon = _trigger_daemon()
    cache = _tripped(daemon)
    store = DashboardStore()
    client = _client(daemon, store)

    res = client.post("/api/symphonies/alpha/env-bootstrap")

    assert res.status_code == 202
    assert cache.bootstrap_exhausted is False
    assert cache.bootstrap_attempts == 0
    assert cache.infra_failures == 0
    assert cache.readme_sha is None
    daemon._webhook_trigger.set.assert_called_once()
    texts = [entry["text"] for entry in store.activity_log.snapshot()]
    assert any(
        "Env bootstrap for 'alpha' forced by local: retry budget reset "
        "(was 3 attempts, breaker tripped)." in text
        for text in texts
    )


@pytest.mark.asyncio
async def test_after_a_manual_trigger_the_next_cycle_dispatches_with_attempts_reset(monkeypatch) -> None:
    daemon = _trigger_daemon()
    cache = _tripped(daemon)
    client = _client(daemon, DashboardStore())
    assert client.post("/api/symphonies/alpha/env-bootstrap").status_code == 202

    dispatch = AsyncMock()
    svc = _real_env_service(monkeypatch, dispatch)
    sym_cfg = _symphony_cfg()
    state: dict[str, Any] = {"env_cache": {"alpha": cache}, "config": MagicMock()}
    await svc.check_and_trigger("alpha", sym_cfg, MagicMock(), state, MagicMock())

    dispatch.assert_awaited_once()
    assert dispatch.await_args.kwargs["current_sha"] == SHA_NEW
    assert cache.bootstrap_attempts == 0
    assert cache.bootstrap_exhausted is False


def test_manual_trigger_is_refused_while_paused_and_changes_nothing() -> None:
    daemon = _trigger_daemon(enabled=False)
    cache = _tripped(daemon)
    before = cache.model_dump()
    client = _client(daemon, DashboardStore())

    res = client.post("/api/symphonies/alpha/env-bootstrap")

    assert res.status_code == 409
    assert res.json()["status"] == "symphony_paused"
    assert "paused" in res.json()["error"]
    assert cache.model_dump() == before
    daemon._webhook_trigger.set.assert_not_called()


def test_manual_trigger_while_in_flight_is_still_refused() -> None:
    daemon = _trigger_daemon(in_flight=True)
    client = _client(daemon, DashboardStore())
    res = client.post("/api/symphonies/alpha/env-bootstrap")
    assert res.status_code == 409
    assert res.json()["status"] == "bootstrap_in_flight"


def test_manual_trigger_requires_authentication_like_every_operator_action() -> None:
    daemon = _trigger_daemon()
    cache = _tripped(daemon)
    store = DashboardStore()
    client = _client(daemon, store, token=TOKEN)

    refused = client.post("/api/symphonies/alpha/env-bootstrap")
    assert refused.status_code == 401
    assert cache.bootstrap_exhausted is True  # nothing reset without authentication
    assert cache.readme_sha == "cafef00d"
    daemon._webhook_trigger.set.assert_not_called()

    accepted = client.post(
        "/api/symphonies/alpha/env-bootstrap", headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert accepted.status_code == 202
    assert cache.bootstrap_exhausted is False
    texts = [entry["text"] for entry in store.activity_log.snapshot()]
    assert any("forced by token" in text for text in texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["env_bootstrap_complete", "failed"])
@pytest.mark.parametrize("limit", ["idle", "wall"])
async def test_completion_between_limit_and_stop_keeps_real_result(monkeypatch, terminal, limit) -> None:
    wall = 20 if limit == "wall" else 3600
    idle = 20 if limit == "idle" else 0
    daemon = _poll_daemon(monkeypatch, wall_clock_s=wall, idle_s=idle)
    polls = 1 if limit == "wall" else 3
    svc = _svc([{"status": "working", "events": []}] * polls + [{"status": terminal}])
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-race", "alpha", ec_svc)

    svc.stop_session_confirmed.assert_not_awaited()
    ec_svc.on_bootstrap_complete.assert_called_once()
    assert ec_svc.on_bootstrap_complete.call_args.args[1] is (terminal == "env_bootstrap_complete")
    assert "job stopped" not in str(ec_svc.on_bootstrap_complete.call_args)


@pytest.mark.asyncio
async def test_open_tool_survives_idle_silence_then_completes(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=3600, idle_s=IDLE_SECONDS)
    silent_polls = IDLE_SECONDS // POLL_SECONDS + 2
    svc = _svc(
        [{"status": "working", "events": [{"type": "tool_use", "id": "install", "text": "bundle install"}]}]
        + [{"status": "working", "events": []}] * silent_polls
        + [{"status": "env_bootstrap_complete"}],
    )
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-install", "alpha", ec_svc)

    svc.stop_session_confirmed.assert_not_awaited()
    assert ec_svc.on_bootstrap_complete.call_args.args[1] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "silent"])
async def test_claude_code_long_tool_survives_until_result_then_restarts_idle(monkeypatch, finish) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=OPEN_TOOL_SECONDS, idle_s=IDLE_SECONDS)
    statuses = [{"status": "working", "events": [_claude_code_start()]}]
    statuses.extend([{"status": "working", "events": []}] * (LONG_TOOL_SECONDS // POLL_SECONDS - 1))
    statuses.append({"status": "working", "events": [_claude_code_result()]})
    if finish == "complete":
        statuses.append({"status": "env_bootstrap_complete"})
    else:
        statuses.extend([{"status": "working", "events": []}] * (IDLE_SECONDS // POLL_SECONDS + 1))
    svc = _svc(statuses)
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-claude-code-install", "alpha", ec_svc)

    if finish == "complete":
        svc.stop_session_confirmed.assert_not_awaited()
        assert ec_svc.on_bootstrap_complete.call_args.args[1] is True
    else:
        svc.stop_session_confirmed.assert_awaited_once()
        assert daemon_mod.monotonic() == LONG_TOOL_SECONDS + POLL_SECONDS + IDLE_SECONDS
        assert "no progress" in ec_svc.on_bootstrap_complete.call_args.kwargs["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wall", [OPEN_TOOL_SECONDS // 2, OPEN_TOOL_SECONDS * 2])
async def test_open_tool_is_bounded_by_tool_limit_and_wall_clock(monkeypatch, wall) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=wall, idle_s=IDLE_SECONDS)
    svc = _svc({"status": "working", "events": [{"type": "tool_use", "id": "install", "text": "bundle install"}]})
    ec_svc = _ec_svc()
    stop_times = []

    async def stop(_job):
        stop_times.append(daemon_mod.monotonic())
        return True

    svc.stop_session_confirmed = AsyncMock(side_effect=stop)
    await daemon._poll_bootstrap_completion(svc, "job-install", "alpha", ec_svc)

    assert stop_times == [min(wall, OPEN_TOOL_SECONDS + POLL_SECONDS)]
    assert ec_svc.on_bootstrap_complete.call_args.args[1] is False
    error = ec_svc.on_bootstrap_complete.call_args.kwargs["error"]
    assert ("wall clock" if wall < OPEN_TOOL_SECONDS else "tool") in error


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["complete", "silent", "wall"])
async def test_streaming_tool_survives_open_limit_until_silent_or_wall(monkeypatch, finish) -> None:
    wall = STREAM_END_SECONDS + IDLE_SECONDS * 2
    daemon = _poll_daemon(monkeypatch, wall_clock_s=wall, idle_s=IDLE_SECONDS)
    start = {"type": "tool_use", "text": "bash: uv sync", "detail": "bash", "call_id": TOOL_CALL_ID}
    statuses = [{"status": "working", "events": [start]}]
    stream_end = wall if finish == "wall" else STREAM_END_SECONDS
    statuses.extend(
        {"status": "working", "events": [{**_codex_delta(step), "text": "."}]}
        for step in range(1, stream_end // POLL_SECONDS)
    )
    if finish == "complete":
        statuses.append({"status": "env_bootstrap_complete"})
    else:
        statuses.extend([{"status": "working", "events": []}] * (wall // POLL_SECONDS))
    svc = _svc(statuses)
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-streaming", "alpha", ec_svc)

    if finish == "complete":
        svc.stop_session_confirmed.assert_not_awaited()
        assert ec_svc.on_bootstrap_complete.call_args.args[1] is True
    else:
        svc.stop_session_confirmed.assert_awaited_once()
        expected_stop = wall if finish == "wall" else STREAM_END_SECONDS + IDLE_SECONDS
        assert daemon_mod.monotonic() == expected_stop
        error = ec_svc.on_bootstrap_complete.call_args.kwargs["error"]
        assert ("wall clock" if finish == "wall" else "tool open") in error


@pytest.mark.asyncio
async def test_codex_deltas_refresh_idle_without_opening_tools(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=OPEN_TOOL_SECONDS, idle_s=IDLE_SECONDS)
    stream_seconds = IDLE_SECONDS * 2
    svc = _svc([
        {"status": "working", "events": [{**_codex_delta(step), "text": "."}]}
        for step in range(stream_seconds // POLL_SECONDS)
    ] + [{"status": "working", "events": []}] * (IDLE_SECONDS // POLL_SECONDS + POLL_SECONDS))
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-deltas", "alpha", ec_svc)

    assert daemon_mod.monotonic() == stream_seconds + IDLE_SECONDS
    assert "no progress" in ec_svc.on_bootstrap_complete.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_anonymous_silent_tool_is_reaped_after_expiry(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=OPEN_TOOL_SECONDS * 2, idle_s=IDLE_SECONDS)
    start = {"type": "tool_use", "text": "bash: uv sync", "detail": "bash"}
    svc = _svc({"status": "working", "events": [start]})
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-anonymous", "alpha", ec_svc)

    assert daemon_mod.monotonic() == ANONYMOUS_TOOL_SECONDS + POLL_SECONDS
    svc.stop_session_confirmed.assert_awaited_once()
    assert "no progress" in ec_svc.on_bootstrap_complete.call_args.kwargs["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [
    {"type": "tool_result", "tool_use_id": "install", "text": "done"},
    {"type": "tool_use", "detail": "install", "status": "ok", "text": "result: done"},
])
async def test_tool_result_restarts_idle_timer(monkeypatch, result) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=3600, idle_s=IDLE_SECONDS)
    start = {"type": "tool_use", "id": "install", "text": "bundle install"}
    svc = _svc([{"status": "working", "events": [start]}] + [
        {"status": "working", "events": [start, result]},
    ] * (IDLE_SECONDS // POLL_SECONDS + 2))
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-done-tool", "alpha", ec_svc)

    assert daemon_mod.monotonic() == IDLE_SECONDS + 2 * POLL_SECONDS
    svc.stop_session_confirmed.assert_awaited_once()
    assert "no progress" in ec_svc.on_bootstrap_complete.call_args.kwargs["error"]


@pytest.mark.asyncio
async def test_idle_uses_elapsed_time_including_slow_status_reads(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=3600, idle_s=IDLE_SECONDS)
    clock = _Clock()
    monkeypatch.setattr(daemon_mod, "monotonic", clock.time)
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", clock.sleep)

    async def slow_status(_job):
        clock.now += IDLE_SECONDS
        return {"status": "working", "events": []}

    svc = _svc({})
    svc.check_status = AsyncMock(side_effect=slow_status)
    await daemon._poll_bootstrap_completion(svc, "job-slow-poll", "alpha", _ec_svc())

    assert svc.check_status.await_count == 3
    svc.stop_session_confirmed.assert_awaited_once()


def test_default_idle_limit_allows_install_gaps() -> None:
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(_raw_with_roles(["env_bootstrap"])))
    assert cfg.global_config.bootstrap_idle_timeout_seconds == DEFAULT_IDLE_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("alpha", "disabled"), ("disabled", "alpha")])
async def test_intake_and_wiki_run_only_for_enabled_symphonies(tmp_path, monkeypatch, names) -> None:
    env_svc = MagicMock(check_and_trigger=AsyncMock())
    daemon = _cycle_daemon(monkeypatch, _cache_state(tmp_path), env_svc)
    gh = daemon._state["symphony_github_services"]["alpha"]
    daemon._state["symphony_github_services"]["disabled"] = MagicMock()
    daemon._wiki_init_requests = {"alpha", "disabled"}
    wiki = AsyncMock()
    monkeypatch.setattr(daemon, "_execute_wiki_init_dispatch", wiki)
    configs = {name: _symphony_cfg(enabled=name == "alpha") for name in names}

    await daemon._run_env_cache_and_cardless_cycle(configs)

    intake = daemon._maybe_dispatch_intake
    assert intake.await_count == 2
    intake.assert_any_await("advocate", "alpha", gh)
    intake.assert_any_await("curator", "alpha", gh)
    wiki.assert_awaited_once_with("alpha", gh)
    assert daemon._wiki_init_requests == {"disabled"}


def test_manual_reset_cooldown_refuses_without_any_record_or_mutation() -> None:
    daemon = _trigger_daemon()
    store = DashboardStore()
    client = _client(daemon, store)
    assert client.post("/api/symphonies/alpha/env-bootstrap").status_code == 202
    cache = _tripped(daemon)
    cache.readme_sha = SHA_OLD
    before = cache.model_dump()
    entries = store.activity_log.snapshot()
    daemon._webhook_trigger.set.reset_mock()

    response = client.post("/api/symphonies/alpha/env-bootstrap")

    assert response.status_code == 429
    assert response.json()["status"] == "bootstrap_reset_cooldown"
    assert "next reset is allowed at" in response.json()["error"]
    assert response.json()["next_reset_at"] in response.json()["error"]
    assert cache.model_dump() == before
    assert store.activity_log.snapshot() == entries
    daemon._webhook_trigger.set.assert_not_called()


def test_manual_reset_cooldown_expires_and_is_per_symphony(monkeypatch) -> None:
    now = datetime.now(UTC)
    monkeypatch.setattr("coordinare.services.env_bootstrap_control._utcnow", lambda: now)
    daemon = _trigger_daemon()
    daemon.state["symphony_configs"]["beta"] = SymphonyConfig(name="beta", github_project_number=11)
    alpha = daemon.state["env_cache"]["alpha"]
    _attach_env_cache(daemon, "beta")
    daemon.state["env_cache"]["alpha"] = alpha
    client = _client(daemon, DashboardStore())
    assert client.post("/api/symphonies/alpha/env-bootstrap").status_code == 202
    assert client.post("/api/symphonies/beta/env-bootstrap").status_code == 202
    now += timedelta(minutes=15) - timedelta(microseconds=1)
    assert client.post("/api/symphonies/alpha/env-bootstrap").status_code == 429
    now += timedelta(microseconds=1)
    assert client.post("/api/symphonies/alpha/env-bootstrap").status_code == 202


@pytest.mark.asyncio
async def test_poll_uses_limits_from_real_root_config(monkeypatch) -> None:
    wall_seconds = 20
    daemon = _poll_daemon(monkeypatch, wall_clock_s=wall_seconds, idle_s=0)
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(_raw_with_roles(["env_bootstrap"])))
    cfg.global_config.bootstrap_max_seconds = wall_seconds
    cfg.global_config.bootstrap_idle_timeout_seconds = 0
    daemon._state["coordinare_config"] = cfg
    svc = _svc({"status": "working", "events": []})

    await daemon._poll_bootstrap_completion(svc, "job-root-config", "alpha", _ec_svc())

    assert daemon_mod.monotonic() == wall_seconds
    svc.stop_session_confirmed.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["idle", "wall"])
async def test_poller_retries_unconfirmed_stop_before_completing(monkeypatch, limit) -> None:
    wall = IDLE_SECONDS if limit == "idle" else POLL_SECONDS
    idle = POLL_SECONDS if limit == "idle" else 0
    daemon = _poll_daemon(monkeypatch, wall_clock_s=wall, idle_s=idle)
    svc = _svc({"status": "working", "events": []})
    ec_svc = _ec_svc()
    stops = []

    async def stop(job_id: str) -> bool:
        ec_svc.on_bootstrap_complete.assert_not_called()
        stops.append(job_id)
        return len(stops) > 1

    svc.stop_session_confirmed = AsyncMock(side_effect=stop)
    await daemon._poll_bootstrap_completion(svc, "job-retry", "alpha", ec_svc)
    assert stops == ["job-retry", "job-retry"]
    ec_svc.on_bootstrap_complete.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_sha", [False, True])
async def test_cancel_cooldown_then_dispatch_without_spending_budget(monkeypatch, tmp_path, changed_sha) -> None:
    dispatch = AsyncMock()
    service = _real_env_service(monkeypatch, dispatch)
    cache = _cache_state(tmp_path, readme_sha=SHA_OLD if changed_sha else SHA_NEW, bootstrap_attempts=1)
    state: dict[str, Any] = {"env_cache": {"alpha": cache}, "bootstrap_history": BootstrapHistory(),
                             "config": MagicMock()}
    begin_attempt(state, "alpha", AttemptStart(SHA_NEW, datetime.now(UTC), 1, "codex", "model", "perf"))
    service.on_bootstrap_cancelled("alpha", state, "Ada")
    await service.check_and_trigger("alpha", _symphony_cfg(), MagicMock(), state, dispatch)
    dispatch.assert_not_awaited()
    record = state["bootstrap_history"].recent("alpha", 1)[0]
    record.ended_at -= timedelta(seconds=BOOTSTRAP_CANCEL_COOLDOWN_S) + timedelta.resolution
    cache.last_bootstrap_at = record.ended_at
    await service.check_and_trigger("alpha", _symphony_cfg(), MagicMock(), state, dispatch)
    dispatch.assert_awaited_once()
    assert cache.bootstrap_attempts == (0 if changed_sha else 1)
