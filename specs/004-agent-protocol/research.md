# Research: Agent Communication Protocol

**Feature**: 004-agent-protocol
**Branch**: `004-agent-protocol`
**Date**: 2026-02-22

---

## Architecture Decision: Protocol Layering

**Decision**: Three-layer architecture — Graph Nodes → AgentService (AgentServiceProtocol) → AgentTransport → SubprocessTransport

**Rationale**:
- Graph nodes (`dispatch_card.py`, `monitor_agent.py`, `relay_feedback.py`) call `AgentServiceProtocol` methods (`dispatch_card`, `check_status`, `relay_feedback`, `check_health`) — **zero changes** to graph node logic
- A new `AgentService` class implements `AgentServiceProtocol` by translating each method call into a `ProtocolMessage`, sending it via the configured `AgentTransport`, and interpreting the `ProtocolResponse`
- `AgentTransport` is a Protocol interface with a single `send(ProtocolMessage) → ProtocolResponse` operation — transport-agnostic
- `SubprocessTransport` is the only concrete implementation; `SshTransport` and `KubernetesTransport` are stubs that raise `NotImplementedError` at startup

**Impact on existing `AgentSSHService`**:
- `AgentSSHService` is retained as-is during this feature; `SubprocessTransport` + `AgentService` is introduced as an alternative
- The config key `agent_transport: subprocess | ssh | kubernetes` determines which path `__main__.py` takes at startup
- When `agent_transport: subprocess`, the new stack is used; when `ssh`, `AgentSSHService` continues to be used (SSH transport stub raises error only if the new K8s/SSH stubs are selected)

Wait — re-reading the spec: FR-005 says `SshTransport` raises a startup error. FR-006 says `KubernetesTransport` raises a startup error. The spec is about the *new* transport abstraction. `AgentSSHService` is the pre-existing implementation that bypasses the abstraction. For this feature, choosing `agent_transport: ssh` should use the new `SshTransport` stub (raises error), not `AgentSSHService`.

**Revised decision**: `AgentSSHService` is fully superseded. The `agent_transport` config key selects from the new transport hierarchy exclusively. Selecting `ssh` raises the startup error (per spec). The `asyncssh` dependency may be removed if no other code uses it.

**Alternatives considered**:
- Keep `AgentSSHService` as the `ssh` transport implementation: rejected — the spec explicitly says SSH transport is "not implemented" (FR-005); adding it here would expand scope
- Make `AgentTransport` an abstract base class (ABC): rejected — Python `Protocol` is simpler, avoids inheritance, and is consistent with existing `AgentServiceProtocol`

---

## Transport Implementation: SubprocessTransport

**Decision**: Use `asyncio.create_subprocess_exec()` with `asyncio.wait_for()` for timeout enforcement

**Rationale**:
- `asyncio.create_subprocess_exec()` avoids shell injection risk (no shell=True)
- `proc.communicate(input=msg_bytes)` handles stdin write + stdout/stderr read atomically — no deadlock from reading one at a time
- `asyncio.wait_for(..., timeout=seconds)` cancels the coroutine and triggers `proc.kill()` on timeout
- One fresh process per `send()` call — stateless from the transport's perspective; session continuity is the agent's responsibility (reading its own state file)

**Timeout handling**:
- Non-health sends: `transport_timeout_seconds` (configurable, default 30s) from config
- Health sends: 10s fixed, regardless of `transport_timeout_seconds` (FR-013)
- On timeout: `proc.kill()` → `await proc.wait()` → raise `TransportTimeoutError`
- Caller (`AgentService`) catches `TransportTimeoutError` and returns `{"status": "unknown"}`

**Process cleanup**:
- `proc.communicate()` cleanly closes stdin on completion
- If timeout occurs, `proc.kill()` terminates the process and `proc.wait()` reaps it — no zombie processes
- `stderr` is captured via `asyncio.subprocess.PIPE` and logged at DEBUG level unconditionally; non-empty stderr with empty stdout → `TransportError`

**Alternatives considered**:
- `asyncio.create_subprocess_shell()`: rejected — shell injection risk
- Running process in a thread (`asyncio.run_in_executor`): rejected — unnecessarily complex; `asyncio.create_subprocess_exec` is natively async
- Persistent subprocess (keep process alive): rejected — spec explicitly says "fresh process per exchange" (FR-004 interpretation); simpler; agent manages its own session state on disk

---

## Protocol Entities: Pydantic v2 Models

**Decision**: `ProtocolMessage` and `ProtocolResponse` as Pydantic v2 `BaseModel` with `model_json_schema()` generating JSON Schema files

**Rationale**:
- Pydantic v2 already present (`pydantic-settings` depends on it)
- `model_dump_json()` → stdin of subprocess; `model_validate_json()` ← stdout — clean round-trip
- `model_json_schema()` generates JSON Schema draft 2020-12 compatible output, published as static files in `contracts/` (FR-016, SC-005, SC-006)
- Status enum as `Literal[...]` in Pydantic gives exhaustive validation

