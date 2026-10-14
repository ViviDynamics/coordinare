"""Spec 132, second focused review of PR #207: the failing tests for each finding.

Run-log I/O must never starve the default thread pool or hold a stop; a secret made of
a pattern (not only an exact value) must never be written across a forced cut; a
refused dispatch's stop must survive cancellation; a Docker stop by label collects the
log first; an abandoned writer lets go of the secrets; the capture bound and the drain
fit together; a committed log is known without touching the disk.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from kubernetes.client.rest import ApiException

from coordinare.dashboard import store as dashboard_store
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import http_performer_service as hps
from coordinare.services import run_log as rl
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.performer_runtime import StartedPerformer
from coordinare.services.run_log import RootGuard, RunLogWriter, make_run_logs_collector
from coordinare.workspace import WorkspaceInfo

SESSION = "sess-round3"
WAIT = 5.0  # generous: CI runs coverage several times slower
GHP = "ghp_" + "c" * 36
PG_PASSWORD = "Pw0rdSecretValue9"
PG_URL = f"postgresql://user:{PG_PASSWORD}@db.example:5432/app"
BEARER_TOKEN = "B" * 40
AWS_KEY = "AKIA" + "0123456789ABCDEF"
SECRET = "SUPERSECRET-XYZ-123456"  # exact job secret: matches no pattern


def _service(tmp: Path, runtime: Any) -> HTTPPerformerService:
    config = PerformerEndpointConfig(id="perf-r3", mode="ephemeral", image="img", roles=["implementer"])
    collector = make_run_logs_collector(
        enabled=True, root=tmp / "run-logs", max_bytes=1024 * 1024, retention_days=30,
        guard=RootGuard(),
    )
    service = HTTPPerformerService(config, runtime=cast("Any", runtime), run_logs=collector)
    service._active_jobs[SESSION] = hps._EphemeralJob(
        container_id="cid-r3", endpoint="http://127.0.0.1:9",
        client=cast("Any", _Client()), run_id=SESSION,
    )
    return service


class _Client:
    async def aclose(self) -> None:
        return None


# --- 1: a hung log read never starves the default pool, nor delays a stop ----------------


class _Core:
    """CoreV1 of a Kubernetes cluster whose log stream stalls on every read until released."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release
        self.deleted: list[str] = []

    def read_namespaced_pod_log(self, **kwargs: Any) -> Any:
        release = self.release

        class _Stalled:
            def stream(self, amount: int) -> Any:
                release.wait(WAIT * 10)  # a silent socket: the read does not return
                yield b""

            def close(self) -> None:
                return None

        return _Stalled()

    def delete_namespaced_pod(self, **kwargs: Any) -> None:
        self.deleted.append(str(kwargs.get("name")))

    def read_namespaced_pod(self, **kwargs: Any) -> None:
        raise ApiException(status=404, reason="gone")


async def test_fifty_hung_collections_do_not_starve_the_stop_or_the_default_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hps, "_RUN_LOG_TEARDOWN_SECONDS", 0.2)
    release = threading.Event()
    core = _Core(release)
    runtime = KubernetesRuntime(core_v1=core, owner="owner-r3")
    service = _service(tmp_path, runtime)
    try:
        for index in range(50):
            run_id = f"hung-{index}"
            service._active_jobs[run_id] = hps._EphemeralJob(
                container_id=f"pod-{index}", endpoint="http://127.0.0.1:9",
                client=cast("Any", _Client()), run_id=run_id,
            )
            await asyncio.wait_for(service._capture_run_log(service._active_jobs[run_id]), WAIT)

        # The default pool still runs ordinary work.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: 7), WAIT) == 7

        # A new run's Pod delete starts promptly.
        started = time.monotonic()
        await asyncio.wait_for(runtime.stop("pod-new"), WAIT)
        assert time.monotonic() - started < WAIT
        assert core.deleted == ["pod-new"]
    finally:
        release.set()


# --- 2: a pattern secret straddling a forced cut is never written -----------------------


def _write(tmp: Path, body: str, secrets: tuple[str, ...] = ()) -> str:
    writer = RunLogWriter(tmp / "run-logs", run_id="r2", max_bytes=10 * 1024 * 1024, secrets=secrets)
    writer.feed(body.encode("utf-8"))  # no newline: only the forced-cut path can split it
    assert writer.finish() is True
    return (tmp / "run-logs" / "r2.log").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("token", "needle"),
    [
        (GHP, GHP),
        (PG_URL, PG_PASSWORD),
        (f"Bearer {BEARER_TOKEN}", BEARER_TOKEN),
        (AWS_KEY, AWS_KEY),
    ],
    ids=["github-token", "postgres-password", "bearer", "aws-key"],
)
def test_a_pattern_secret_straddling_a_forced_cut_is_not_written(
    tmp_path: Path, token: str, needle: str,
) -> None:
    leaks = []
    for shift in range(-25, 26):
        body = "a" * (70000 + shift) + token + "x" * 8172
        if needle in _write(tmp_path / f"n{shift}", body):
            leaks.append(shift)
    assert leaks == []


