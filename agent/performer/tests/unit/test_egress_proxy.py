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
    "CONNECT api.github.com:\u00b2 HTTP/1.1\r\n\r\n".encode("latin-1"),  # a digit to isdigit(), not to int()
    b"GET http://[bad/ HTTP/1.1\r\n\r\n",  # urlsplit raises on this
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


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.asyncio
async def test_serve_relays_until_cancelled_without_an_idle_exit() -> None:
    port = _free_port()
    task = asyncio.create_task(egress_proxy.serve(parse_allowlist("api.github.com"), "127.0.0.1", port))
    for _ in range(100):
        try:
            answer = await _ask(port, b"CONNECT blocked.example:443 HTTP/1.1\r\n\r\n")
            break
        except OSError:
            await asyncio.sleep(0.02)
    else:
        pytest.fail("the proxy never listened")
    assert answer.startswith(b"HTTP/1.1 403")
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_with_an_idle_exit_serve_ends_once_nothing_connects() -> None:
    """The job's entry point connects while the job runs; after it is gone, the proxy goes too."""
    port = _free_port()
    task = asyncio.create_task(
        egress_proxy.serve(parse_allowlist("api.github.com"), "127.0.0.1", port, idle_exit=0.4),
    )
    for _ in range(4):  # a heartbeat every 0.2 s keeps it up
        await asyncio.sleep(0.2)
        with contextlib.suppress(OSError):
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
        assert not task.done()
    await asyncio.wait_for(task, 3)


# -- relays end -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plain_request_closes_both_ways_so_the_connection_is_never_reused() -> None:
    """A keep-alive client may not send a second request, for another host, down this connection."""
    seen: list[bytes] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    dialled: list[tuple[str, int]] = []
    async with _server(upstream) as port_up, _proxy(["a.allowed"], _routing(port_up, dialled)) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET http://a.allowed/x HTTP/1.1\r\nHost: a.allowed\r\nConnection: keep-alive\r\n"
                     b"Keep-Alive: timeout=5\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 5)  # ends: the proxy closed the connection
        writer.close()
    assert response.endswith(b"ok")
    head = seen[0].lower()
    assert b"connection: close" in head
    assert b"keep-alive" not in head
    assert dialled == [("a.allowed", 80)]


@pytest.mark.asyncio
async def test_a_tunnel_left_half_open_is_closed_after_the_grace() -> None:
    release = asyncio.Event()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await release.wait()  # never answers, never closes, until the test is over
        writer.close()

    dialled: list[tuple[str, int]] = []
    async with _server(silent) as port_up:
        proxy = EgressProxy(parse_allowlist("api.github.com"), connect=_routing(port_up, dialled), half_close_grace=0.2)
        async with _server(proxy.handle) as port:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"CONNECT api.github.com:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 3) == b""
            writer.close()
            for _ in range(100):
                if proxy.active == 0:
                    break
                await asyncio.sleep(0.01)
            assert proxy.active == 0
        release.set()


@pytest.mark.asyncio
async def test_a_plain_request_goes_upstream_with_the_host_it_was_checked_for() -> None:
    """A Host header naming another site may not ride along to the allowed host's server."""
    seen: list[bytes] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    dialled: list[tuple[str, int]] = []
    async with _server(upstream) as port_up, _proxy(["a.allowed"], _routing(port_up, dialled)) as port:
        await _ask(port, b"GET http://a.allowed:8080/x HTTP/1.1\r\nHost: blocked.example\r\nhost: other.example\r\n\r\n")
    lines = seen[0].lower().split(b"\r\n")
    hosts = [line for line in lines if line.startswith(b"host:")]
    assert hosts == [b"host: a.allowed:8080"], seen[0]


# -- 449: the performer's port, relayed in from outside its network ---------------------------


