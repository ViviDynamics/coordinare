"""447: the egress proxy of a sandboxed QAre run with an egress allowlist.

A QAre provider with ``sandbox: sysbox`` runs its job on a Docker network created with
``--internal``, which has no route out. This proxy runs in a separate container, on that
network and on the default bridge, and is the job's only way out: everything the job
sends, and everything the containers its own Docker starts send (they are NATed through
the job's one interface), reaches the outside only through here. Code in the sandbox can
change neither the network nor this process.

It relays ``CONNECT host:port`` and plain ``http://`` requests to hosts named in the
allowlist (exact names, any port) and answers everything else with 403 without opening a
connection. Names are resolved here, when a connection is made, so hosts whose addresses
rotate keep working. A plain request gets ``Connection: close`` both ways, so a client
cannot send a second request, for another host, down a connection already relayed to the
first. With ``--idle-exit`` it exits once nothing has connected for that long: the job's
entry point connects every half minute while the job runs, so the proxy (a ``--rm``
container) goes away soon after its job does. It needs nothing beyond the standard
library and structlog, so it runs from the performer image with
``python -m performer.egress_proxy --allow <hosts>``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger(__name__)

#: The port the proxy listens on in its container.
DEFAULT_PORT = 3128
_HEAD_LIMIT = 64 * 1024
_HEAD_TIMEOUT_SECONDS = 30.0
_CONNECT_TIMEOUT_SECONDS = 30.0
#: How long one direction of a relay may stay open after the other has ended.
_HALF_CLOSE_GRACE_SECONDS = 60.0
_CHUNK = 64 * 1024
_MAX_PORT = 65535
_REQUEST_LINE_PARTS = 3  # method, target, version
_HOST = re.compile(r"^(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)*[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_IPV6 = re.compile(r"^[0-9a-f:.]+$")
#: Request headers not passed upstream on a plain request: the proxy's own, and the
#: connection's, which the proxy sets to ``close``.
_DROPPED_HEADERS = (b"proxy-authorization", b"proxy-connection", b"connection", b"keep-alive")

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


def _normal(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.rstrip(".")


def parse_allowlist(text: str) -> frozenset[str]:
    """The allowed hosts from a comma-separated list, as ``PERFORMER_EGRESS_ALLOWLIST`` has them."""
    return frozenset(h for h in (_normal(part) for part in text.split(",")) if h)


def allowed(host: str, allow: frozenset[str]) -> bool:
    """Whether ``host`` is exactly one of the allowed hosts (no suffix matching)."""
    name = _normal(host)
    return bool(name) and name in allow


def _valid_host(host: str) -> bool:
    name = _normal(host)
    return bool(_HOST.match(name) or (":" in name and _IPV6.match(name)))


async def _open(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(asyncio.open_connection(host, port), _CONNECT_TIMEOUT_SECONDS)


class _RefusedError(Exception):
    """A request refused, with the answer to give."""

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class EgressProxy:
    """One allowlist; ``handle`` serves one client connection."""

    def __init__(
        self, allow: frozenset[str], *, connect: Connector = _open, head_timeout: float = _HEAD_TIMEOUT_SECONDS,
        half_close_grace: float = _HALF_CLOSE_GRACE_SECONDS,
    ) -> None:
        self.allow = allow
        self._connect = connect
        self._head_timeout = head_timeout
        self._grace = half_close_grace
        #: Open client connections, and an event set whenever one begins or ends (``--idle-exit``).
        self.active = 0
        self.activity = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.active += 1
        self.activity.set()
        try:
            await self._serve(reader, writer)
        except _RefusedError as refused:
            await _answer(writer, refused.code, refused.reason)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            self.active -= 1
            self.activity.set()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._head_timeout)
        lines = head[:-4].split(b"\r\n")
        method, target, version = _request_line(lines[0])
        if method.upper() == "CONNECT":
            upstream = await self._open_allowed(*_split_authority(target))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await self._relay(reader, writer, *upstream, plain=False)
            return
        host, port, path = _http_target(target)
        upstream = await self._open_allowed(host, port)
        kept = [line for line in lines[1:] if line.split(b":", 1)[0].strip().lower() not in _DROPPED_HEADERS]
        request = [f"{method} {path} {version}".encode("latin-1"), *kept, b"Connection: close"]
        upstream[1].write(b"\r\n".join(request) + b"\r\n\r\n")
        await upstream[1].drain()
        await self._relay(reader, writer, *upstream, plain=True)

    async def _open_allowed(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A connection to ``host:port``, which must be allowed. Nothing is dialled otherwise."""
        if not (_valid_host(host) and allowed(host, self.allow)):
            logger.warning("egress_proxy.refused", host=host, port=port)
            raise _RefusedError(403, f"{host} is not in the egress allowlist")
        try:
            upstream = await self._connect(_normal(host), port)
        except (OSError, TimeoutError) as exc:
            logger.warning("egress_proxy.unreachable", host=host, port=port, error=str(exc))
            raise _RefusedError(502, f"could not reach {host}:{port}") from exc
        logger.info("egress_proxy.allowed", host=host, port=port)
        return upstream

    async def _relay(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter, *, plain: bool,
    ) -> None:
        """Pipe both ways. A plain request ends with its response; a tunnel, once both sides end
        (one side may stay open for ``half_close_grace`` after the other has ended)."""
        out = asyncio.ensure_future(_pipe(client_reader, upstream_writer))
        back = asyncio.ensure_future(_pipe(upstream_reader, client_writer))
        try:
            done, pending = await asyncio.wait({out, back}, return_when=asyncio.FIRST_COMPLETED)
            if pending and not (plain and back in done):
                await asyncio.wait(pending, timeout=self._grace)
        finally:
            for task in (out, back):
                task.cancel()
            await asyncio.gather(out, back, return_exceptions=True)
            upstream_writer.close()
            with contextlib.suppress(Exception):
                await upstream_writer.wait_closed()


