#!/usr/bin/env python3
"""Mock agent executable for integration testing.

Reads ProtocolMessage JSON lines from stdin one at a time, selects a scenario
from MOCK_AGENT_SCENARIO env var, tracks call count via a state file in
MOCK_AGENT_STATE_DIR, and writes a ProtocolResponse JSON line to stdout.

This uses a persistent line-by-line loop to match SubprocessTransport._exchange(),
which writes one line and reads one line without closing stdin.

Always exits 0 — transport error scenarios are tested via monkeypatching.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from coordinare.protocol import ProtocolMessage


def _get_state_dir(scenario: str) -> Path:
    base = os.environ.get("MOCK_AGENT_STATE_DIR", f"/tmp/mock_agent_{scenario}")
    return Path(base)


def _read_call_count(state_dir: Path) -> int:
    count_file = state_dir / "call_count"
    if count_file.exists():
        return int(count_file.read_text().strip())
    return 0


def _write_call_count(state_dir: Path, count: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "call_count").write_text(str(count))


def _happy_path(call_count: int, session_id: str) -> dict:
    if call_count == 0:
        return {
            "status": "accepted",
            "session_id": session_id or "mock-session-1",
        }
    if call_count == 1:
        return {
            "status": "working",
            "session_id": session_id or "mock-session-1",
            "progress": "Building and testing",
        }
    return {
        "status": "pr_opened",
        "session_id": session_id or "mock-session-1",
        "pr_url": "https://github.com/org/repo/pull/42",
        "pr_node_id": "PR_kwDOTest42",
    }


def _blocked(session_id: str) -> dict:
    return {
        "status": "blocked",
        "session_id": session_id or "mock-session-1",
        "questions": ["What API key should I use?", "Which environment?"],
        "reason": "Need human input",
    }


def _error(session_id: str) -> dict:
    return {
        "status": "error",
        "session_id": session_id or "mock-session-1",
        "reason": "Unrecoverable failure in mock agent",
    }


def _busy(session_id: str) -> dict:
    return {
        "status": "busy",
        "session_id": session_id or "mock-session-1",
        "reason": "Agent is at capacity",
    }


def _session_expired(session_id: str) -> dict:
    return {
        "status": "session_expired",
        "session_id": session_id or "mock-session-1",
        "reason": "Session context has been lost",
    }


def _acknowledged(session_id: str) -> dict:
    return {
        "status": "acknowledged",
        "session_id": session_id or "mock-session-1",
    }


def _unknown(session_id: str) -> dict:
    return {
        "status": "unknown",
        "session_id": session_id or "mock-session-1",
    }


def _write_response(response: dict) -> None:
    json.dump(response, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    scenario = os.environ.get("MOCK_AGENT_SCENARIO", "happy_path")

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        try:
            validated = ProtocolMessage.model_validate_json(raw_line)
            message = validated.model_dump()
        except Exception:
            _write_response({"status": "error", "reason": "Invalid ProtocolMessage input"})
            continue

        session_id = message.get("session_id", "")
        action = message.get("action", "")

        # Health always responds the same regardless of scenario
        if action == "health":
            _write_response({"status": "accepted", "session_id": ""})
            continue

        state_dir = _get_state_dir(scenario)
        call_count = _read_call_count(state_dir)

        scenarios = {
            "happy_path": lambda cc=call_count, sid=session_id: _happy_path(cc, sid),
            "blocked": lambda sid=session_id: _blocked(sid),
            "error": lambda sid=session_id: _error(sid),
            "busy": lambda sid=session_id: _busy(sid),
            "session_expired": lambda sid=session_id: _session_expired(sid),
            "acknowledged": lambda sid=session_id: _acknowledged(sid),
            "unknown": lambda sid=session_id: _unknown(sid),
        }

        handler = scenarios.get(scenario, lambda sid=session_id: _error(sid))
        response = handler()

        _write_call_count(state_dir, call_count + 1)
        _write_response(response)


if __name__ == "__main__":
    main()