@pytest.mark.asyncio
async def test_a_forward_relays_every_byte_to_its_one_fixed_target_both_ways() -> None:
    """The daemon reaches a performer on an internal network through the proxy's published port."""
    dialled: list[tuple[str, int]] = []
    async with _server(_echo) as echo:
        proxy = EgressProxy(parse_allowlist("api.github.com"), connect=_routing(echo, dialled))
        async with _server(proxy.forward("performer", 8088)) as port:
            # Even a proxy request goes to the fixed target as plain bytes: the forward never
            # opens a connection anywhere else, so it is no way out.
            answer = await _ask(port, b"CONNECT blocked.example:443 HTTP/1.1\r\n\r\n")
    assert answer == b"CONNECT blocked.example:443 HTTP/1.1\r\n\r\n"
    assert dialled == [("performer", 8088)]


@pytest.mark.asyncio
async def test_a_forward_whose_target_is_not_up_yet_closes_the_client_connection() -> None:
    """While the performer starts, the daemon's readiness poll sees a closed connection and retries."""

    async def down(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise ConnectionRefusedError(host, port)

    proxy = EgressProxy(parse_allowlist("api.github.com"), connect=down)
    async with _server(proxy.forward("performer", 8088)) as port:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await asyncio.wait_for(reader.read(65536), 5) == b""
        writer.close()


# -- 449: the proxy follows its performer ------------------------------------------------------


def _resolver(answers: list[bool]) -> Callable[[str], Awaitable[bool]]:
    """Answers in turn, then the last one for ever."""
    last = answers[-1]

    async def resolve(name: str) -> bool:
        assert name == "performer"
        return answers.pop(0) if answers else last

    return resolve


@pytest.mark.asyncio
async def test_follow_returns_once_the_name_it_saw_misses_three_lookups_in_a_row() -> None:
    """A miss or two (dockerd busy) is not the performer gone; three in a row are."""
    answers = [False, True, False, False, True, False, False, False, True]
    asked: list[bool] = []

    async def resolve(name: str) -> bool:
        asked.append(answers[len(asked)])
        return asked[-1]

    await asyncio.wait_for(egress_proxy.follow("performer", resolve=resolve, interval=0.01, grace=60), 3)
    assert len(asked) == 8  # it ended on the third miss in a row, not on an earlier pair


@pytest.mark.asyncio
async def test_follow_keeps_going_while_the_name_resolves() -> None:
    task = asyncio.create_task(egress_proxy.follow("performer", resolve=_resolver([True]), interval=0.01, grace=0.05))
    await asyncio.sleep(0.3)
    assert not task.done()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_follow_returns_when_the_name_never_resolves_within_the_grace() -> None:
    """A performer that never started (its start failed after the proxy) leaves no proxy behind."""
    task = asyncio.create_task(egress_proxy.follow("performer", resolve=_resolver([False]), interval=0.01, grace=0.1))
    await asyncio.wait_for(task, 3)


@pytest.mark.asyncio
async def test_serve_forwards_and_ends_once_the_followed_performer_is_gone() -> None:
    port, forward_port = _free_port(), _free_port()
    gone = asyncio.Event()

    async def resolve(name: str) -> bool:
        assert name == "performer"
        return not gone.is_set()

    async with _server(_echo) as echo:
        task = asyncio.create_task(egress_proxy.serve(
            parse_allowlist("api.github.com"), "127.0.0.1", port,
            link=egress_proxy.PerformerLink(
                forwards=[(forward_port, "127.0.0.1", echo)], follow="performer", resolve=resolve, interval=0.02,
            ),
        ))
        for _ in range(100):
            try:
                answer = await _ask(forward_port, b"status?")
                break
            except OSError:
                await asyncio.sleep(0.02)
        else:
            pytest.fail("the forward never listened")
        assert answer == b"status?"
        assert (await _ask(port, b"CONNECT blocked.example:443 HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 403")
        await asyncio.sleep(0.1)
        assert not task.done()
        gone.set()
        await asyncio.wait_for(task, 3)


def test_the_command_line_reads_forwards_and_refuses_malformed_ones() -> None:
    assert egress_proxy.parse_forward("8088=performer:8088") == (8088, "performer", 8088)
    for bad in ("8088", "8088=performer", "x=performer:8088", "8088=:8088", "8088=performer:0", "70000=p:1"):
        with pytest.raises(ValueError, match="forward"):
            egress_proxy.parse_forward(bad)
    with pytest.raises(SystemExit):
        egress_proxy.main(["--allow", "api.github.com", "--forward", "8088"])
