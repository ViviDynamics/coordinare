#!/usr/bin/env python3
"""Wire-capture isolation proxy (spec 122 US1 debugging).

A tiny host-run forwarding proxy that sits between a backend CLI (running in a
performer container) and the real LiteLLM gateway. For every request it records
the EXACT wire `model`, path, and whether an Authorization header was present;
for every response it records the upstream status and the RAW response bytes
(capped) so malformed-JSON gateway bugs are visible. Then it relays the request
to LiteLLM verbatim and streams the response back, so the CLI run proceeds.

Point a backend at it by setting its provider base URL to
``http://host.docker.internal:<port>/v1`` (containers reach the host there on
Docker Desktop) and OPENAI_API_KEY / the provider key to the LiteLLM master key.

Usage:
  LITELLM_MASTER_KEY=... scripts/wire_capture.py [--port 8099] [--out FILE]
                                                 [--upstream https://litellm.vividynamics.com]
Captures are appended as JSON lines to --out (default tmp/wire_capture.jsonl)
and echoed to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
ARGS = None
_CLIENT: httpx.Client | None = None


def _log(rec: dict) -> None:
    line = json.dumps(rec, default=str)
    print(line, flush=True)
    try:
        with open(ARGS.out, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence default logging
        pass

    def _relay(self, method: str) -> None:
        body = b""
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            body = self.rfile.read(n)
        # --- capture the request wire model ---
        model = None
        try:
            model = json.loads(body).get("model")
        except Exception:
            pass
        has_auth = bool((self.headers.get("Authorization") or "").strip())
        upstream_url = ARGS.upstream.rstrip("/") + self.path
        # forward auth: reuse the client's, else inject the master key
        fwd_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "connection", "accept-encoding")
        }
        # inject the master key when the client sent none, OR (--force-auth) when
        # it sent a wrong/unexpanded key — isolates MODEL compatibility from the
        # backend's auth wiring (which is tested separately).
        bad_auth = has_auth and (
            "${" in (self.headers.get("Authorization") or "")
            or "no-key" in (self.headers.get("Authorization") or "").lower()
        )
        if ARGS.master and (not has_auth or (ARGS.force_auth and bad_auth)):
            fwd_headers["Authorization"] = f"Bearer {ARGS.master}"
        _auth = (self.headers.get("Authorization") or "")
        req_rec = {
            "dir": "request", "ts": time.time(), "method": method, "path": self.path,
            "wire_model": model, "auth_present": has_auth,
            "auth_prefix": _auth[:14], "header_names": sorted(self.headers.keys()),
            "req_body_head": body[:500].decode("utf-8", "backslashreplace"),
        }
        _log(req_rec)
        # --- forward + capture raw response ---
        try:
            r = _CLIENT.request(method, upstream_url, content=body, headers=fwd_headers,
                                timeout=ARGS.timeout)
        except Exception as exc:
            _log({"dir": "error", "ts": time.time(), "path": self.path,
                  "error": f"{type(exc).__name__}: {exc}"})
            self.send_response(502)
            self.end_headers()
            self.wfile.write(b'{"error":"wire_capture upstream failure"}')
            return
        raw = r.content
        # is the body valid JSON? (the glm invalid-JSON bug surfaces here)
        json_ok, json_err = True, None
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                json.loads(raw)
            except Exception as exc:
                json_ok, json_err = False, f"{type(exc).__name__}: {exc}"
        _log({
            "dir": "response", "ts": time.time(), "path": self.path,
            "status": r.status_code, "content_type": ctype,
            "wire_model": model, "json_valid": json_ok, "json_error": json_err,
            "body_len": len(raw),
            "body_head": raw[:600].decode("utf-8", "backslashreplace"),
        })
        # relay verbatim
        self.send_response(r.status_code)
        for k, v in r.headers.items():
            if k.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        self._safe_relay("POST")

    def do_GET(self):
        # model-list / health probes
        self._safe_relay("GET")

    def _safe_relay(self, method: str) -> None:
        import traceback
        try:
            self._relay(method)
        except Exception:
            _log({"dir": "handler_error", "ts": time.time(), "path": self.path,
                  "trace": traceback.format_exc()})
            try:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                pass


def main() -> int:
    global ARGS, _CLIENT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--out", default=str(REPO_ROOT / "tmp" / "wire_capture.jsonl"))
    ap.add_argument("--upstream", default="https://litellm.vividynamics.com")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--force-auth", action="store_true",
                    help="override a wrong/unexpanded client key with the master key")
    ARGS = ap.parse_args()
    ARGS.master = (os.environ.get("LITELLM_MASTER_KEY") or "").strip()
    Path(ARGS.out).parent.mkdir(parents=True, exist_ok=True)
    Path(ARGS.out).write_text("")  # truncate per run
    _CLIENT = httpx.Client(follow_redirects=True)
    srv = ThreadingHTTPServer(("0.0.0.0", ARGS.port), Handler)
    print(f"wire_capture listening on :{ARGS.port} -> {ARGS.upstream} (out={ARGS.out})",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
