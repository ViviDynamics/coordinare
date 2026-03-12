#!/usr/bin/env python3
"""Mock opencode HTTP server for integration tests.

Starts a minimal HTTP server that mimics ``opencode serve``:
- GET /global/health → {"healthy": true, "version": "mock"}
- POST /session      → {"id": "mock-session-1"}
- POST /session/<id>/prompt_async → 204
- GET /event         → SSE stream emitting session.idle after a brief delay
- GET /session/<id>  → {"id": ..., "time": {"idle": 1}}

Usage: python mock_opencode_serve.py <port>

The server exits after the SSE /event client disconnects (or after 5s).
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

SESSION_ID = "mock-session-1"
_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19999


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
    except asyncio.TimeoutError:
        writer.close()
        return

    request = data.decode(errors="replace")
    first_line = request.split("\r\n")[0] if request else ""
    method, path = (first_line.split(" ")[:2] + ["", ""])[:2]

    path_no_qs = path.split("?")[0]

    if method == "GET" and path_no_qs == "/global/health":
        body = json.dumps({"healthy": True, "version": "mock"})
        _respond(writer, 200, "application/json", body)

    elif method == "POST" and path_no_qs == "/session":
        body = json.dumps({"id": SESSION_ID})
        _respond(writer, 200, "application/json", body)

    elif method == "POST" and "/prompt_async" in path_no_qs:
        _respond(writer, 204, "application/json", "")

    elif method == "GET" and path_no_qs == f"/session/{SESSION_ID}":
        body = json.dumps({"id": SESSION_ID, "time": {"idle": int(time.time())}})
        _respond(writer, 200, "application/json", body)

    elif method == "GET" and path_no_qs == "/event":
        # SSE: emit session.idle after short delay
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.1)
        event_data = json.dumps({"type": "session.idle", "properties": {}})
        sse_line = f"data: {event_data}\n\n".encode()
        writer.write(sse_line)
        await writer.drain()
        await asyncio.sleep(0.1)
    else:
        _respond(writer, 404, "application/json", json.dumps({"error": "not found"}))

    try:
        await writer.drain()
    except Exception:
        pass
    writer.close()


def _respond(writer: asyncio.StreamWriter, status: int, content_type: str, body: str) -> None:
    body_bytes = body.encode()
    response = (
        f"HTTP/1.1 {status} OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        "\r\n"
    ).encode() + body_bytes
    writer.write(response)


async def main() -> None:
    server = await asyncio.start_server(handle, "127.0.0.1", _PORT)
    async with server:
        await asyncio.wait_for(server.serve_forever(), timeout=10.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (asyncio.TimeoutError, KeyboardInterrupt):
        pass
