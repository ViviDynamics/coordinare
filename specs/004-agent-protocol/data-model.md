# Data Model: Agent Communication Protocol

**Feature**: 004-agent-protocol
**Branch**: `004-agent-protocol`
**Date**: 2026-02-22

---

## ActionType

```python
ActionType = Literal["dispatch", "status", "relay_feedback", "health"]
```

---

## StatusType

```python
StatusType = Literal[
    "accepted",       # Dispatch accepted; work starting
    "working",        # Agent actively processing
    "pr_opened",      # Agent opened a PR; pr_url and pr_node_id populated
    "blocked",        # Agent needs human input; questions populated
    "error",          # Unrecoverable agent error; reason populated
    "unknown",        # Session ID not recognised
    "busy",           # Agent already working on another task
    "acknowledged",   # relay_feedback received; agent resuming
    "session_expired",# Session context lost; relay_feedback failed
]
```

---

## ProtocolMessage (coordinare → agent)

```python
class ProtocolMessage(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    action: ActionType
    session_id: str = ""  # Empty on initial dispatch; populated for status, relay_feedback, health
    payload: dict[str, Any] = Field(default_factory=dict)
```

### Payload shapes by action

| Action | Required payload keys | Optional payload keys |
|---|---|---|
| `dispatch` | `title`, `description`, `acceptance_criteria`, `board_card_id`, `column` | `assigned_agent` |
| `status` | — (session_id in outer envelope) | — |
| `relay_feedback` | `session_id`, `pr_url`, `reviews` | — |
| `health` | — | — |

### `dispatch` payload example

```json
{
  "title": "Implement user login flow",
  "description": "As a user I want to log in with my email and password...",
  "acceptance_criteria": ["Valid credentials grant access", "Invalid credentials show error"],
  "board_card_id": "PVT_kwDOABCDEF",
  "column": "In Progress"
}
```

### `relay_feedback` payload example

```json
{
  "session_id": "sess_abc123",
  "pr_url": "https://github.com/org/repo/pull/42",
  "reviews": [
    {
      "reviewer": "copilot",
      "body": "Line 42: null check missing",
      "file": "src/auth.py",
      "line": 42
    }
  ]
}
```

---

## ProtocolResponse (agent → coordinare)

```python
class ProtocolResponse(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    status: StatusType
    session_id: str = ""
    reason: str | None = None        # Populated for: error, blocked, session_expired
    questions: list[str] = Field(default_factory=list)  # Non-empty when status=blocked
    pr_url: str | None = None        # Non-empty when status=pr_opened
    pr_node_id: str | None = None    # Non-empty when status=pr_opened
    progress: str | None = None      # Optional when status=working
```

### Validation rules

| Field | Rule | On violation |
|---|---|---|
| `status` | One of StatusType literal values | Pydantic ValidationError → TransientError |
| `pr_url` | Present and non-empty when `status == "pr_opened"` | Soft validation; log warning |
| `pr_node_id` | Present and non-empty when `status == "pr_opened"` | Soft validation; log warning |
| `questions` | Non-empty list when `status == "blocked"` | Soft validation; log warning |
| `reason` | Non-empty when `status in {"error", "blocked", "session_expired"}` | Soft validation; log warning |
| JSON bytes | Valid UTF-8 JSON parseable by Pydantic | ValidationError → TransientError |

---

## AgentTransport (interface)

```python
class AgentTransport(Protocol):
    async def send(self, message: ProtocolMessage) -> ProtocolResponse: ...
```

Transport-level exceptions (distinct from protocol-level status values):

```python
class TransportError(RuntimeError):
    """Process failed to start, non-zero exit, broken pipe, stderr with empty stdout."""

class TransportTimeoutError(TransportError):
    """No response received within transport_timeout_seconds."""
    def __init__(self, timeout: int) -> None: ...
```

---

## SubprocessTransport

```python
class SubprocessTransport:
    def __init__(self, executable: str, timeout_seconds: int = 30) -> None:
        self._executable = executable
        self._timeout_seconds = timeout_seconds

    async def send(self, message: ProtocolMessage) -> ProtocolResponse:
        """
        1. asyncio.create_subprocess_exec(self._executable, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        2. await asyncio.wait_for(proc.communicate(msg_bytes), timeout=timeout)
           - On TimeoutError: proc.kill() → proc.wait() → raise TransportTimeoutError
        3. If proc.returncode != 0: raise TransportError
        4. If stdout empty and stderr non-empty: raise TransportError
        5. ProtocolResponse.model_validate_json(stdout) → return
           - On ValidationError: re-raise as TransportError (caller treats as transient)
        """
```

**Health send**: caller passes `timeout_seconds=10` override; or `SubprocessTransport` has a separate `send_health()` method with hardcoded 10s.

