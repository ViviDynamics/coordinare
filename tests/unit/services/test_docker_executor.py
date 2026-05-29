"""Spec 076 T043 — DockerExecutor unit tests.

Covers happy path, timeout, daemon-unreachable, malformed JSON, and
label/timestamp parsing.  Subprocess is fully mocked; no actual Docker
daemon contacted.
"""
from __future__ import annotations

import asyncio

import pytest

from coordinare.services.docker_executor import (
    ContainerInfo,
    DockerExecutor,
    DockerUnreachableError,
    _parse_docker_created_at,
    _parse_docker_label_string,
)

# ---------------------------------------------------------------------------
# Label / timestamp parsing
# ---------------------------------------------------------------------------


def test_parse_label_string_returns_dict() -> None:
    raw = "coordinare.session_id=abc,coordinare.card_id=PVTI_X"
    assert _parse_docker_label_string(raw) == {
        "coordinare.session_id": "abc",
        "coordinare.card_id": "PVTI_X",
    }


def test_parse_label_string_empty_input() -> None:
    assert _parse_docker_label_string("") == {}
    assert _parse_docker_label_string(None) == {}


def test_parse_label_string_handles_whitespace_and_empty_pieces() -> None:
    raw = "  k1=v1 ,  ,k2=v2  "
    assert _parse_docker_label_string(raw) == {"k1": "v1", "k2": "v2"}


def test_parse_created_at_docker_format() -> None:
    dt = _parse_docker_created_at("2026-05-28 22:06:50 +0000 UTC")
    assert dt.year == 2026
    assert dt.hour == 22


def test_parse_created_at_iso_format() -> None:
    dt = _parse_docker_created_at("2026-05-28T22:06:50+0000")
    assert dt.year == 2026


def test_parse_created_at_falls_back_on_garbage() -> None:
    # Should not raise; should return SOME datetime
    dt = _parse_docker_created_at("not a timestamp")
    assert dt is not None


# ---------------------------------------------------------------------------
# list_containers_by_label
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, rc: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = rc
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


def _make_subprocess_factory(rc: int, stdout: bytes, stderr: bytes = b""):
    async def _create(*args, **kwargs):
        return _FakeProc(rc, stdout, stderr)
    return _create


@pytest.mark.asyncio
async def test_list_containers_happy_path(monkeypatch) -> None:
    stdout = (
        b'{"ID":"abc123","Names":"compassionate_meitner","Image":"coordinare-performer:full",'
        b'"CreatedAt":"2026-05-28 22:06:50 +0000 UTC",'
        b'"Labels":"coordinare.session_id=sess-1,coordinare.card_id=PVTI_X"}\n'
        b'{"ID":"def456","Names":"keen_poitras","Image":"coordinare-performer:full",'
        b'"CreatedAt":"2026-05-28 22:07:41 +0000 UTC",'
        b'"Labels":"coordinare.session_id=sess-2,coordinare.card_id=PVTI_X"}'
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, stdout),
    )

    executor = DockerExecutor()
    containers = await executor.list_containers_by_label({"coordinare.spec_version": "076"})

    assert len(containers) == 2
    assert containers[0].container_id == "abc123"
    assert containers[0].name == "compassionate_meitner"
    assert containers[0].labels["coordinare.session_id"] == "sess-1"
    assert containers[1].container_id == "def456"
    assert containers[1].labels["coordinare.session_id"] == "sess-2"


@pytest.mark.asyncio
async def test_list_containers_handles_empty_stdout(monkeypatch) -> None:
    """No matching containers → empty list, no error."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, b""),
    )
    executor = DockerExecutor()
    result = await executor.list_containers_by_label({"coordinare.spec_version": "076"})
    assert result == []


@pytest.mark.asyncio
async def test_list_containers_tolerates_malformed_lines(monkeypatch) -> None:
    """A single malformed JSON line MUST NOT crash the pass."""
    stdout = (
        b'{"ID":"abc","Names":"a","Image":"x","CreatedAt":"","Labels":""}\n'
        b'this-is-not-json\n'
        b'{"ID":"def","Names":"b","Image":"x","CreatedAt":"","Labels":""}'
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, stdout),
    )
    executor = DockerExecutor()
    containers = await executor.list_containers_by_label({})
    assert len(containers) == 2
    assert {c.container_id for c in containers} == {"abc", "def"}


@pytest.mark.asyncio
async def test_list_containers_raises_on_daemon_unreachable(monkeypatch) -> None:
    """rc=125 from Docker MUST surface as DockerUnreachableError."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(125, b"", b"Cannot connect to the Docker daemon"),
    )
    executor = DockerExecutor()
    with pytest.raises(DockerUnreachableError, match="daemon unreachable"):
        await executor.list_containers_by_label({})


@pytest.mark.asyncio
async def test_list_containers_raises_on_missing_docker_binary(monkeypatch) -> None:
    """If `docker` is not on PATH, surface as DockerUnreachableError."""

    async def _raise_fnf(*args, **kwargs):
        raise FileNotFoundError("no docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_fnf)
    executor = DockerExecutor()
    with pytest.raises(DockerUnreachableError, match="docker CLI not found"):
        await executor.list_containers_by_label({})


@pytest.mark.asyncio
async def test_list_containers_raises_on_timeout(monkeypatch) -> None:
    """A subprocess that never returns within `timeout` MUST raise
    DockerUnreachableError so the reconciliation pass can fail-closed."""

    class _SlowProc:
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def _create_slow(*args, **kwargs):
        return _SlowProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create_slow)
    executor = DockerExecutor()
    with pytest.raises(DockerUnreachableError, match="timed out"):
        await executor.list_containers_by_label({}, timeout=0.1)


