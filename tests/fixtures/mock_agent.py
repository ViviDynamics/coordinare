#!/usr/bin/env python3
"""Mock agent executable for integration testing.

Reads a ProtocolMessage JSON from stdin, selects a scenario from
MOCK_AGENT_SCENARIO env var, tracks call count via a state file in
MOCK_AGENT_STATE_DIR, and writes a ProtocolResponse JSON to stdout.

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


def main() -> None:
    scenario = os.environ.get("MOCK_AGENT_SCENARIO", "happy_path")
    raw_input = sys.stdin.read()

    try:
        validated = ProtocolMessage.model_validate_json(raw_input)
        message = validated.model_dump()
    except Exception:
        json.dump({"status": "error", "reason": "Invalid ProtocolMessage input"}, sys.stdout)
        return

    session_id = message.get("session_id", "")
    action = message.get("action", "")

    # Health always responds the same regardless of scenario
    if action == "health":
        json.dump({"status": "accepted", "session_id": ""}, sys.stdout)
        return

    state_dir = _get_state_dir(scenario)
    call_count = _read_call_count(state_dir)

    scenarios = {
        "happy_path": lambda: _happy_path(call_count, session_id),
        "blocked": lambda: _blocked(session_id),
        "error": lambda: _error(session_id),
        "busy": lambda: _busy(session_id),
        "session_expired": lambda: _session_expired(session_id),
        "acknowledged": lambda: _acknowledged(session_id),
        "unknown": lambda: _unknown(session_id),
    }

    handler = scenarios.get(scenario, lambda: _error(session_id))
    response = handler()

    _write_call_count(state_dir, call_count + 1)
    json.dump(response, sys.stdout)


if __name__ == "__main__":
    main()
