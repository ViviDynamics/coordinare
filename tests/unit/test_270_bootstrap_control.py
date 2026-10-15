"""270: env bootstrap control.

Pausing gates the whole env-cache cycle; a hung bootstrap is reaped by the daemon
through its runtime at the idle and wall-clock limits; the example roles register
the bootstrap endpoint; an operator's manual trigger resets a tripped breaker.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

import coordinare.daemon as daemon_mod
from coordinare.config import CoordinareConfiguration, SymphonyConfig
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.daemon import CoordinareDaemon
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted
from coordinare.models.env_cache import EnvCacheState
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


async def _no_sleep(_: float) -> None:
    return None


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
    monkeypatch.setattr(daemon_mod.asyncio, "sleep", _no_sleep)
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

    # Progress is first seen at poll 0; no change for 120 s reaps at poll 12.
    assert svc.check_status.await_count == 13
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

    assert svc.check_status.await_count == 12  # 120 s at 10 s a poll, no more
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
async def test_failed_stop_is_still_recorded_as_a_failed_attempt(monkeypatch) -> None:
    daemon = _poll_daemon(monkeypatch, wall_clock_s=3600, idle_s=30)
    svc = _svc({"status": "working", "events": []})
    svc.stop_session_confirmed = AsyncMock(side_effect=RuntimeError("runtime unreachable"))
    ec_svc = _ec_svc()

    await daemon._poll_bootstrap_completion(svc, "job-x", "alpha", ec_svc)

    ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)


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
    from pathlib import Path

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