Actually, the simpler design: `AgentService.check_health()` constructs a `SubprocessTransport` with `timeout_seconds=10` specifically for health, or passes a timeout override. Implementation detail — plan should document this.

**Design**: `AgentService` wraps the transport and passes a `timeout` kwarg on health calls. `SubprocessTransport.send()` accepts an optional `timeout_override: int | None = None`.

---

## SshTransport (stub)

```python
class SshTransport:
    def __init__(self) -> None:
        raise NotImplementedError(
            "SSH transport is defined but not yet implemented. "
            "Set agent_transport: subprocess in your configuration."
        )
    # send() never reached — __init__ raises
```

---

## KubernetesTransport (stub)

```python
class KubernetesTransport:
    def __init__(self) -> None:
        raise NotImplementedError(
            "Kubernetes transport is defined but not yet implemented. "
            "Set agent_transport: subprocess in your configuration."
        )
```

---

## AgentService (implements AgentServiceProtocol)

```python
class AgentService:
    def __init__(self, transport: AgentTransport) -> None:
        self._transport = transport

    async def dispatch_card(self, card_context: dict[str, Any]) -> dict[str, Any]:
        msg = ProtocolMessage(action="dispatch", session_id="", payload=card_context)
        response = await self._transport.send(msg)
        return response.model_dump()

    async def check_status(self, session_id: str) -> dict[str, Any]:
        msg = ProtocolMessage(action="status", session_id=session_id, payload={})
        response = await self._transport.send(msg)
        return response.model_dump()

    async def relay_feedback(self, review_payload: dict[str, Any]) -> dict[str, Any]:
        msg = ProtocolMessage(
            action="relay_feedback",
            session_id=review_payload.get("session_id", ""),
            payload=review_payload,
        )
        response = await self._transport.send(msg)
        return response.model_dump()

    async def check_health(self) -> dict[str, Any]:
        msg = ProtocolMessage(action="health", session_id="", payload={})
        response = await self._transport.send(msg, timeout_override=10)
        return response.model_dump()
```

**TransportError handling in AgentService**: Catches `TransportError` and `TransportTimeoutError`, logs the error, and returns `{"status": "unknown"}` for status polls; re-raises for dispatch (so the coordinare knows it failed to send).

---

## Config Extensions

Add to `ProjectConfiguration`:

```python
agent_transport: Literal["subprocess", "ssh", "kubernetes"] = Field(default="subprocess")
agent_executable: str = ""           # Path to agent binary/script (subprocess transport)
transport_timeout_seconds: int = Field(default=30, ge=1, le=300)
```

Make SSH-specific fields optional (retain for backward compat, ignored when transport=subprocess):

```python
agent_host: str = ""
agent_port: int = 22
agent_user: str = ""
agent_key_path: Path = Path("~/.ssh/id_ed25519")
agent_command: str | None = None   # Remove {card_context} validator; deprecated
```

---

## Mock Agent (`tests/fixtures/mock_agent.py`)

A standalone Python script that reads a `ProtocolMessage` JSON from stdin and writes a `ProtocolResponse` JSON to stdout, with response sequencing controlled by the `MOCK_AGENT_SCENARIO` environment variable.

### Scenarios

| Scenario | Response sequence |
|---|---|
| `happy_path` | dispatch→`accepted`, status→`working`, status→`pr_opened` |
| `blocked` | dispatch→`accepted`, status→`working`, status→`blocked` |
| `error` | dispatch→`error` |
| `busy` | dispatch→`busy` |
| `session_expired` | dispatch→`accepted`, relay_feedback→`session_expired` |

### State tracking

The mock agent writes a state file to `MOCK_AGENT_STATE_DIR` (default: `/tmp/mock_agent_state/`) that tracks how many times each action has been called. This enables the sequenced responses (e.g., first `status` → `working`, second `status` → `pr_opened`).

### Invocation

```bash
MOCK_AGENT_SCENARIO=happy_path python tests/fixtures/mock_agent.py
```

(Reads JSON from stdin, writes JSON to stdout, exits 0)

---

## `AgentServiceProtocol` Update

The existing `check_status(card_id: str)` signature is renamed to `check_status(session_id: str)` in `src/coordinare/graph/state.py`. The caller `monitor_agent.py` is updated to pass `state.get("agent_dispatch", {}).get("session_id", "")`.

---

## JSON Schema Contract Files

Generated from Pydantic models via `model_json_schema()`:

- `contracts/protocol-message.schema.json` — ProtocolMessage wire format
- `contracts/protocol-response.schema.json` — ProtocolResponse wire format

Published as static files in `specs/004-agent-protocol/contracts/` and also exported to `src/coordinare/protocol_schemas/` for use by the coordinare's runtime validator (FR-014).
