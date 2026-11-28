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
rotate keep working. Standard library only, so it runs from the performer image with
``python -m performer.egress_proxy --allow <hosts>``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import sys
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

#: The port the proxy listens on in its container.
DEFAULT_PORT = 3128
_HEAD_LIMIT = 64 * 1024
_HEAD_TIMEOUT_SECONDS = 30.0
_CONNECT_TIMEOUT_SECONDS = 30.0
_CHUNK = 64 * 1024
_MAX_PORT = 65535
_REQUEST_LINE_PARTS = 3  # method, target, version
_HOST = re.compile(r"^(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)*[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?$")
_IPV6 = re.compile(r"^[0-9a-f:.]+$")
_DROPPED_HEADERS = (b"proxy-authorization", b"proxy-connection")

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


def _log(message: str) -> None:
    sys.stderr.write(f"egress-proxy: {message}\n")
    sys.stderr.flush()


class _Refused(Exception):  # noqa: N818 - a request refused, with the answer to give
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class EgressProxy:
    """One allowlist; ``handle`` serves one client connection."""

    def __init__(
        self, allow: frozenset[str], *, connect: Connector = _open, head_timeout: float = _HEAD_TIMEOUT_SECONDS,
    ) -> None:
        self.allow = allow
        self._connect = connect
        self._head_timeout = head_timeout

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except _Refused as refused:
            await _answer(writer, refused.code, refused.reason)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._head_timeout)
        lines = head[:-4].split(b"\r\n")
        method, target, version = _request_line(lines[0])
        if method.upper() == "CONNECT":
            upstream = await self._open_allowed(*_split_authority(target))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
        else:
            host, port, path = _http_target(target)
            upstream = await self._open_allowed(host, port)
            kept = [line for line in lines[1:] if line.split(b":", 1)[0].strip().lower() not in _DROPPED_HEADERS]
            upstream[1].write(b"\r\n".join([f"{method} {path} {version}".encode("latin-1"), *kept]) + b"\r\n\r\n")
            await upstream[1].drain()
        await _relay(reader, writer, *upstream)

    async def _open_allowed(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """A connection to ``host:port``, which must be allowed. Nothing is dialled otherwise."""
        if not (_valid_host(host) and allowed(host, self.allow)):
            _log(f"refused {host!r}: not in the egress allowlist")
            raise _Refused(403, f"{host} is not in the egress allowlist")
        try:
            upstream = await self._connect(_normal(host), port)
        except (OSError, TimeoutError) as exc:
            _log(f"could not reach {host}:{port}: {exc}")
            raise _Refused(502, f"could not reach {host}:{port}") from exc
        _log(f"allowed {host}:{port}")
        return upstream


def _request_line(line: bytes) -> tuple[str, str, str]:
    parts = line.decode("latin-1").split(" ")
    if len(parts) != _REQUEST_LINE_PARTS or not parts[2].startswith("HTTP/"):
        raise _Refused(400, "not an HTTP request")
    return parts[0], parts[1], parts[2]


def _split_authority(target: str) -> tuple[str, int]:
    host, sep, port_text = target.rpartition(":")
    port = int(port_text) if sep and port_text.isdigit() else 0
    if not host or not 0 < port <= _MAX_PORT or not _valid_host(host):
        raise _Refused(400, "CONNECT needs host:port")
    return host, port


def _http_target(target: str) -> tuple[str, int, str]:
    """Host, port and origin-form path of an absolute ``http://`` request target."""
    url = urlsplit(target)
    if url.scheme.lower() != "http" or not url.hostname:
        raise _Refused(400, "only CONNECT and absolute http:// requests are proxied")
    try:
        port = url.port or 80
    except ValueError as exc:
        raise _Refused(400, "bad port") from exc
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


async def _relay(
    client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader, upstream_writer: asyncio.StreamWriter,
) -> None:
    try:
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
    finally:
        upstream_writer.close()
        with contextlib.suppress(Exception):
            await upstream_writer.wait_closed()


async def serve(allow: frozenset[str], host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:  # noqa: S104
    """Serve until cancelled."""
    proxy = EgressProxy(allow)
    server = await asyncio.start_server(proxy.handle, host, port, limit=_HEAD_LIMIT + 1)
    bound = server.sockets[0].getsockname()[1]
    _log(f"listening on {host}:{bound}; allowed: {', '.join(sorted(allow))}")
    async with server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m performer.egress_proxy", description=__doc__)
    parser.add_argument("--allow", required=True, help="comma-separated hosts, exact names")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - its container's networks only
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    allow = parse_allowlist(args.allow)
    if not allow:
        parser.error("--allow names no host")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(allow, args.host, args.port))


if __name__ == "__main__":
    main()
