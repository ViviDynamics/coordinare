"""Spec 132: the runtimes stream a container's or Pod's complete output in chunks.

Docker is driven through a fake subprocess, Kubernetes through a fake CoreV1 whose
log call returns an unbuffered response. Neither ever loads the whole log.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.kubernetes_runtime import KubernetesRuntime


class _FakeStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = _FakeStdout(chunks)
        self.returncode: int | None = None
        self.killed = False

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _patch_docker(monkeypatch: pytest.MonkeyPatch, chunks: list[bytes]) -> tuple[_FakeProc, list[tuple[Any, ...]], list[dict[str, Any]]]:
    proc = _FakeProc(chunks)
    calls: list[tuple[Any, ...]] = []
    kwargs_seen: list[dict[str, Any]] = []

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(args)
        kwargs_seen.append(kwargs)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return proc, calls, kwargs_seen


async def test_docker_logs_stream_every_chunk_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [b"first\n", b"\xe2\x9c", b"\x94 second\n"]
    proc, calls, kwargs_seen = _patch_docker(monkeypatch, chunks)

    received = [chunk async for chunk in DockerRuntime().iter_logs("cid-132")]

    assert received == chunks
    assert calls == [("docker", "logs", "cid-132")]
    assert kwargs_seen[0]["stderr"] == asyncio.subprocess.STDOUT
    assert proc.stdout.read_sizes and all(size > 0 for size in proc.stdout.read_sizes)
    assert proc.killed is False


async def test_docker_logs_are_killed_when_the_reader_stops_early(monkeypatch: pytest.MonkeyPatch) -> None:
    proc, _, _ = _patch_docker(monkeypatch, [b"a\n", b"b\n", b"c\n"])
    stream = DockerRuntime().iter_logs("cid-132")

    assert await stream.__anext__() == b"a\n"
    await stream.aclose()

    assert proc.killed is True


class _FakeStream:
    """What a urllib3 response gives back: a chunk iterator and a close."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False
        self.amounts: list[int] = []

    def stream(self, amount: int) -> Any:
        self.amounts.append(amount)
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _FakeCoreV1:
    def __init__(self, response: _FakeStream | None = None, error: BaseException | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def read_namespaced_pod_log(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


async def test_kubernetes_logs_are_read_unbuffered_and_passed_through_as_bytes() -> None:
    body = [b"step one\n", "café ✔\n".encode(), b"done\n"]
    response = _FakeStream(body)
    core = _FakeCoreV1(response)
    runtime = KubernetesRuntime(core_v1=core, owner="owner-132")

    received = [chunk async for chunk in runtime.iter_logs("pod-132")]

    assert received == body
    assert core.calls[0]["name"] == "pod-132"
    assert core.calls[0]["_preload_content"] is False
    assert "tail_lines" not in core.calls[0]
    assert response.amounts and all(amount > 0 for amount in response.amounts)
    assert response.closed is True


async def test_kubernetes_logs_are_closed_when_the_reader_stops_early() -> None:
    response = _FakeStream([b"a\n", b"b\n"])
    runtime = KubernetesRuntime(core_v1=_FakeCoreV1(response), owner="owner-132")
    stream = runtime.iter_logs("pod-132")

    assert await stream.__anext__() == b"a\n"
    await stream.aclose()

    assert response.closed is True


async def test_a_kubernetes_log_error_reaches_the_reader() -> None:
    runtime = KubernetesRuntime(core_v1=_FakeCoreV1(error=RuntimeError("pod is gone")), owner="owner-132")

    with pytest.raises(RuntimeError, match="pod is gone"):
        [chunk async for chunk in runtime.iter_logs("pod-132")]
