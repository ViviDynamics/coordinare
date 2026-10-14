"""Spec 208: the daemon writes nothing into a performer-writable directory.

``performer_log_dir`` is mounted read-write into ephemeral performers, so a file the
container planted there (a symlink or a hard link) must never be written through by
the daemon, and must never be read back as trusted content. These tests plant a link
at every former dump path and at the attempt telemetry path, then check that the
victim file is untouched and nothing new lands in the performer directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, cast

import pytest
from kubernetes.client.rest import ApiException

import coordinare.__main__ as app_main
from coordinare.attempt_log import AttemptLog
from coordinare.models.performer_endpoint import (
    PerformerEndpointConfig,
    VolumeMount,
)
from coordinare.routing_config_service import (
    ROUTING_MAX_BYTES,
    RoutingLocation,
    create_routing_entry,
    read_routing,
)
from coordinare.services import http_performer_service as hps
from coordinare.services import performer_lifecycle as lifecycle
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.services.kubernetes_runtime import KubernetesRuntime
from coordinare.services.mounted_file import mounted_file_refusal, read_mounted_file

GH_TOKEN = "ghp_" + "d" * 36
CONTAINER = "abcdef1234567890"
CONTAINER_SHORT = "abcdef123456"
PERFORMER = "perf-208"
VICTIM_TEXT = "KEEP: the daemon must not write here\n"


def _victim(tmp: Path) -> Path:
    """A file outside the performer directory, standing in for whatever a link points at."""
    victim = tmp / "victim" / "target.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text(VICTIM_TEXT)
    return victim


def _plant(performer_dir: Path, name: str, victim: Path, *, hard: bool) -> Path:
    """Plant a symlink or a hard link at *name* inside the performer directory."""
    performer_dir.mkdir(parents=True, exist_ok=True)
    planted = performer_dir / name
    if hard:
        os.link(victim, planted)
    else:
        planted.symlink_to(victim)
    return planted


def _snapshot(directory: Path) -> dict[str, str]:
    """Names and link targets (or content) of everything in *directory*, for a before/after diff."""
    out: dict[str, str] = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_symlink():
            out[entry.name] = f"link->{os.readlink(entry)}"
        else:
            out[entry.name] = entry.read_text()
    return out


class _FakeLinkRuntime:
    """A runtime that records what ``stop`` was called with and never touches disk."""

    def __init__(self) -> None:
        self.stop_calls: list[tuple[str, dict[str, Any]]] = []

    async def stop(self, handle: str, **kwargs: Any) -> None:
        self.stop_calls.append((handle, kwargs))


class _Client:
    async def aclose(self) -> None:
        return None


def _ephemeral_service(performer_dir: Path, runtime: Any) -> HTTPPerformerService:
    config = PerformerEndpointConfig(
        id=PERFORMER,
        mode="ephemeral",
        image="performer:full",
        roles=["implementer"],
        volumes=[
            VolumeMount(
                host_path=performer_dir,
                container_path=PurePosixPath("/var/log/performer"),
                mode="rw",
            ),
        ],
    )
    return HTTPPerformerService(config, runtime=cast("Any", runtime))


def _job() -> hps._EphemeralJob:
    return hps._EphemeralJob(
        container_id=CONTAINER,
        endpoint="http://127.0.0.1:9",
        client=cast("Any", _Client()),
        run_id="sess-208",
    )


# ---------------------------------------------------------------------------
# Former dump sites: nothing is written, and no link is followed.
# ---------------------------------------------------------------------------

# The former Docker dump name, and the former Kubernetes dump name.
_DOCKER_DUMP_NAME = f"{PERFORMER}.{CONTAINER_SHORT}.container.log"
_KUBE_DUMP_NAME = f"{PERFORMER}.log"


@pytest.mark.parametrize("hard", [False, True], ids=["symlink", "hard-link"])
def test_docker_dump_path_is_never_written_through(tmp_path: Path, monkeypatch, hard: bool) -> None:
    """The Docker teardown no longer dumps ``docker logs``, so a planted link is never opened."""
    victim = _victim(tmp_path)
    performer_dir = tmp_path / "performer-logs"
    _plant(performer_dir, _DOCKER_DUMP_NAME, victim, hard=hard)
    before = _snapshot(performer_dir)
    docker_calls: list[str] = []

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        docker_calls.append(args[0])
        return 0, "docker logs would be here\n", ""

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)
    asyncio.run(lifecycle.stop(CONTAINER, timeout_s=3))

    assert docker_calls == ["stop"], "the teardown must not read docker logs any more"
    assert victim.read_text() == VICTIM_TEXT
    assert _snapshot(performer_dir) == before


@pytest.mark.parametrize("hard", [False, True], ids=["symlink", "hard-link"])
def test_kubernetes_dump_path_is_never_written_through(tmp_path: Path, hard: bool) -> None:
    """The Kubernetes teardown no longer reads or writes pod logs into performer_log_dir."""
    victim = _victim(tmp_path)
    performer_dir = tmp_path / "performer-logs"
    _plant(performer_dir, _KUBE_DUMP_NAME, victim, hard=hard)
    before = _snapshot(performer_dir)
    reads: list[str] = []

    class FakeApi:
        def read_namespaced_pod_log(self, *, name: str, namespace: str, **_: Any) -> str:
            reads.append(name)
            return "pod log\n"

        def delete_namespaced_pod(self, **_: Any) -> None:
            raise ApiException(status=404)

    runtime = KubernetesRuntime(core_v1=FakeApi())
    asyncio.run(runtime.stop("pod-208", timeout_s=1))

    assert reads == [], "the Kubernetes teardown must not read pod logs any more"
    assert victim.read_text() == VICTIM_TEXT
    assert _snapshot(performer_dir) == before


def test_service_teardown_passes_no_log_dir_to_the_runtime(tmp_path: Path) -> None:
    """The daemon no longer hands the performer mount to the runtime at stop time."""
    performer_dir = tmp_path / "performer-logs"
    performer_dir.mkdir()
    runtime = _FakeLinkRuntime()
    service = _ephemeral_service(performer_dir, runtime)
    asyncio.run(service._cleanup_ephemeral_job(_job(), collect=False))

    assert runtime.stop_calls == [(CONTAINER, {})]
    assert list(performer_dir.iterdir()) == []


def test_no_dump_file_is_written_for_a_stopped_container(tmp_path: Path, monkeypatch) -> None:
    """A clean teardown leaves the performer directory exactly as the container left it."""
    performer_dir = tmp_path / "performer-logs"
    performer_dir.mkdir()

    async def fake_run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        return 0, "", ""

    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)
    asyncio.run(lifecycle.stop(CONTAINER))
    assert list(performer_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# Attempt telemetry: daemon-only directory, and a planted link is refused.
# ---------------------------------------------------------------------------


def _today_name() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d") + ".jsonl"


@pytest.mark.parametrize("hard", [False, True], ids=["symlink", "hard-link"])
def test_planted_attempt_file_is_not_written_through(tmp_path: Path, hard: bool) -> None:
    victim = _victim(tmp_path)
    attempts_dir = tmp_path / "performer-logs" / "attempts"
    _plant(attempts_dir, _today_name(), victim, hard=hard)
    before = _snapshot(attempts_dir)

    log = AttemptLog(log_dir=attempts_dir)
    log.open_attempt(task_id="card-208", routing_reason="default_policy")

    assert victim.read_text() == VICTIM_TEXT
    assert _snapshot(attempts_dir) == before


def test_attempt_row_is_written_owner_only(tmp_path: Path) -> None:
    attempts_dir = tmp_path / "daemon" / "logs" / "attempts"
    AttemptLog(log_dir=attempts_dir).open_attempt(task_id="card-208", routing_reason="x")
    (row_file,) = attempts_dir.glob("*.jsonl")
    assert stat.S_IMODE(row_file.stat().st_mode) == 0o600
    assert json.loads(row_file.read_text().splitlines()[0])["task_id"] == "card-208"


def test_attempt_dir_is_not_the_performer_directory(tmp_path: Path) -> None:
    """With performer_log_dir set, attempt telemetry resolves beside the config, not in the mount."""
    performer_dir = tmp_path / "performer-logs"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = SimpleNamespace(
        state_file_path=state_dir / "coordinare.state.json",
        performer_log_dir=performer_dir,
        performer_endpoints=[],
        env_cache_root=state_dir / "env-caches",
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    resolved = app_main._resolve_attempt_log_dir(
        cast("Any", config),
        config_dir / "config.yaml",
    )
    assert resolved == config_dir / "logs" / "attempts"
    assert not resolved.is_relative_to(performer_dir)


def test_reopened_attempt_from_the_performer_dir_writes_daemon_side(tmp_path: Path) -> None:
    """A pre-208 persisted path under performer_log_dir is not reused for the end row."""
    performer_dir = tmp_path / "performer-logs" / "attempts"
    daemon_dir = tmp_path / "daemon" / "logs" / "attempts"
    old_path = performer_dir / _today_name()
    log = AttemptLog(log_dir=daemon_dir)
    log.reopen_attempt("att-1", "card-208", old_path)
    log.close_attempt("att-1", verdict="pass", verdict_source="qa_role")

    assert not old_path.exists()
    assert not performer_dir.exists()
    (row_file,) = daemon_dir.glob("*.jsonl")
    assert json.loads(row_file.read_text().splitlines()[0])["verdict"] == "pass"


# ---------------------------------------------------------------------------
# Reads from performer-writable paths: refused when linked, odd, or oversized.
# ---------------------------------------------------------------------------


def test_regular_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "table.yaml"
    path.write_bytes(b"entries: []\n")
    assert read_mounted_file(path, max_bytes=1024, mounts=(path.parent,)) == b"entries: []\n"
    assert mounted_file_refusal(path, mounts=(path.parent,)) is None


def test_symlink_is_refused(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    link = tmp_path / "table.yaml"
    link.symlink_to(victim)
    assert read_mounted_file(link, max_bytes=1024, mounts=(link.parent,)) is None
    assert mounted_file_refusal(link, mounts=(link.parent,)) == "symlink"


def test_hard_link_is_refused(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    link = tmp_path / "table.yaml"
    os.link(victim, link)
    assert read_mounted_file(link, max_bytes=1024, mounts=(link.parent,)) is None
    assert mounted_file_refusal(link, mounts=(link.parent,)) == "hard linked"


def test_fifo_is_refused_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "table.yaml"
    os.mkfifo(fifo)
    assert read_mounted_file(fifo, max_bytes=1024, mounts=(fifo.parent,)) is None


def test_directory_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "table.yaml"
    directory.mkdir()
    assert read_mounted_file(directory, max_bytes=1024, mounts=(directory.parent,)) is None


def test_oversize_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "table.yaml"
    path.write_bytes(b"x" * 11)
    assert read_mounted_file(path, max_bytes=10, mounts=(path.parent,)) is None
    path.write_bytes(b"x" * 10)
    assert read_mounted_file(path, max_bytes=10, mounts=(path.parent,)) == b"x" * 10


def test_missing_file_is_none_not_an_error(tmp_path: Path) -> None:
    assert read_mounted_file(tmp_path / "absent.yaml", max_bytes=1024, mounts=(tmp_path,)) is None


# ---------------------------------------------------------------------------
# Routing table (a file an endpoint may mount read-write): links are never read or replaced.
# ---------------------------------------------------------------------------

_ROUTING_BODY = "entries:\n  - backend: hermes\n    note: " + GH_TOKEN + "\n"


def _location(path: Path) -> RoutingLocation:
    # The routing file sits in a performer mount (its parent), so links below it are refused.
    return RoutingLocation(
        endpoint_id="endpoint-208",
        host_path=path,
        container_path="/r.yaml",
        mounts=(path.parent,),
    )


def test_routing_table_is_read_and_its_secrets_redacted(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_text(_ROUTING_BODY)
    view = read_routing(_location(path))
    assert view.routing_available is True
    assert view.entries and GH_TOKEN not in json.dumps(view.entries)
    assert path.read_text() == _ROUTING_BODY


def test_planted_routing_symlink_is_not_read_or_replaced(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    link = tmp_path / "routing.yaml"
    link.symlink_to(victim)
    location = _location(link)

    view = read_routing(location)
    assert view.routing_available is False
    assert view.entries == []

    result = create_routing_entry(
        location,
        {"backend": "hermes", "note": "x"},
        base_hash="sha256:" + "0" * 64,
    )
    assert result.ok is False
    assert victim.read_text() == VICTIM_TEXT
    assert link.is_symlink()


def test_planted_routing_hard_link_is_not_written(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    link = tmp_path / "routing.yaml"
    os.link(victim, link)
    result = create_routing_entry(
        _location(link),
        {"backend": "hermes", "note": "x"},
        base_hash="sha256:" + "0" * 64,
    )
    assert result.ok is False
    assert victim.read_text() == VICTIM_TEXT


def test_oversize_routing_table_is_not_read(tmp_path: Path) -> None:
    path = tmp_path / "routing.yaml"
    path.write_bytes(b"#" * (ROUTING_MAX_BYTES + 1))
    view = read_routing(_location(path))
    assert view.routing_available is False
    assert view.entries == []