**Schema generation script**: A small utility in `src/coordinare/protocol.py` module exports `generate_contracts()` that writes the schema files; called during build or manually; output committed to `specs/004-agent-protocol/contracts/`

**New module location**: `src/coordinare/protocol.py` — single file for all protocol models (ProtocolMessage, ProtocolResponse, ActionType, StatusType). Transport classes in `src/coordinare/transport/`.

---

## `check_status` Signature Change

**Decision**: Rename `check_status(self, card_id: str)` to `check_status(self, session_id: str)` in `AgentServiceProtocol` and update `monitor_agent.py`

**Rationale**:
- FR-011: "The `status` action MUST include the session ID from the original dispatch response" — `card_id` (board node ID) is the wrong parameter
- `monitor_agent.py` currently passes `card["id"]` — must be updated to pass `state.get("agent_dispatch", {}).get("session_id", "")`
- `dispatch_card.py` stores the dispatch response in `state["agent_dispatch"]` which after this feature contains `{"session_id": "...", "status": "accepted", ...}`
- This is a breaking change to `AgentServiceProtocol` — any callers must be updated (only `monitor_agent.py` calls `check_status`)

**Alternatives considered**: Keep `card_id` and translate to `session_id` inside `AgentService`: rejected — leaks a mapping concern into the service; makes the protocol intent unclear

---

## Config Changes

**Decision**: Add to `ProjectConfiguration`:

```python
agent_transport: Literal["subprocess", "ssh", "kubernetes"] = Field(default="subprocess")
agent_executable: str  # Path to the agent script/binary (for subprocess transport)
transport_timeout_seconds: int = Field(default=30, ge=1, le=300)
```

Remove or deprecate: `agent_host`, `agent_port`, `agent_user`, `agent_key_path`, `agent_command` (SSH-specific fields, no longer used when transport is `subprocess`)

**Decision**: Keep SSH config fields optional (`agent_host: str = ""` etc.) for backward compatibility; they are ignored when `agent_transport: subprocess`. Removing them would break existing config.yaml files.

Actually: `agent_command` has a validator requiring `{card_context}` placeholder — this will fail if the field exists but is empty. Need to make it optional (`str | None = None`) with validator only when non-None.

---

## Mock Agent Location and Format

**Decision**: `tests/fixtures/mock_agent.py` — a Python script invoked by `SubprocessTransport` as `python tests/fixtures/mock_agent.py`

**Scenario control**: Environment variable `MOCK_AGENT_SCENARIO` selects the response sequence from a built-in dict (no external file needed for simplicity). Scenarios: `happy_path`, `blocked`, `error`, `session_expired`, `busy`.

**Rationale**:
- No external file dependency simplifies CI (FR-018: no network, no containers)
- Python is already the project language; no cross-language toolchain
- `MOCK_AGENT_SCENARIO` env var is set by the integration test fixture via `os.environ` patching

**Alternatives considered**: JSON scenario file: rejected — adds I/O and file-path management to tests; env var is simpler for a test fixture

---

## Source Impact Summary

| File | Change |
|---|---|
| `src/coordinare/protocol.py` | **NEW**: ProtocolMessage, ProtocolResponse, ActionType, StatusType, generate_contracts() |
| `src/coordinare/transport/__init__.py` | **NEW**: transport package |
| `src/coordinare/transport/base.py` | **NEW**: AgentTransport Protocol, TransportError, TransportTimeoutError |
| `src/coordinare/transport/subprocess_transport.py` | **NEW**: SubprocessTransport (fully implemented) |
| `src/coordinare/transport/ssh_transport.py` | **NEW**: SshTransport (stub, raises NotImplementedError at init) |
| `src/coordinare/transport/kubernetes_transport.py` | **NEW**: KubernetesTransport (stub, raises NotImplementedError at init) |
| `src/coordinare/services/agent_service.py` | **NEW**: AgentService (wraps AgentTransport, implements AgentServiceProtocol) |
| `src/coordinare/graph/state.py` | MODIFY: rename `check_status(card_id)` → `check_status(session_id)` in AgentServiceProtocol |
| `src/coordinare/graph/nodes/monitor_agent.py` | MODIFY: pass `state.get("agent_dispatch", {}).get("session_id", "")` to `check_status()` |
| `src/coordinare/config.py` | MODIFY: add `agent_transport`, `agent_executable`, `transport_timeout_seconds`; make SSH-specific fields optional |
| `src/coordinare/__main__.py` | MODIFY: transport factory, startup validation for unimplemented transports, instantiate AgentService |
| `tests/fixtures/mock_agent.py` | **NEW**: Mock agent executable |
| `tests/unit/transport/test_subprocess_transport.py` | **NEW**: SubprocessTransport unit tests |
| `tests/unit/protocol/test_protocol_models.py` | **NEW**: ProtocolMessage/Response unit tests |
| `tests/unit/services/test_agent_service.py` | **NEW**: AgentService unit tests |
| `tests/contract/test_agent_protocol.py` | **NEW**: JSON Schema contract tests |
| `tests/integration/test_agent_protocol_flow.py` | **NEW**: Full dispatch-poll-feedback cycles with mock agent |