# ---------------------------------------------------------------------------
# stop_container
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_container_returns_true_on_success(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, b"abc"),
    )
    executor = DockerExecutor()
    assert await executor.stop_container("abc") is True


@pytest.mark.asyncio
async def test_stop_container_falls_back_to_kill_on_stop_failure(monkeypatch) -> None:
    """If `docker stop` fails, try `docker kill` (best-effort per FR-004)."""
    call_count = [0]

    async def _create(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _FakeProc(1, b"", b"stop failed")
        return _FakeProc(0, b"abc", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    executor = DockerExecutor()
    result = await executor.stop_container("abc")
    assert result is True
    assert call_count[0] == 2


@pytest.mark.asyncio
async def test_stop_container_returns_false_if_kill_also_fails(monkeypatch) -> None:
    """Both stop and kill failing → return False; caller logs reap_failed
    but proceeds (the daemon must not wedge on a single sticky container)."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(1, b"", b"failure"),
    )
    executor = DockerExecutor()
    assert await executor.stop_container("abc") is False


# ---------------------------------------------------------------------------
# port_of
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_port_of_returns_host_port(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, b"0.0.0.0:55555"),
    )
    executor = DockerExecutor()
    port = await executor.port_of("abc")
    assert port == 55555


@pytest.mark.asyncio
async def test_port_of_returns_none_when_port_not_exposed(monkeypatch) -> None:
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(1, b"", b"no port"),
    )
    executor = DockerExecutor()
    port = await executor.port_of("abc")
    assert port is None


@pytest.mark.asyncio
async def test_probe_healthz_returns_false_when_port_unresolvable(monkeypatch) -> None:
    """If port_of returns None, probe_healthz short-circuits to False
    without an HTTP attempt."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(1, b"", b"no port"),
    )
    executor = DockerExecutor()
    assert await executor.probe_healthz("abc") is False


@pytest.mark.asyncio
async def test_probe_healthz_returns_false_on_http_error(monkeypatch) -> None:
    """A network error inside probe_healthz MUST return False (caller
    treats False as un-adoptable → reap)."""
    import httpx

    # Mock port_of to return a valid port
    async def _port_create(*args, **kwargs):
        return _FakeProc(0, b"0.0.0.0:55555", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _port_create)

    # Make httpx unreachable
    def _broken_get(*a, **kw):
        raise httpx.ConnectError("connection refused")

    class _BrokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **kw):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _BrokenClient())
    executor = DockerExecutor()
    result = await executor.probe_healthz("abc", timeout=1.0)
    assert result is False


@pytest.mark.asyncio
async def test_probe_healthz_returns_true_on_200(monkeypatch) -> None:
    """Happy path: port resolves, /healthz returns 200, probe → True."""
    import httpx

    async def _port_create(*args, **kwargs):
        return _FakeProc(0, b"0.0.0.0:55555", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _port_create)

    class _OkResponse:
        status_code = 200

    class _OkClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            assert "/healthz" in url
            return _OkResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _OkClient())
    executor = DockerExecutor()
    result = await executor.probe_healthz("abc", auth_token="tok")
    assert result is True


@pytest.mark.asyncio
async def test_probe_healthz_includes_bearer_auth_when_token_given(monkeypatch) -> None:
    """The probe MUST forward the auth token as a Bearer header."""
    import httpx

    async def _port_create(*args, **kwargs):
        return _FakeProc(0, b"0.0.0.0:55555", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _port_create)

    captured_headers: dict[str, str] = {}

    class _Response:
        status_code = 200

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            captured_headers.update(headers or {})
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    executor = DockerExecutor()
    await executor.probe_healthz("abc", auth_token="my-token")
    assert captured_headers.get("Authorization") == "Bearer my-token"


@pytest.mark.asyncio
async def test_port_of_handles_unparseable_output(monkeypatch) -> None:
    """If `docker port` returns something we can't parse as int, return None."""
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, b"garbage_no_colon"),
    )
    executor = DockerExecutor()
    assert await executor.port_of("abc") is None


@pytest.mark.asyncio
async def test_list_containers_skips_entries_with_no_container_id(monkeypatch) -> None:
    """Malformed entries missing ID should be skipped, not crash."""
    stdout = (
        b'{"ID":"","Names":"a","Image":"x","CreatedAt":"","Labels":""}\n'
        b'{"ID":"good","Names":"b","Image":"x","CreatedAt":"","Labels":""}'
    )
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        _make_subprocess_factory(0, stdout),
    )
    executor = DockerExecutor()
    containers = await executor.list_containers_by_label({})
    assert len(containers) == 1
    assert containers[0].container_id == "good"


def test_container_info_is_frozen() -> None:
    """ContainerInfo MUST be a frozen dataclass to prevent accidental
    mutation during the reconciliation pass."""
    info = ContainerInfo(
        container_id="abc",
        name="x",
        image="y",
        started_at=_parse_docker_created_at(None),
    )
    with pytest.raises(AttributeError):
        info.container_id = "different"  # type: ignore[misc]