def _request_line(line: bytes) -> tuple[str, str, str]:
    parts = line.decode("latin-1").split(" ")
    if len(parts) != _REQUEST_LINE_PARTS or not parts[2].startswith("HTTP/"):
        raise _RefusedError(400, "not an HTTP request")
    return parts[0], parts[1], parts[2]


def _split_authority(target: str) -> tuple[str, int]:
    host, sep, port_text = target.rpartition(":")
    port = int(port_text) if sep and port_text.isascii() and port_text.isdigit() else 0
    if not host or not 0 < port <= _MAX_PORT or not _valid_host(host):
        raise _RefusedError(400, "CONNECT needs host:port")
    return host, port


def _http_target(target: str) -> tuple[str, int, str]:
    """Host, port and origin-form path of an absolute ``http://`` request target."""
    url = urlsplit(target)
    if url.scheme.lower() != "http" or not url.hostname:
        raise _RefusedError(400, "only CONNECT and absolute http:// requests are proxied")
    try:
        port = url.port or 80
    except ValueError as exc:
        raise _RefusedError(400, "bad port") from exc
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    return url.hostname, port, path


async def _answer(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
    status = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}[code]
    body = f"{reason}\n".encode()
    writer.write(
        f"HTTP/1.1 {code} {status}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n".encode() + body,
    )
    with contextlib.suppress(OSError):
        await writer.drain()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_CHUNK):
            writer.write(chunk)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


async def _idle(proxy: EgressProxy, idle_exit: float) -> None:
    """Return once no connection has begun or ended for ``idle_exit`` seconds and none is open."""
    while True:
        proxy.activity.clear()
        try:
            await asyncio.wait_for(proxy.activity.wait(), idle_exit)
        except TimeoutError:
            if not proxy.active:
                return


async def serve(
    allow: frozenset[str], host: str | None = None, port: int = DEFAULT_PORT, *, idle_exit: float = 0.0,
) -> None:
    """Serve until cancelled, or until idle for ``idle_exit`` seconds when that is set.

    ``host`` None listens on every interface of the proxy's container: its address on the
    job's network is not known when it starts.
    """
    proxy = EgressProxy(allow)
    server = await asyncio.start_server(proxy.handle, host, port, limit=_HEAD_LIMIT + 1)
    bound = server.sockets[0].getsockname()[1]
    logger.info("egress_proxy.listening", host=host, port=bound, allowed=sorted(allow), idle_exit=idle_exit)
    async with server:
        if idle_exit > 0:
            await _idle(proxy, idle_exit)
            logger.info("egress_proxy.idle_exit", idle_seconds=idle_exit)
        else:
            await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m performer.egress_proxy", description=__doc__)
    parser.add_argument("--allow", required=True, help="comma-separated hosts, exact names")
    parser.add_argument("--host", default=None, help="the address to listen on (default: every interface)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle-exit", type=float, default=0.0, help="exit after this many seconds with no connection")
    args = parser.parse_args(argv)
    allow = parse_allowlist(args.allow)
    if not allow:
        parser.error("--allow names no host")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(allow, args.host, args.port, idle_exit=args.idle_exit))


if __name__ == "__main__":
    main()
