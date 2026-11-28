"""447: the egress proxy, the only way out of a sandboxed QAre run that has an allowlist.

Real sockets on loopback throughout. The proxy's ``connect`` is replaced so an allowlisted
name reaches a local server, and so a refused host can be shown never to be dialled.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from performer import egress_proxy
from performer.egress_proxy import EgressProxy, allowed, parse_allowlist

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """A stand-in for an outside host: echoes bytes, or answers one plain HTTP request."""
    first = await reader.read(65536)
    if first.startswith(b"GET "):
        body = first.split(b"\r\n", 1)[0]
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
    else:
        writer.write(first)
    await writer.drain()
    writer.close()


@contextlib.asynccontextmanager
async def _server(handler: Callable[..., Awaitable[None]]) -> AsyncIterator[int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def _proxy(allow: list[str], connect: Connector) -> AsyncIterator[int]:
    proxy = EgressProxy(parse_allowlist(",".join(allow)), connect=connect)
    async with _server(proxy.handle) as port:
        yield port


async def _ask(port: int, head: bytes, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head)
    await writer.drain()
    if then:
        line = await reader.readuntil(b"\r\n\r\n")
        if not line.startswith(b"HTTP/1.1 200"):
            writer.close()
            return line
        writer.write(then)
        await writer.drain()
        out = line + await asyncio.wait_for(reader.read(65536), 5)
    else:
        out = await asyncio.wait_for(reader.read(65536), 5)
    writer.close()
    return out


def _routing(echo_port: int, dialled: list[tuple[str, int]]) -> Connector:
    async def connect(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        dialled.append((host, port))
        return await asyncio.open_connection("127.0.0.1", echo_port)

    return connect


# -- the allowlist ------------------------------------------------------------------------


def test_the_allowlist_is_exact_names_without_case_or_a_trailing_dot() -> None:
    allow = parse_allowlist(" API.github.com , ,pypi.org.,[::1]")
    assert allow == frozenset({"api.github.com", "pypi.org", "::1"})
    assert allowed("api.github.com", allow)
    assert allowed("Api.GitHub.com.", allow)
    assert allowed("[::1]", allow)
    # No suffix or prefix matching: a look-alike is another host.
    assert not allowed("evil-api.github.com", allow)
    assert not allowed("api.github.com.evil.example", allow)
    assert not allowed("github.com", allow)
    assert not allowed("", allow)


# -- refused ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connect_to_a_host_outside_the_allowlist_is_refused_and_never_dialled() -> None:
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo, _proxy(["api.github.com"], _routing(echo, dialled)) as port:
        answer = await _ask(port, b"CONNECT blocked.example:443 HTTP/1.1\r\nHost: blocked.example:443\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403")
    assert b"blocked.example" in answer
    assert dialled == []


@pytest.mark.asyncio
async def test_a_plain_request_to_a_host_outside_the_allowlist_is_refused_and_never_dialled() -> None:
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo, _proxy(["api.github.com"], _routing(echo, dialled)) as port:
        answer = await _ask(port, b"GET http://blocked.example/x HTTP/1.1\r\nHost: blocked.example\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 403")
    assert dialled == []


@pytest.mark.parametrize("head", [
    b"GET /relative HTTP/1.1\r\nHost: api.github.com\r\n\r\n",  # not a proxy request: Host is not trusted
    b"GET https://api.github.com/ HTTP/1.1\r\n\r\n",  # https goes through CONNECT only
    b"CONNECT api.github.com HTTP/1.1\r\n\r\n",  # no port
    b"CONNECT api.github.com:99999 HTTP/1.1\r\n\r\n",
    b"CONNECT bad host:443 HTTP/1.1\r\n\r\n",
    b"nonsense\r\n\r\n",
])
@pytest.mark.asyncio
async def test_anything_but_a_well_formed_proxy_request_is_refused(head: bytes) -> None:
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo, _proxy(["api.github.com"], _routing(echo, dialled)) as port:
        answer = await _ask(port, head)
    assert answer.startswith((b"HTTP/1.1 400", b"HTTP/1.1 403")), answer
    assert dialled == []


@pytest.mark.asyncio
async def test_a_client_that_never_sends_a_request_is_dropped() -> None:
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo:
        proxy = EgressProxy(parse_allowlist("api.github.com"), connect=_routing(echo, dialled), head_timeout=0.2)
        async with _server(proxy.handle) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
    assert dialled == []


# -- allowed ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connect_to_an_allowed_host_is_relayed() -> None:
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo, _proxy(["api.github.com"], _routing(echo, dialled)) as port:
        answer = await _ask(port, b"CONNECT API.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n\r\n",
                            then=b"tls bytes")
    assert answer.startswith(b"HTTP/1.1 200")
    assert answer.endswith(b"tls bytes")
    assert dialled == [("api.github.com", 443)]


@pytest.mark.asyncio
async def test_a_plain_request_to_an_allowed_host_is_forwarded_in_origin_form() -> None:
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo, _proxy(["llm"], _routing(echo, dialled)) as port:
        answer = await _ask(
            port,
            b"GET http://llm:4000/v1/models?x=1 HTTP/1.1\r\nHost: llm:4000\r\n"
            b"Proxy-Authorization: Basic eA==\r\nProxy-Connection: keep-alive\r\n\r\n",
        )
    assert dialled == [("llm", 4000)]
    assert answer.startswith(b"HTTP/1.1 200")
    assert answer.endswith(b"GET /v1/models?x=1 HTTP/1.1")


@pytest.mark.asyncio
async def test_an_allowed_host_that_cannot_be_reached_is_a_bad_gateway() -> None:
    async def unreachable(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise OSError(f"no route to {host}:{port}")

    async with _proxy(["api.github.com"], unreachable) as port:
        answer = await _ask(port, b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n")
    assert answer.startswith(b"HTTP/1.1 502")


# -- the command line ---------------------------------------------------------------------


def test_the_command_line_needs_an_allowlist() -> None:
    with pytest.raises(SystemExit):
        egress_proxy.main(["--port", "3128"])
    with pytest.raises(SystemExit):
        egress_proxy.main(["--allow", " , "])


@pytest.mark.asyncio
async def test_serve_listens_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    task = asyncio.create_task(egress_proxy.serve(parse_allowlist("api.github.com"), "127.0.0.1", 0))
    for _ in range(100):
        if "listening" in capsys.readouterr().err:
            break
        await asyncio.sleep(0.02)
    else:
        pytest.fail("the proxy never said it was listening")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