@pytest.mark.parametrize("token,needle", [(GHP, GHP), (PG_URL, PG_PASSWORD)])
def test_a_pattern_secret_is_not_written_when_spaces_surround_it(tmp_path: Path, token: str, needle: str) -> None:
    leaks = []
    for shift in (-7, 0, 7):
        filler = ("a" * 10 + " ") * ((70000 + shift) // 11) + "a" * ((70000 + shift) % 11)
        body = f"{filler} {token} {'x' * 8172}"
        if needle in _write(tmp_path / f"w{shift}", body):
            leaks.append(shift)
    assert leaks == []


def test_a_pattern_secret_in_carriage_return_progress_is_not_written(tmp_path: Path) -> None:
    leaks = []
    for shift in (-5, 0, 5):
        body = "progress 9%\r" * ((70000 + shift) // 12) + "p" * ((70000 + shift) % 12)
        body += GHP + "\r" + "x" * 8172
        if GHP in _write(tmp_path / f"c{shift}", body):
            leaks.append(shift)
    assert leaks == []


def test_an_exact_secret_straddling_a_forced_cut_is_not_written(tmp_path: Path) -> None:
    leaks = []
    for shift in range(-10, 11):
        body = "a" * (70000 + shift) + SECRET + "x" * 8172
        if SECRET in _write(tmp_path / f"e{shift}", body, secrets=(SECRET,)):
            leaks.append(shift)
    assert leaks == []


# --- 3: a refused dispatch's stop survives two cancellations ----------------------------


class _SlowStop:
    def __init__(self) -> None:
        self.stops = 0
        self.finished = 0
        self.started = asyncio.Event()

    async def start_ephemeral(self, config: Any, **kwargs: Any) -> StartedPerformer:
        return StartedPerformer(handle="pod-r3", endpoint="http://127.0.0.1:9")

    async def stop(self, handle: str, **kwargs: Any) -> None:
        self.stops += 1
        self.started.set()
        await asyncio.sleep(0.5)
        self.finished += 1

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        return []


class _Busy:
    async def post_job(self, payload: Any) -> Any:
        return SimpleNamespace(accepted=False, reason="busy", detail="slot")

    async def aclose(self) -> None:
        return None


async def test_a_refused_dispatch_stop_survives_a_double_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_wait_ready(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(hps.performer_lifecycle, "wait_ready", fake_wait_ready)
    runtime = _SlowStop()
    config = PerformerEndpointConfig(id="perf-r3", mode="ephemeral", image="img", roles=["implementer"])
    service = HTTPPerformerService(config, client=cast("Any", _Busy()), runtime=cast("Any", runtime))
    workspace = WorkspaceInfo(path=None, repo_url="https://github.com/x/y", branch="main", github_token=SECRET)
    card = {"id": "card-r3", "role": "implementer", "backend": "claude_code", "persona_instructions": "x"}

    task = asyncio.ensure_future(service.dispatch_card(card, workspace))
    await asyncio.wait_for(runtime.started.wait(), WAIT)
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await asyncio.sleep(0.8)
    assert runtime.stops == 1
    assert runtime.finished == 1


# --- 4: a Docker stop by label collects the log before the stop -------------------------


class _LabelRuntime:
    """A runtime with no ``find_session``: the stop is confirmed by label, as Docker's is."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def iter_logs(self, handle: str) -> Any:
        self.events.append("logs")
        yield b"docker output\n"

    async def stop(self, handle: str, **kwargs: Any) -> None:
        self.events.append("stop")

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        return []


async def test_a_docker_stop_by_label_collects_the_log_before_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _LabelRuntime()
    service = _service(tmp_path, runtime)
    final = tmp_path / "run-logs" / f"{SESSION}.log"
    seen: dict[str, bool] = {}

    async def label_stop(rt: Any, session_id: str) -> bool:
        rt.events.append("label-stop")
        seen["present"] = final.exists()
        return True

    monkeypatch.setattr(hps, "_stop_docker_session_confirmed", label_stop)

    assert await service._confirm_stopped(SESSION) is True

    assert runtime.events == ["logs", "label-stop"]
    assert seen["present"] is True
    assert final.read_bytes() == b"docker output\n"


# --- 5: the capture bound and the drain fit within twenty seconds ------------------------


def test_the_capture_bound_and_the_drain_fit_within_twenty_seconds() -> None:
    assert hps._RUN_LOG_TEARDOWN_SECONDS + hps._TRANSCRIPT_TEARDOWN_SECONDS <= 20.0


# --- 6: an abandoned writer lets go of the job's secret values --------------------------


def test_an_abandoned_writer_drops_its_secret_values(tmp_path: Path) -> None:
    writer = RunLogWriter(tmp_path / "run-logs", run_id="r6", max_bytes=8192, secrets=(SECRET,))
    assert writer.holds_secrets is True
    writer.abort()
    assert writer.holds_secrets is False


# --- 8: a committed log is known without touching the disk on the snapshot path ---------


async def test_a_committed_log_is_known_without_a_disk_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _LabelRuntime()
    service = _service(tmp_path, runtime)
    await service._cleanup_ephemeral_job_by_id(SESSION)
    assert service._run_logs is not None
    assert service._run_logs.is_committed(SESSION) is True

    def no_disk(root: Path, run_id: str) -> bool:
        raise AssertionError("the snapshot path touched the disk")

    monkeypatch.setattr(dashboard_store, "run_log_exists", no_disk)
    daemon = SimpleNamespace(state={"run_log_collector": service._run_logs})
    assert dashboard_store.DashboardStore._full_log_available(daemon, SESSION) is True
    assert dashboard_store.DashboardStore._full_log_available(daemon, "never-ran") is False
