#!/usr/bin/env python3
"""Mock opencode ACP subprocess for integration tests.

Reads nd-JSON from stdin, sleeps 0.1 s, emits session.idle to stdout, then exits.
Used by tests/integration/test_performance.py in place of the real `opencode acp`.

Usage (invoked by OpenCodeAdapter.start via AGENT_BACKEND env var hook):
    python mock_opencode_acp.py
"""
from __future__ import annotations

import json
import sys
import time

# Consume initial task message from stdin (may or may not arrive)
try:
    line = sys.stdin.readline()
    if line.strip():
        _msg = json.loads(line)
except (json.JSONDecodeError, OSError):
    pass

# Brief delay to simulate work
time.sleep(0.1)

# Signal completion
sys.stdout.write(json.dumps({"type": "session.idle"}) + "\n")
sys.stdout.flush()

sys.exit(0)
