#!/usr/bin/env python3
"""Minimal Codex app-server WebSocket client example.

Usage:
  python docs/examples/codex_app_server_ws_client.py "Say OK and nothing else."

Prerequisites:
  - `codex` is installed and authenticated (`codex login status`)
  - Python package `websocket-client` is installed
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import time
from typing import Any

import websocket


def _pick_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_listen_line(proc: subprocess.Popen[str], timeout_s: float = 10.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            continue
        line = line.strip()
        if "listening on:" in line:
            return
    raise TimeoutError("timed out waiting for codex app-server to start")


def _send_request(ws: websocket.WebSocket, request_id: str, method: str, params: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    ws.send(json.dumps(payload))


def _recv_until_id(ws: websocket.WebSocket, request_id: str) -> dict[str, Any]:
    while True:
        msg = json.loads(ws.recv())
        if str(msg.get("id")) == request_id:
            return msg


def run(prompt: str) -> int:
    port = _pick_port()
    server = subprocess.Popen(
        ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    ws: websocket.WebSocket | None = None
    try:
        _wait_for_listen_line(server)
        ws = websocket.create_connection(f"ws://127.0.0.1:{port}", timeout=120)

        _send_request(
            ws,
            request_id="1",
            method="initialize",
            params={"clientInfo": {"name": "example-python-client", "version": "0.1.0"}},
        )
        _recv_until_id(ws, "1")

        ws.send(json.dumps({"method": "initialized"}))

        _send_request(ws, request_id="2", method="thread/start", params={"ephemeral": True})
        thread_start = _recv_until_id(ws, "2")
        thread_id = thread_start["result"]["thread"]["id"]

        _send_request(
            ws,
            request_id="3",
            method="turn/start",
            params={
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
        )

        assistant_text: list[str] = []
        while True:
            msg = json.loads(ws.recv())
            method = msg.get("method")

            if method == "item/agentMessage/delta":
                delta = msg.get("params", {}).get("delta", "")
                if delta:
                    assistant_text.append(delta)
                    print(delta, end="", flush=True)
            elif method == "item/commandExecution/outputDelta":
                delta = msg.get("params", {}).get("delta", "")
                if delta:
                    print(f"\n[command] {delta}", file=sys.stderr, end="")
            elif method == "item/fileChange/outputDelta":
                delta = msg.get("params", {}).get("delta", "")
                if delta:
                    print(f"\n[file-change] {delta}", file=sys.stderr, end="")
            elif method == "turn/completed":
                break

        if assistant_text:
            print()
        return 0
    finally:
        if ws is not None:
            with contextlib.suppress(Exception):
                ws.close()
        server.terminate()
        try:
            server.wait(timeout=2)
        except subprocess.TimeoutExpired:
            server.kill()


def main() -> int:
    prompt = " ".join(sys.argv[1:]).strip() or "Say OK and nothing else."
    return run(prompt)


if __name__ == "__main__":
    raise SystemExit(main())
