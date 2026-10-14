"""Spec 132, independent review of PR #207: the failing tests for each finding.

The guiding rule these tests hold to: log collection is a side feature. It must never
delay, skip or freeze a container stop, and it must never write a secret to disk. The
teardown tests drive the real ``_cleanup_ephemeral_job`` and ``aclose`` paths; the
dispatch tests drive the real ``dispatch_card`` with a refusing performer.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services import run_log as rl
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.performer_runtime import StartedPerformer
from coordinare.services.run_log import (
    RootGuard,
    RunLogWriter,
    make_run_logs_collector,
    read_run_log_window,
)
from coordinare.transport.base import TransportError
from coordinare.workspace import WorkspaceInfo

SESSION = "sess-review132"
CONTAINER = "cid-review132"
SECRET = "SUPERSECRET-XYZ-123456"  # matches no pattern: only exact-value redaction removes it
GH_TOKEN = "ghp_" + "c" * 36
AMBIENT = "ambient-deploy-998877"
WAIT = 5.0  # generous: CI runs coverage several times slower


class _Client:
    async def aclose(self) -> None:
        return None


class _Runtime:
    """Observable runtime. ``hang`` makes the log stream wait forever after ``head``."""

    def __init__(self, *, head: tuple[bytes, ...] = (b"line one\n",), hang: bool = False) -> None:
        self.head = head
        self.hang = hang
        self.stops = 0
        self.started = asyncio.Event()
        self.tail: list[str] = []

    async def iter_logs(self, handle: str) -> Any:
        self.started.set()
        for chunk in self.head:
            yield chunk
        if self.hang:
            await asyncio.Event().wait()

    async def stop(self, handle: str, **kwargs: Any) -> None:
        self.stops += 1

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        return list(self.tail)


def _service(tmp: Path, runtime: Any, *, run_id: str = SESSION) -> HTTPPerformerService:
    config = PerformerEndpointConfig(id="perf-r132", mode="ephemeral", image="img", roles=["implementer"])
    collector = make_run_logs_collector(
        enabled=True, root=tmp / "run-logs", max_bytes=1024 * 1024, retention_days=30,
        guard=RootGuard(),
    )
    service = HTTPPerformerService(config, runtime=cast("Any", runtime), run_logs=collector)
    service._active_jobs[run_id] = hps._EphemeralJob(
        container_id=CONTAINER, endpoint="http://127.0.0.1:9",
        client=cast("Any", _Client()), run_id=run_id,
    )
    return service


async def _drain_finishers(service: HTTPPerformerService) -> None:
    finishers = list(service._log_finishers)
    if finishers:
        await asyncio.wait(finishers, timeout=WAIT)


# --- 1: a cancelled capture must not skip the stop (direct, unshielded path) ---------------


async def test_a_cancelled_capture_still_stops_the_container_on_the_direct_cleanup_path(
    tmp_path: Path,
) -> None:
    runtime = _Runtime(hang=True)
    service = _service(tmp_path, runtime)
    job = service._active_jobs.pop(SESSION)

    task = asyncio.ensure_future(service._cleanup_ephemeral_job(job))
    await asyncio.wait_for(runtime.started.wait(), WAIT)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.stops == 1


async def test_a_failing_stop_is_logged_and_the_cleanup_completes(tmp_path: Path) -> None:
    class _StopFails(_Runtime):
        async def stop(self, handle: str, **kwargs: Any) -> None:
            self.stops += 1
            raise RuntimeError("docker is gone")

    runtime = _StopFails()
    service = _service(tmp_path, runtime)
    job = service._active_jobs.pop(SESSION)

    await service._cleanup_ephemeral_job(job)

    assert runtime.stops == 1


# --- 2: a blocked write must not hold the bound, the stop, or the event loop ---------------


async def test_a_write_that_never_returns_is_abandoned_at_the_bound_and_the_loop_keeps_ticking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = 0.3
    monkeypatch.setattr(hps, "_RUN_LOG_TEARDOWN_SECONDS", bound)
    release = threading.Event()
    original = rl._write_all
    calls = {"n": 0}

    def stuck_once(fd: int, payload: bytes) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            release.wait(WAIT * 4)  # a stalled disk: the write does not return in time
        original(fd, payload)

    monkeypatch.setattr(rl, "_write_all", stuck_once)
    runtime = _Runtime(head=(b"a" * 100 + b"\n", b"b" * 100 + b"\n"))
    service = _service(tmp_path, runtime)
    ticks: list[float] = []

    async def ticker() -> None:
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    ticking = asyncio.ensure_future(ticker())
    try:
        started = time.monotonic()
        await asyncio.wait_for(service._cleanup_ephemeral_job_by_id(SESSION), WAIT)
        elapsed = time.monotonic() - started
        gap = max(later - earlier for earlier, later in pairwise(ticks))
    finally:
        ticking.cancel()
        release.set()
        await _drain_finishers(service)

    assert runtime.stops == 1
    assert elapsed < bound * 4 + 1.0
    assert gap < bound  # the loop never froze on the writer's lock


# --- 3: shutdown must not delay the stops ---------------------------------------------------


async def test_shutdown_reaches_every_stop_promptly_without_collecting_hung_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hps, "_RUN_LOG_TEARDOWN_SECONDS", 1.0)
    runtime = _Runtime(hang=True)
    service = _service(tmp_path, runtime)
    for index in range(2):
        service._active_jobs[f"s{index}"] = hps._EphemeralJob(
            container_id=f"c{index}", endpoint="http://127.0.0.1:9",
            client=cast("Any", _Client()), run_id=f"s{index}",
        )

    started = time.monotonic()
    await asyncio.wait_for(service.aclose(), WAIT)
    elapsed = time.monotonic() - started

    assert runtime.stops == 3
    assert elapsed < 0.9  # three sequential 1 s collections would take 3 s


# --- 4: a secret split by a forced cut must not reach the disk ----------------------------


def _writer(tmp: Path, secrets: tuple[str, ...] = (SECRET,)) -> RunLogWriter:
    return RunLogWriter(tmp / "run-logs", run_id="r4", max_bytes=10 * 1024 * 1024, secrets=secrets)


def test_a_secret_split_across_a_forced_cut_is_not_written(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.feed(b"x" * 70000 + SECRET[:6].encode())
    writer.feed(SECRET[6:].encode() + b" after\n")
    assert writer.finish() is True

    text = (tmp_path / "run-logs" / "r4.log").read_text(encoding="utf-8")
    assert SECRET not in text
    assert "SUPERS" not in text
    assert "[REDACTED]" in text


def test_a_secret_split_in_carriage_return_progress_output_is_not_written(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.feed(b"p" * 70000 + b"\r" + SECRET[:9].encode())
    writer.feed(SECRET[9:].encode() + b"\r" + b"done\n")
    assert writer.finish() is True

    text = (tmp_path / "run-logs" / "r4.log").read_text(encoding="utf-8")
    assert SECRET not in text
    assert "SUPERSECRET" not in text


def test_a_secret_split_by_a_long_line_is_redacted_when_the_line_ends(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    writer.feed(b"y" * 100 + SECRET[:4].encode() + b"z" * 70000)
    writer.feed(SECRET[4:].encode() + b"\n")
    assert writer.finish() is True

    text = (tmp_path / "run-logs" / "r4.log").read_text(encoding="utf-8")
    assert SECRET not in text
    assert "SUPER" not in text


# --- 5: a refused dispatch leaves no secret behind and cleans its container ---------------


def _card() -> dict[str, object]:
    return {"id": "card-r132", "role": "implementer", "backend": "claude_code",
            "persona_instructions": "be excellent"}


def _workspace() -> WorkspaceInfo:
    return WorkspaceInfo(path=None, repo_url="https://github.com/x/y", branch="main", github_token=SECRET)


class _StartingRuntime(_Runtime):
    async def start_ephemeral(self, config: Any, **kwargs: Any) -> StartedPerformer:
        return StartedPerformer(handle="pod-r132", endpoint="http://127.0.0.1:9")


class _Refuser:
    def __init__(self, error: BaseException | None = None, accepted: bool = True) -> None:
        self.error = error
        self.accepted = accepted

    async def post_job(self, payload: Any) -> Any:
        if self.error is not None:
            raise self.error
        if not self.accepted:
            return SimpleNamespace(accepted=False, reason="busy", detail="slot")
        return SimpleNamespace(accepted=True, job_id="job-r132")

    async def aclose(self) -> None:
        return None


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://performer.test/jobs")
    return httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))


@pytest.mark.parametrize(
    ("client", "expects_raise"),
    [
        (_Refuser(error=_status_error()), True),
        (_Refuser(accepted=False), False),
        (_Refuser(error=TransportError("connection reset")), False),
        (_Refuser(error=RuntimeError("unexpected")), True),
    ],
    ids=["http-5xx", "busy-409", "transport", "unexpected"],
)
async def test_a_refused_dispatch_keeps_no_secret_and_stops_its_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: _Refuser,
    expects_raise: bool,
) -> None:
    async def fake_wait_ready(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(hps.performer_lifecycle, "wait_ready", fake_wait_ready)
    runtime = _StartingRuntime()
    config = PerformerEndpointConfig(id="perf-r132", mode="ephemeral", image="img", roles=["implementer"])
    service = HTTPPerformerService(
        config, client=cast("Any", client), runtime=cast("Any", runtime),
        run_logs=make_run_logs_collector(
            enabled=True, root=tmp_path / "run-logs", max_bytes=1024 * 1024, retention_days=30,
            guard=RootGuard(),
        ),
    )

    if expects_raise:
        with pytest.raises((httpx.HTTPStatusError, RuntimeError)):  # the refusal propagates
            await service.dispatch_card(_card(), _workspace())
    else:
        result = await service.dispatch_card(_card(), _workspace())
        assert result["status"] == "error"

    assert service._transcript_secrets == {}
    assert runtime.stops == 1
    assert service._active_jobs == {}


# --- 6: an unstarted download holds no file descriptor ------------------------------------


def test_an_unstarted_download_holds_no_descriptor(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    (root / "r6.log").write_bytes(b"hello\n")
    before = len(os.listdir("/dev/fd"))

    stream = rl.iter_run_log_file(root, "r6")
    assert len(os.listdir("/dev/fd")) == before  # nothing opened until it is iterated
    del stream

    assert len(os.listdir("/dev/fd")) == before


# --- 7: an offset beyond the file is an empty page, never an overflow ----------------------


def test_an_offset_far_past_the_end_is_an_empty_page(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    (root / "r7.log").write_bytes(b"short\n")

    for offset in (6, 2**63, 2**70):
        window = read_run_log_window(root, "r7", offset=offset, limit=10)
        assert window is not None
        assert window.text == ""
        assert window.next_offset is None


# --- 8: the live tail redacts what the full log redacts -----------------------------------


async def test_the_live_tail_redacts_ambient_secrets_too(tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.tail = [f"deploy with {AMBIENT} now", f"push {GH_TOKEN}"]
    service = _service(tmp_path, runtime)
    assert service._run_logs is not None
    service._run_logs.add_ambient_secrets({"DEPLOY_TOKEN": AMBIENT})
    service._active_jobs.pop(SESSION)

    await service._poll_container_logs(CONTAINER, SESSION)

    joined = "\n".join(service.get_agent_logs())
    assert AMBIENT not in joined
    assert GH_TOKEN not in joined


async def test_a_refreshed_secret_does_not_recreate_a_released_run(tmp_path: Path) -> None:
    runtime = _Runtime()
    service = _service(tmp_path, runtime)
    await service._cleanup_ephemeral_job_by_id(SESSION)

    service._remember_refreshed_secret(SESSION, "rotated-token-424242")

    assert SESSION not in service._transcript_secrets


# --- 10 and 11 are covered at the writer and route level in the test files below ----------


async def test_a_large_log_cut_off_at_the_bound_keeps_what_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hps, "_RUN_LOG_TEARDOWN_SECONDS", 0.3)

    class _Slow(_Runtime):
        async def iter_logs(self, handle: str) -> Any:
            yield b"first part of a long run\n"
            await asyncio.Event().wait()  # the rest never arrives in time
            yield b"never\n"

    runtime = _Slow()
    service = _service(tmp_path, runtime)
    await service._cleanup_ephemeral_job_by_id(SESSION)
    await _drain_finishers(service)

    final = tmp_path / "run-logs" / f"{SESSION}.log"
    assert final.exists()
    text = final.read_text(encoding="utf-8")
    assert "first part of a long run" in text
    assert "cut off" in text
    assert "never" not in text
    assert rl.run_log_exists(tmp_path / "run-logs", SESSION)


# --- 10: a page never splits a UTF-8 character ---------------------------------------------


def test_pages_never_split_a_character(tmp_path: Path) -> None:
    root = tmp_path / "run-logs"
    root.mkdir()
    body = ("aé€\U0001f600" * 50).encode("utf-8")
    (root / "r10.log").write_bytes(body)

    pages: list[str] = []
    offset: int | None = 0
    while offset is not None and len(pages) < 1000:
        window = read_run_log_window(root, "r10", offset=offset, limit=4)
        assert window is not None
        assert "�" not in window.text
        pages.append(window.text)
        offset = window.next_offset

    assert "".join(pages) == body.decode("utf-8")
    inside = read_run_log_window(root, "r10", offset=2, limit=4)  # offset 2 is mid-character
    assert inside is not None
    assert "�" not in inside.text
