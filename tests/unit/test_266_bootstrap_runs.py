"""Bootstrap run persistence, cancellation and scoped detail reads."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.activity_log import ActivityLog
from coordinare.services.bootstrap_history import (
    RUN_SNAPSHOT_KIND,
    AttemptEnd,
    AttemptStart,
    BootstrapHistory,
    BudgetNow,
    attach_job,
    begin_attempt,
    persist_attempt,
    report_completion,
)
from coordinare.services.bootstrap_runs import (
    BootstrapRuntime,
    cancel_bootstrap,
    load_bootstrap_history,
    read_bootstrap_run,
    record_bootstrap_events,
    record_verification,
    release_bootstrap_runtime,
)
from coordinare.services.bootstrap_status import hold_text, symphony_view
from coordinare.services.env_cache import BOOTSTRAP_RETRY_COOLDOWN_S, EnvCacheService
from coordinare.services.event_capture import install_event_store
from coordinare.services.event_store import EventStore
from coordinare.services.event_store_query import EventFilters
from tests.unit.test_060_env_cache import _make_daemon_for_env_cache

NOW = datetime(2026, 10, 15, tzinfo=UTC)
HISTORY_BOUND = 3
RUN_COUNT = 6
CANCEL_COUNT = 3


def _start(state: dict[str, Any], symphony: str = "alpha") -> Any:
    begin_attempt(state, symphony, AttemptStart("abc1234", NOW, 1, "codex", "model", "perf"))
    attach_job(state, symphony, job_id="job-alpha", pod="pod-alpha")
    return state["bootstrap_history"].open_attempt(symphony)


@pytest.mark.asyncio
async def test_restart_restores_bounded_runs_and_redacted_details(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        state: dict[str, Any] = {"bootstrap_history": BootstrapHistory(), "event_store": store}
        first_run_id = None
        for _ in range(RUN_COUNT):
            record = _start(state)
            first_run_id = first_run_id or record.run_id
            report_completion(state, "alpha", AttemptEnd(NOW, False, "failed\nTOKEN=private-value", "abc1234", 1),
                              BudgetNow(3, False, None))
        run_id = record.run_id
        await store.flush()
    finally:
        await store.close()
    reopened = EventStore(tmp_path / "events.db")
    await reopened.start()
    try:
        restored: dict[str, Any] = {"bootstrap_history": BootstrapHistory(limit=HISTORY_BOUND), "event_store": reopened}
        await load_bootstrap_history(restored, ["alpha", "beta"])
        rows = restored["bootstrap_history"].recent("alpha", RUN_COUNT)
        assert len(rows) == HISTORY_BOUND
        assert rows[0].run_id == run_id
        assert rows[0].outcome == "failed"
        assert restored["bootstrap_history"].recent("beta", RUN_COUNT) == []
        detail = await read_bootstrap_run(restored, "alpha", run_id)
        assert detail["job_id"] == "job-alpha"
        assert "private-value" not in str(detail)
        assert len(detail["reason_tail"]) == 2
        assert await read_bootstrap_run(restored, "beta", run_id) is None
        archived = await read_bootstrap_run(restored, "alpha", first_run_id)
        assert archived["outcome"] == "failed"
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_cancel_stops_owning_runtime_and_closes_history_once(tmp_path) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    state["activity_log"] = ActivityLog()
    state["bootstrap_history"] = BootstrapHistory()
    run = _start(state)
    state["env_cache"]["alpha"] = EnvCacheState(symphony_name="alpha", sanitised_name="alpha",
                                               cache_dir=tmp_path, bootstrap_in_flight=True)
    state["env_cache_service"] = EnvCacheService(state["coordinare_config"])
    owner = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    task = asyncio.create_task(asyncio.Event().wait())
    state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(owner, "job-alpha", task)}
    result = await cancel_bootstrap(state, "alpha", "operator@example.org")
    assert result["stopped"] is True
    owner.stop_session_confirmed.assert_awaited_once_with("job-alpha")
    assert task.cancelled()
    assert run.outcome == "cancelled"
    assert run.reason == "cancelled by operator@example.org"
    assert run.cancelled_by == "operator@example.org"
    assert run.ended_at is not None
    assert state["env_cache"]["alpha"].bootstrap_attempts == 0
    assert not state["env_cache"]["alpha"].bootstrap_in_flight
    entries = state["activity_log"].snapshot()
    assert any("cancelled by operator@example.org" in entry["text"] for entry in entries)
    assert not any("failed" in entry["text"].lower() for entry in entries)
    with pytest.raises(ValueError, match="No bootstrap"):
        await cancel_bootstrap(state, "alpha", "operator@example.org")
    assert len(state["bootstrap_history"].recent("alpha", RUN_COUNT)) == 1


@pytest.mark.asyncio
async def test_job_activity_is_persisted_and_scoped_after_restart(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        state: dict[str, Any] = {"event_store": store, "bootstrap_history": BootstrapHistory(),
                                 "activity_log": ActivityLog()}
        run = _start(state)
        event = {"type": "tool_use", "text": "install package", "timestamp": NOW.isoformat()}
        record_bootstrap_events(state, "alpha", "job-alpha", [event])
        record_bootstrap_events(state, "alpha", "job-alpha", [event])
        await store.flush()
        detail = await read_bootstrap_run(state, "alpha", run.run_id)
        assert len(detail["events"]) == 1
        assert detail["last_activity_at"] == NOW.isoformat()
        assert detail["events"][0]["text"] == "install package"
        assert state["activity_log"].snapshot()[0]["session_id"] == run.run_id
        assert state["activity_log"].snapshot()[0]["activity_type"] == "tool_use"
        state["bootstrap_history"] = BootstrapHistory()
        await load_bootstrap_history(state, ["alpha"])
        recovered = await read_bootstrap_run(state, "alpha", run.run_id)
        assert recovered["outcome"] == "interrupted"
        assert recovered["events"][0]["text"] == "install package"
        assert recovered["last_activity_at"] == NOW.isoformat()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_verifier_tail_and_original_manifest_survive_restart(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        cache = SimpleNamespace(manifest_items=[{"name": "python", "version": "3.12", "install_hint": "TOKEN=private"}])
        state: dict[str, Any] = {"bootstrap_history": BootstrapHistory(), "event_store": store,
                                 "env_cache": {"alpha": cache}}
        run = _start(state)
        cache.manifest_items = [{"name": "different"}]
        record_verification(state, "alpha", True, "Verified python\nTOKEN=private")
        report_completion(state, "alpha", AttemptEnd(NOW, True, None, "abc1234", 1), BudgetNow(3, False, None))
        await store.flush()
        state["bootstrap_history"] = BootstrapHistory()
        detail = await read_bootstrap_run(state, "alpha", run.run_id)
        assert detail["verify_verdict"] == "passed"
        assert detail["manifest"][0]["name"] == "python"
        assert detail["reason_tail"][0] == "Verified python"
        assert "private" not in str(detail)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unavailable_store_does_not_prevent_startup() -> None:
    state: dict[str, Any] = {"bootstrap_history": BootstrapHistory(),
                             "event_store": SimpleNamespace(query_events=AsyncMock(side_effect=sqlite3.OperationalError()))}
    await load_bootstrap_history(state, ["alpha"])
    assert state["bootstrap_history"].recent("alpha", RUN_COUNT) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_result", [False, RuntimeError("runtime unavailable")])
async def test_unconfirmed_cancel_keeps_job_in_flight(tmp_path, stop_result) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    state["bootstrap_history"] = BootstrapHistory()
    run = _start(state)
    state["env_cache"]["alpha"] = EnvCacheState(symphony_name="alpha", sanitised_name="alpha",
                                               cache_dir=tmp_path, bootstrap_in_flight=True)
    state["env_cache_service"] = EnvCacheService(state["coordinare_config"])
    stop = AsyncMock(return_value=stop_result if stop_result is False else None,
                     side_effect=stop_result if isinstance(stop_result, Exception) else None)
    task = asyncio.create_task(asyncio.Event().wait())
    state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(SimpleNamespace(stop_session_confirmed=stop),
                                                            "job-alpha", task)}
    result = await cancel_bootstrap(state, "alpha", "Ada")
    assert result["stopped"] is False
    assert result["status"] == "stop_unconfirmed"
    assert run.ended_at is None
    assert run.outcome == "running"
    assert run.cancelled_by == "Ada"
    assert state["env_cache"]["alpha"].bootstrap_in_flight
    assert not task.cancelled()
    detail = await read_bootstrap_run(state, "alpha", run.run_id)
    assert detail["state"] == "stop_unconfirmed"
    status = symphony_view(state, "alpha", configured=True, now=NOW)
    assert status["state"] == "stop_unconfirmed"
    assert status["label"] == "Stop unconfirmed"
    assert "stop unconfirmed" in hold_text(status).lower()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["env_cache"]["alpha"].bootstrap_attempts == 0


@pytest.mark.asyncio
async def test_second_cancel_cannot_close_or_stop_the_run_twice(tmp_path) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    state["bootstrap_history"] = BootstrapHistory()
    run = _start(state)
    state["env_cache"]["alpha"] = EnvCacheState(symphony_name="alpha", sanitised_name="alpha",
                                               cache_dir=tmp_path, bootstrap_in_flight=True)
    state["env_cache_service"] = EnvCacheService(state["coordinare_config"])
    started, release = asyncio.Event(), asyncio.Event()

    async def stop(job_id: str) -> bool:
        assert job_id == "job-alpha"
        started.set()
        await release.wait()
        return True

    task = asyncio.create_task(asyncio.Event().wait())
    state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(SimpleNamespace(stop_session_confirmed=stop),
                                                            "job-alpha", task)}
    cancellation = asyncio.create_task(cancel_bootstrap(state, "alpha", "Ada"))
    await started.wait()
    try:
        with pytest.raises(ValueError, match="No bootstrap"):
            await cancel_bootstrap(state, "alpha", "Other actor")
    finally:
        release.set()
        await cancellation
    assert run.reason == "cancelled by Ada"
    assert state["env_cache"]["alpha"].bootstrap_attempts == 0


@pytest.mark.asyncio
async def test_dispatch_retains_the_owner_when_configuration_changes(tmp_path) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    run = _start(state)
    state["env_cache"]["alpha"] = EnvCacheState(symphony_name="alpha", sanitised_name="alpha",
                                               cache_dir=tmp_path, bootstrap_in_flight=True)
    cache_service = EnvCacheService(state["coordinare_config"])
    state["env_cache_service"] = cache_service
    owner = SimpleNamespace(dispatch_card=AsyncMock(return_value={"session_id": "job-alpha", "container_id": "pod"}),
                            check_status=AsyncMock(return_value={"status": "working"}),
                            stop_session_confirmed=AsyncMock(return_value=True))
    await daemon._finish_bootstrap_dispatch(owner, "perf", "alpha", {}, {}, cache_service)
    replacement = SimpleNamespace(stop_session_confirmed=AsyncMock())
    state["performer_services_by_id"] = {"perf": replacement}
    result = await daemon.cancel_env_bootstrap("alpha", "Ada")
    assert result["stopped"] is True
    assert run.outcome == "cancelled"
    owner.stop_session_confirmed.assert_awaited_once_with("job-alpha")
    replacement.stop_session_confirmed.assert_not_awaited()
    assert not state["bootstrap_runtimes"]


def test_observability_failures_cannot_fail_the_bootstrap() -> None:
    store = SimpleNamespace(emit=Mock(side_effect=OSError("read only disk")))
    state: dict[str, Any] = {"bootstrap_history": BootstrapHistory(), "event_store": store}
    run = _start(state)
    record_verification(state, "alpha", True, "Verified")
    record_bootstrap_events(state, "alpha", "job-alpha", [{("invalid", "key"): "value"}])
    assert run.outcome == "running"
    assert run.verify_verdict == "passed"


@pytest.mark.asyncio
async def test_failed_poller_does_not_remove_the_owner_of_a_live_job(tmp_path) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    run = _start(state)
    state["env_cache"]["alpha"] = EnvCacheState(symphony_name="alpha", sanitised_name="alpha",
                                               cache_dir=tmp_path, bootstrap_in_flight=True)
    state["env_cache_service"] = EnvCacheService(state["coordinare_config"])

    async def failed_poll() -> None:
        message = "poll failed"
        raise RuntimeError(message)

    task = asyncio.create_task(failed_poll())
    await asyncio.sleep(0)
    owner = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(owner, "job-alpha", task)}
    release_bootstrap_runtime(state, "alpha", task)
    assert "alpha" in state["bootstrap_runtimes"]
    assert (await cancel_bootstrap(state, "alpha", "Ada"))["stopped"] is True
    assert run.reason == "cancelled by Ada"


@pytest.mark.asyncio
async def test_cardless_bootstrap_activity_keeps_its_explicit_symphony(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    install_event_store(store, lambda card_id: "other-symphony")
    try:
        ActivityLog().record(activity_type="env_bootstrap_failed", symphony="alpha", session_id="run-alpha",
                             stage="env_bootstrap", text="Bootstrap failed")
        await store.flush()
        rows = (await store.query_events(EventFilters(run_id="run-alpha", kind="env_bootstrap_failed")))["events"]
        assert len(rows) == 1
        assert rows[0]["symphony"] == "alpha"
        assert rows[0]["run_id"] == "run-alpha"
    finally:
        install_event_store(None)
        await store.close()


@pytest.mark.asyncio
async def test_three_operator_cancels_preserve_budget_and_sha(tmp_path) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    cache = EnvCacheState(symphony_name="alpha", sanitised_name="alpha", cache_dir=tmp_path,
                          readme_sha="retained", bootstrap_attempts=1)
    state["env_cache"]["alpha"] = cache
    state["bootstrap_history"] = BootstrapHistory()
    service = EnvCacheService(state["coordinare_config"])
    state["env_cache_service"] = service
    owner = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=True))
    for _ in range(CANCEL_COUNT):
        _start(state)
        cache.bootstrap_in_flight = True
        task = asyncio.create_task(asyncio.Event().wait())
        state["bootstrap_runtimes"] = {"alpha": BootstrapRuntime(owner, "job-alpha", task)}
        await cancel_bootstrap(state, "alpha", "Ada")
        assert cache.bootstrap_attempts == 1
        assert not cache.bootstrap_exhausted
        assert cache.readme_sha == "retained"
        assert cache.last_bootstrap_at is not None
        dispatch = AsyncMock()
        await service.check_and_trigger("alpha", SimpleNamespace(env_bootstrap_performer_id="perf"),
                                        Mock(), state, dispatch)
        dispatch.assert_not_awaited()
    assert len(state["bootstrap_history"].recent("alpha", RUN_COUNT)) == CANCEL_COUNT
    status = symphony_view(state, "alpha", configured=True, now=cache.last_bootstrap_at)
    assert status["state"] == "cancelled"
    assert status["reason"] == "cancelled by Ada"
    assert "cancelled by Ada" in hold_text(status)
    assert "exhausted" not in hold_text(status)
    assert datetime.fromisoformat(status["next_attempt_at"]) >= (
        cache.last_bootstrap_at + timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S)
    )


@pytest.mark.asyncio
async def test_performer_cannot_forge_snapshot_history(tmp_path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        state: dict[str, Any] = {"bootstrap_history": BootstrapHistory(), "event_store": store}
        run = _start(state)
        forged = {"number": 1, "attempt": 1, "commit": "forged", "started_at": NOW.isoformat(),
                  "backend": "forged", "model": "", "performer_id": "", "run_id": run.run_id,
                  "outcome": "succeeded"}
        record_bootstrap_events(state, "alpha", "job-alpha", [{"type": RUN_SNAPSHOT_KIND, **forged}])
        store.emit({"symphony": "alpha", "run_id": "fake", "source": "performer",
                    "kind": RUN_SNAPSHOT_KIND, "data": {**forged, "run_id": "fake"}})
        await store.flush()
        output = await store.query_events(EventFilters(source="performer", run_id=run.run_id))
        assert output["events"][0]["kind"] == "bootstrap.output.output"
        state["bootstrap_history"] = BootstrapHistory()
        await load_bootstrap_history(state, ["alpha"])
        rows = state["bootstrap_history"].recent("alpha", RUN_COUNT)
        assert len(rows) == 1
        assert rows[0].commit == "abc1234"
        assert await read_bootstrap_run(state, "alpha", "fake") is None
        state["bootstrap_history"] = BootstrapHistory()
        detail = await read_bootstrap_run(state, "alpha", run.run_id)
        assert detail["commit"] == "abc1234"
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [True, False])
async def test_recovery_never_falls_back_to_an_older_snapshot(tmp_path, malformed) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    try:
        state: dict[str, Any] = {"bootstrap_history": BootstrapHistory(), "event_store": store}
        run = _start(state)
        run.commit = "newest"
        if malformed:
            store.emit({"symphony": "alpha", "run_id": run.run_id, "source": "bootstrap",
                        "kind": RUN_SNAPSHOT_KIND, "data": {"started_at": "invalid"}})
        else:
            persist_attempt(state, "alpha", run)
        await store.flush()
        state["bootstrap_history"] = BootstrapHistory()
        await load_bootstrap_history(state, ["alpha"])
        rows = state["bootstrap_history"].recent("alpha", RUN_COUNT)
        if malformed:
            assert rows == []
            assert await read_bootstrap_run(state, "alpha", run.run_id) is None
        else:
            assert len(rows) == 1
            assert rows[0].commit == "newest"
    finally:
        await store.close()


@pytest.mark.parametrize("exhausted", [True, False])
def test_cancelled_completion_preserves_both_budgets(tmp_path, exhausted) -> None:
    daemon = _make_daemon_for_env_cache()
    state = daemon.state
    run = _start(state)
    cache = EnvCacheState(symphony_name="alpha", sanitised_name="alpha", cache_dir=tmp_path,
                          readme_sha="retained", bootstrap_attempts=CANCEL_COUNT, bootstrap_exhausted=exhausted,
                          infra_failures=CANCEL_COUNT, bootstrap_in_flight=True)
    state["env_cache"]["alpha"] = cache
    service = EnvCacheService(state["coordinare_config"])
    service.on_bootstrap_cancelled("alpha", state, "Ada")
    assert cache.bootstrap_attempts == CANCEL_COUNT
    assert cache.bootstrap_exhausted is exhausted
    assert cache.infra_failures == CANCEL_COUNT
    assert cache.readme_sha == "retained"
    assert run.outcome == "cancelled"
