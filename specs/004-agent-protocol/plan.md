# Implementation Plan: Agent Communication Protocol

**Branch**: `004-agent-protocol` | **Date**: 2026-02-22 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/004-agent-protocol/spec.md`

---

## Summary

Replace the hard-coded `AgentSSHService` with a pluggable transport architecture. Introduce `ProtocolMessage` and `ProtocolResponse` as the wire protocol (Pydantic v2 models, JSON Schema published as contracts). Implement `SubprocessTransport` as the first fully working transport; provide `SshTransport` and `KubernetesTransport` as startup-error stubs. Wrap the transport in a new `AgentService` that implements the existing `AgentServiceProtocol` interface — zero changes to graph node logic. Deliver a `MockAgent` executable for integration testing with no containers or network dependencies.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: Pydantic v2 (already present), `asyncio.create_subprocess_exec` (stdlib), `structlog` (already present), `pytest-asyncio` (already present)
**Storage**: N/A — session IDs stored in state (spec 003); this spec owns no persistent storage
**Testing**: pytest, pytest-asyncio; mock agent subprocess as integration test fixture
**Target Platform**: Linux container (same as daemon)
**Project Type**: Single project (`src/` + `tests/`)
**Performance Goals**: SC-001 (60s full cycle), SC-004 (CI suite < 2min), FR-013 (health 10s), default transport timeout 30s
**Constraints**: No containers, network calls, or external credentials required for any test (SC-004, FR-018)
**Scale/Scope**: Single agent per coordinare; single concurrent message per transport call

---

## Constitution Check

### I. Code Quality First ✅
- `SubprocessTransport` has one job: I/O to a subprocess (spawn → write → read → parse)
- `AgentService` has one job: translate `AgentServiceProtocol` calls into `ProtocolMessage`/`ProtocolResponse`
- `ProtocolMessage` and `ProtocolResponse` are pure data models with no I/O logic
- All public interfaces fully type-annotated
- `asyncssh` dependency removed from `pyproject.toml` when `AgentSSHService` is superseded (minimal dependencies principle)

### II. Testing Discipline ✅
- **Unit**: `SubprocessTransport` (mock subprocess via monkeypatch), `AgentService` (mock transport), `ProtocolMessage`/`ProtocolResponse` (Pydantic validation), config transport selection logic
- **Integration**: Full dispatch→poll→pr_opened cycle with mock agent subprocess; all 9 status values exercised (FR-019); error scenarios: malformed response, timeout, non-zero exit, unknown session
- **Contract**: JSON Schema validation of `ProtocolMessage` and `ProtocolResponse` against `contracts/*.schema.json`; assert `generate_contracts()` output matches checked-in files

### III. User Experience Consistency ✅
- Transport startup errors are structured: `event="transport_not_implemented"`, `transport=`, actionable message (not a Python traceback exposed to operators)
- All transport log events follow existing daemon structlog pattern (`event=`, `category=`, key-value pairs)

### IV. Performance by Design ✅
- **Budgets defined**: 30s per-message timeout; 10s health timeout; 60s full cycle (SC-001); CI suite < 2min (SC-004)
- **Measurement**: All `send()` calls log `duration_ms`; integration test asserts full mock-agent cycle < 60s wall time
- **CI regression prevention**: Integration test suite gated to < 2min in CI; enforced by pytest timeout plugin if available

### V. Clarity Before Action ✅
- FR-016 schema format resolved in clarify session 2026-02-22: JSON Schema (draft 2020-12) from Pydantic v2 `model_json_schema()`
- FR-016a transport timeout resolved: `transport_timeout_seconds` configurable (default 30s); health fixed at 10s
- No NEEDS CLARIFICATION markers remain

---

## Project Structure

### Documentation (this feature)

```text
specs/004-agent-protocol/
├── plan.md                                      # This file
├── research.md                                  # Phase 0 decisions
├── data-model.md                                # Entity definitions and interfaces
├── quickstart.md                                # Developer and operator guide
├── contracts/
│   ├── protocol-message.schema.json             # ProtocolMessage JSON Schema (v1)
│   └── protocol-response.schema.json            # ProtocolResponse JSON Schema (v1)
└── tasks.md                                     # Phase 2 output (/speckit.tasks)
```

### Source Code

```text
src/coordinare/
├── protocol.py                   # NEW: ProtocolMessage, ProtocolResponse, generate_contracts()
├── transport/
│   ├── __init__.py               # NEW: package, exports AgentTransport
│   ├── base.py                   # NEW: AgentTransport Protocol, TransportError, TransportTimeoutError
│   ├── subprocess_transport.py   # NEW: SubprocessTransport (fully implemented)
│   ├── ssh_transport.py          # NEW: SshTransport (stub — NotImplementedError at __init__)
│   └── kubernetes_transport.py   # NEW: KubernetesTransport (stub — NotImplementedError at __init__)
├── services/
│   ├── agent_service.py          # NEW: AgentService (implements AgentServiceProtocol via transport)
│   └── agent_ssh.py              # SUPERSEDED: no longer instantiated; kept for git history
├── graph/
│   ├── state.py                  # MODIFY: rename check_status(card_id) → check_status(session_id)
│   └── nodes/
│       └── monitor_agent.py      # MODIFY: pass session_id from agent_dispatch dict
├── config.py                     # MODIFY: add agent_transport, agent_executable, transport_timeout_seconds
└── __main__.py                   # MODIFY: transport factory + startup error for stubs

tests/
├── fixtures/
│   └── mock_agent.py             # NEW: mock agent executable (stdin→stdout JSON)
├── unit/
│   ├── transport/
│   │   ├── __init__.py
│   │   └── test_subprocess_transport.py  # NEW
│   ├── protocol/
│   │   ├── __init__.py
│   │   └── test_protocol_models.py       # NEW
│   └── services/
│       └── test_agent_service.py         # NEW
├── contract/
│   └── test_agent_protocol.py            # NEW
└── integration/
    └── test_agent_protocol_flow.py       # NEW
```

**Structure Decision**: Single project layout. Transport implementations in `src/coordinare/transport/` sub-package — consistent with separation of concerns; keeps infrastructure code out of `services/`.

---

## Implementation Phases

### Phase A: Protocol Models (`src/coordinare/protocol.py`)

Create `src/coordinare/protocol.py` with:

1. `ActionType = Literal["dispatch", "status", "relay_feedback", "health"]`
2. `StatusType = Literal["accepted", "working", "pr_opened", "blocked", "error", "unknown", "busy", "acknowledged", "session_expired"]`
3. `class ProtocolMessage(BaseModel)`:
   - `action: ActionType`
   - `session_id: str = ""`
   - `payload: dict[str, Any] = Field(default_factory=dict)`
4. `class ProtocolResponse(BaseModel)`:
   - `status: StatusType`
   - `session_id: str = ""`
   - `reason: str | None = None`
   - `questions: list[str] = Field(default_factory=list)`
   - `pr_url: str | None = None`
   - `pr_node_id: str | None = None`
   - `progress: str | None = None`
5. `def generate_contracts(output_dir: Path) -> None` — writes JSON Schema from `model_json_schema()`

---

### Phase B: Transport Interface and Stubs (`src/coordinare/transport/`)

**`base.py`**:
- `class TransportError(RuntimeError)` — transport-level failures
- `class TransportTimeoutError(TransportError)` with `timeout: int`
- `class AgentTransport(Protocol)`:
  - `async def send(self, message: ProtocolMessage, *, timeout_override: int | None = None) -> ProtocolResponse`

**`ssh_transport.py`**:
```python
class SshTransport:
    def __init__(self) -> None:
        raise NotImplementedError(
            "SSH transport is defined but not yet implemented. "
            "Set agent_transport: subprocess in your configuration."
        )
```

**`kubernetes_transport.py`**:
```python
class KubernetesTransport:
    def __init__(self) -> None:
        raise NotImplementedError(
            "Kubernetes transport is defined but not yet implemented. "
            "Set agent_transport: subprocess in your configuration."
        )
```

---

### Phase C: SubprocessTransport (`src/coordinare/transport/subprocess_transport.py`)

Key implementation details:
- `asyncio.create_subprocess_exec(self._executable, stdin=PIPE, stdout=PIPE, stderr=PIPE)`
- `asyncio.wait_for(proc.communicate(input=msg_bytes), timeout=effective_timeout)` — `communicate()` avoids deadlock
- On `asyncio.TimeoutError`: `proc.kill()` + `await proc.wait()` → raise `TransportTimeoutError`
- On non-zero `returncode`: raise `TransportError`
- On empty stdout: raise `TransportError`
- stderr always logged at DEBUG level
- `ProtocolResponse.model_validate_json(stdout)` — `ValidationError` → `TransportError`
- Health sends use `timeout_override=10` (FR-013)

---

### Phase D: AgentService (`src/coordinare/services/agent_service.py`)

- `dispatch_card`: wraps dispatch; catches `TransportError` → returns `{"status": "error"}`
- `check_status`: wraps status; catches all transport errors → `{"status": "unknown"}`
- `relay_feedback`: wraps relay_feedback; catches transport errors → `{"status": "error"}`
- `check_health`: wraps health with `timeout_override=10`; catches transport errors → `{"status": "unknown"}`

---

### Phase E: Config and Bootstrap

**`src/coordinare/config.py`** additions:
```python
agent_transport: Literal["subprocess", "ssh", "kubernetes"] = Field(default="subprocess")
agent_executable: str = ""
transport_timeout_seconds: int = Field(default=30, ge=1, le=300)
# SSH fields made optional:
agent_host: str = ""
agent_user: str = ""
agent_command: str | None = None   # Remove {card_context} validator
```

**`src/coordinare/__main__.py`** — `_build_transport(config)` factory:
```python
def _build_transport(config: ProjectConfiguration) -> AgentTransport:
    match config.agent_transport:
        case "subprocess":
            return SubprocessTransport(config.agent_executable, config.transport_timeout_seconds)
        case "ssh":
            return SshTransport()      # raises NotImplementedError
        case "kubernetes":
            return KubernetesTransport()  # raises NotImplementedError
        case _:
            raise ValueError(f"Unknown transport: {config.agent_transport!r}")
```

Wrap in startup (before graph compilation):
```python
try:
    transport = _build_transport(config)
except NotImplementedError as exc:
    logger.error("transport_not_implemented", transport=config.agent_transport, message=str(exc))
    sys.exit(1)
agent_service = AgentService(transport)
```

---

### Phase F: Graph Updates

**`src/coordinare/graph/state.py`**:
- Rename `check_status(self, card_id: str)` → `check_status(self, session_id: str)` in `AgentServiceProtocol`

**`src/coordinare/graph/nodes/monitor_agent.py`**:
- Replace call with `await agent.check_status(state.get("agent_dispatch", {}).get("session_id", ""))`

---

### Phase G: Mock Agent (`tests/fixtures/mock_agent.py`)

Standalone Python script:
- Reads ProtocolMessage JSON from `sys.stdin.read()`
- Selects scenario from `MOCK_AGENT_SCENARIO` env var
- Tracks call count via state file in `MOCK_AGENT_STATE_DIR` (default `/tmp/mock_agent_{scenario}/`)
- Writes ProtocolResponse JSON to `sys.stdout`
- Exits 0 always (transport errors are tested via pytest monkeypatching, not mock agent exit codes)

Built-in scenarios: `happy_path`, `blocked`, `error`, `busy`, `session_expired`

---

## Complexity Tracking

No constitution violations requiring justification.

---

## Risk Register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `asyncio.wait_for` doesn't kill subprocess on timeout | Low | Medium | Explicit `proc.kill()` + `proc.wait()` after TimeoutError (verified pattern) |
| Mock agent state file race condition in parallel tests | Low | Low | `MOCK_AGENT_STATE_DIR` uses `tmp_path` fixture per test via `monkeypatch.setenv` |
| `check_status(session_id)` rename breaks existing tests | Medium | Low | Update `test_monitor_agent.py` and `test_agent_ssh.py` in same PR |
| `agent_command` validator rejects empty string | Medium | Low | Make `str \| None = None`, validator only fires when non-None |
| `asyncssh` removal breaks other imports | Low | Medium | Grep all imports before removing; keep if still used elsewhere |
