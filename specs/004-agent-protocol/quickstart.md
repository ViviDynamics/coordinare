# Quickstart: Agent Communication Protocol

**Feature**: 004-agent-protocol
**Branch**: `004-agent-protocol`

This guide covers configuring the transport layer, running the mock agent locally, and implementing an agent that satisfies the protocol contract.

---

## Configuration

Three config keys control agent communication:

**`config.yaml`:**
```yaml
# Transport selection: subprocess | ssh | kubernetes
# ssh and kubernetes raise a startup error (not yet implemented)
agent_transport: subprocess

# Path to the agent executable (subprocess transport only)
# May be a Python script, compiled binary, or shell wrapper
agent_executable: /path/to/your/agent.py

# Per-message timeout in seconds (non-health sends). Default: 30
transport_timeout_seconds: 30
```

**Environment variable overrides:**
```bash
export COORDINARE_AGENT_TRANSPORT=subprocess
export COORDINARE_AGENT_EXECUTABLE=/path/to/agent.py
export COORDINARE_TRANSPORT_TIMEOUT_SECONDS=30
```

---

## Running with the Mock Agent

The mock agent is a test fixture that simulates an agent implementing the protocol contract. No AI model or network required.

### Start the coordinare with the mock agent

```bash
# In config.yaml:
#   agent_transport: subprocess
#   agent_executable: tests/fixtures/mock_agent.py

MOCK_AGENT_SCENARIO=happy_path uv run coordinare --max-cycles 5
```

### Available scenarios

| Scenario | Response sequence |
|---|---|
| `happy_path` | dispatch→accepted, status→working, status→pr_opened |
| `blocked` | dispatch→accepted, status→working, status→blocked |
| `error` | dispatch→error |
| `busy` | dispatch→busy |
| `session_expired` | dispatch→accepted, relay_feedback→session_expired |

### Run integration tests with the mock agent

```bash
cd src && pytest tests/integration/test_agent_protocol_flow.py -v
```

---

## Unimplemented Transport Error

Selecting an unimplemented transport produces a startup error before any polling begins:

```bash
# config.yaml: agent_transport: kubernetes
uv run coordinare
# Output:
# ERROR  coordinare  event="transport_not_implemented"
#        transport="kubernetes"
#        message="Kubernetes transport is defined but not yet implemented. Use subprocess transport."
# exit code: 1
```

---

## Implementing an Agent (External Contract)

An agent that satisfies the protocol contract must:

1. **Read** a JSON object from `stdin` matching `protocol-message.schema.json`
2. **Write** a JSON object to `stdout` matching `protocol-response.schema.json`
3. **Exit 0** on success (even if the protocol status is `error` or `blocked`)
4. **Exit non-zero** only on transport-level failures (process couldn't start, etc.)
5. **Write diagnostics to `stderr`** (captured separately, never parsed as a response)

### Minimal Python agent skeleton

```python
#!/usr/bin/env python3
"""Minimal agent skeleton implementing the coordinare protocol."""
import json
import sys
import uuid

def handle(message: dict) -> dict:
    action = message.get("action")
    session_id = message.get("session_id", "")

    if action == "dispatch":
        # Start working on the card
        return {
            "status": "accepted",
            "session_id": str(uuid.uuid4()),
        }
    elif action == "status":
        # Return current work status
        return {
            "status": "working",
            "session_id": session_id,
            "progress": "Writing unit tests...",
        }
    elif action == "relay_feedback":
        # Acknowledge and resume
        return {"status": "acknowledged", "session_id": session_id}
    elif action == "health":
        return {"status": "accepted", "session_id": ""}
    else:
        return {"status": "error", "reason": f"Unknown action: {action}"}

if __name__ == "__main__":
    try:
        message = json.loads(sys.stdin.read())
        response = handle(message)
        sys.stdout.write(json.dumps(response))
        sys.stdout.flush()
    except Exception as exc:
        print(f"Fatal agent error: {exc}", file=sys.stderr)
        sys.exit(1)
```

---

## Protocol Contract Reference

The machine-readable JSON Schema files are the authoritative contract:

- **`specs/004-agent-protocol/contracts/protocol-message.schema.json`** — messages sent by the coordinare
- **`specs/004-agent-protocol/contracts/protocol-response.schema.json`** — responses required from agents

These files are versioned in the repository and serve as the specification document for any agent implementation.

---

## Observing Transport Behaviour

Every `send()` call is logged at DEBUG level:

```
DEBUG coordinare.transport  event="transport_send"  action="dispatch"  executable="/path/agent.py"
DEBUG coordinare.transport  event="transport_response"  status="accepted"  session_id="sess_abc123"  duration_ms=142
```

Transport errors:

```
WARN  coordinare.transport  event="transport_error"  action="status"  error="subprocess exited with code 1"
WARN  coordinare.transport  event="transport_timeout"  action="status"  timeout_seconds=30
```

---

## Running Tests

```bash
# All protocol-related tests
cd src && pytest tests/unit/transport/ tests/unit/protocol/ tests/unit/services/test_agent_service.py tests/contract/test_agent_protocol.py tests/integration/test_agent_protocol_flow.py -v

# Integration only (with mock agent, no containers)
cd src && pytest tests/integration/test_agent_protocol_flow.py -v -m integration

# Validate protocol contract schemas
cd src && pytest tests/contract/test_agent_protocol.py -v -m contract
```
