"""Spec 132: the full log is collected at teardown, before the container is removed.

The teardown ordering and bounds from #174 must still hold: the stop always happens,
once, within a bounded time, whatever the runtime's log stream does. Redaction of the
live tail is pinned here too, because it is the same text.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, cast

import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.run_log import RootGuard, make_run_logs_collector

GH_TOKEN = "ghp_" + "c" * 36
EXACT_JOB_SECRET = "jobsecret-7f3a91c2"
SESSION = "sess-132"
CONTAINER = "cid-132"
WAIT_FOR_CAPTURE = 5.0  # generous: CI runs coverage several times slower
TEARDOWN_BOUND = 0.05  # short enough to test the bound quickly, long enough to be a real wait


class _Client:
    async def aclose(self) -> None:
        return None


class _Runtime:
    """A runtime whose log stream and stop are observable. ``hang`` makes the stream wait forever."""

    def __init__(self, tmp: Path, *, hang: bool = False) -> None:
        self.events: list[str] = []
        self.final_path = tmp / "run-logs" / f"{SESSION}.log"
        self.file_present_at_stop: bool | None = None
        self.stops = 0
        self.hang = hang
        self.started = asyncio.Event()
        self.tail: list[str] = [f"push with {GH_TOKEN}", f"value {EXACT_JOB_SECRET} here"]

    async def iter_logs(self, handle: str) -> Any:
        self.events.append("logs")
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        yield b"line one\n"
        yield b"line two\n"

    async def stop(
        self,
        handle: str,
        *,
        timeout_s: int = 10,
        host_log_dir: Path | None = None,
        performer_id: str | None = None,
    ) -> None:
        self.events.append("stop")
        self.stops += 1
        self.file_present_at_stop = os.path.exists(self.final_path)

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        return list(self.tail)


class _FailingRuntime(_Runtime):
    """A log stream that yields once and then raises, as a Pod that disappears would."""

    async def iter_logs(self, handle: str) -> Any:
        self.events.append("logs")
        yield b"partial\n"
        raise RuntimeError("the pod went away")


class _ErrorAtOnceRuntime(_Runtime):
    """A log stream that fails before it yields anything."""

    async def iter_logs(self, handle: str) -> Any:
        self.events.append("logs")
        await asyncio.sleep(0)
        raise ConnectionError("unreachable")
        yield b""  # makes this an async generator that fails on first use


class _NoLogsRuntime:
    """A runtime that predates the full-log seam: no ``iter_logs`` at all."""

    def __init__(self) -> None:
        self.stops = 0

    async def stop(self, handle: str, **kwargs: Any) -> None:
        self.stops += 1


def _service(tmp: Path, runtime: Any, *, enabled: bool = True) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id="perf-132", mode="ephemeral", image="performer:full", roles=["implementer"],
    )
    collector = make_run_logs_collector(
        enabled=enabled, root=tmp / "run-logs", max_bytes=1024 * 1024, retention_days=30,
        guard=RootGuard(),
    )
    service = HTTPPerformerService(config, runtime=cast("Any", runtime), run_logs=collector)
    service._active_jobs[SESSION] = hps._EphemeralJob(
        container_id=CONTAINER,
        endpoint="http://127.0.0.1:9",
        client=cast("Any", _Client()),
        run_id=SESSION,
    )
    return service


def _final(tmp: Path) -> Path:
    return tmp / "run-logs" / f"{SESSION}.log"


def _partial(tmp: Path) -> Path:
    return tmp / "run-logs" / f"{SESSION}.log.partial"


async def _drain_finishers(service: HTTPPerformerService) -> None:
    """Wait for background finishes of cut-off logs, so their file is in place to check."""
    finishers = list(service._log_finishers)
    if finishers:
        await asyncio.wait(finishers, timeout=WAIT_FOR_CAPTURE)


async def test_the_full_log_is_written_before_the_container_is_stopped(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    service = _service(tmp_path, runtime)

    await service._cleanup_ephemeral_job_by_id(SESSION)

    assert runtime.events == ["logs", "stop"]
    assert runtime.file_present_at_stop is True
    assert _final(tmp_path).read_bytes() == b"line one\nline two\n"


async def test_the_collection_runs_once_per_job(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    service = _service(tmp_path, runtime)
    job = service._active_jobs[SESSION]

    await service._cleanup_ephemeral_job(job)
    await service._cleanup_ephemeral_job(job)

    assert runtime.events.count("logs") == 1


async def test_a_log_stream_that_hangs_is_cut_off_at_the_bound_and_the_container_still_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hps, "_RUN_LOG_TEARDOWN_SECONDS", TEARDOWN_BOUND)
    runtime = _Runtime(tmp_path, hang=True)
    service = _service(tmp_path, runtime)

    started = time.monotonic()
    await asyncio.wait_for(service._cleanup_ephemeral_job_by_id(SESSION), WAIT_FOR_CAPTURE)
    assert time.monotonic() - started < WAIT_FOR_CAPTURE
    await _drain_finishers(service)

    # What arrived before the bound is kept, and the log says it was cut off.
    assert runtime.stops == 1
    assert _final(tmp_path).exists()
    assert "cut off" in _final(tmp_path).read_text(encoding="utf-8")
    assert not _partial(tmp_path).exists()


async def test_a_log_stream_that_raises_mid_way_leaves_no_file_and_still_stops(tmp_path: Path) -> None:
    runtime = _FailingRuntime(tmp_path)
    service = _service(tmp_path, runtime)

    await service._cleanup_ephemeral_job_by_id(SESSION)

    assert runtime.stops == 1
    assert not _final(tmp_path).exists()
    assert not _partial(tmp_path).exists()


async def test_a_log_stream_that_fails_at_once_still_stops(tmp_path: Path) -> None:
    runtime = _ErrorAtOnceRuntime(tmp_path)
    service = _service(tmp_path, runtime)

    await service._cleanup_ephemeral_job_by_id(SESSION)

    assert runtime.stops == 1
    assert not _final(tmp_path).exists()


async def test_a_runtime_without_the_log_seam_is_stopped_as_before(tmp_path: Path) -> None:
    runtime = _NoLogsRuntime()
    service = _service(tmp_path, runtime)

    await service._cleanup_ephemeral_job_by_id(SESSION)

    assert runtime.stops == 1
    assert not (tmp_path / "run-logs").exists()


async def test_cancelling_the_teardown_still_stops_the_container_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hps, "_RUN_LOG_TEARDOWN_SECONDS", TEARDOWN_BOUND)
    runtime = _Runtime(tmp_path, hang=True)
    service = _service(tmp_path, runtime)

    task = asyncio.ensure_future(service._cleanup_ephemeral_job_by_id(SESSION))
    await asyncio.wait_for(runtime.started.wait(), WAIT_FOR_CAPTURE)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.stops == 1


async def test_a_disabled_run_log_collector_collects_nothing(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    service = _service(tmp_path, runtime, enabled=False)

    await service._cleanup_ephemeral_job_by_id(SESSION)

    assert runtime.events == ["stop"]
    assert not (tmp_path / "run-logs").exists()


async def test_the_live_tail_is_redacted_before_it_is_stored(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    service = _service(tmp_path, runtime)
    service._transcript_secrets[SESSION] = [EXACT_JOB_SECRET]

    # Off the active list, so the poll performs its final capture and returns.
    service._active_jobs.pop(SESSION)
    await service._poll_container_logs(CONTAINER, SESSION)

    lines = service.get_agent_logs()
    assert len(lines) == 2
    assert GH_TOKEN not in "\n".join(lines)
    assert EXACT_JOB_SECRET not in "\n".join(lines)
    assert "[REDACTED]" in lines[0]
    assert "[REDACTED]" in lines[1]


async def test_the_job_secrets_are_released_once_its_container_is_torn_down(tmp_path: Path) -> None:
    runtime = _Runtime(tmp_path)
    service = _service(tmp_path, runtime)
    service._transcript_secrets[SESSION] = [EXACT_JOB_SECRET]

    await service._cleanup_ephemeral_job_by_id(SESSION)

    assert SESSION not in service._transcript_secrets
