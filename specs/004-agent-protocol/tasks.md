# Tasks: Agent Communication Protocol

**Input**: Design documents from `/specs/004-agent-protocol/`
**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US5)
- All paths are relative to repository root

---

## Phase 1: Setup

**Purpose**: Create package directory structure before any source files are written

- [ ] T001 Create `src/coordinare/transport/` package with `src/coordinare/transport/__init__.py`
- [ ] T002 [P] Create test package stubs: `tests/fixtures/`, `tests/unit/transport/__init__.py`, `tests/unit/protocol/__init__.py`, `tests/unit/services/`, `tests/contract/`, `tests/integration/`

---

## Phase 2: Foundational (Protocol Models + Transport Interface)

**Purpose**: Wire-protocol data models and abstract transport interface — MUST be complete before any user-story implementation or transport can be written

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete

- [ ] T003 Create `src/coordinare/protocol.py` with `ActionType = Literal["dispatch", "status", "relay_feedback", "health"]`
- [ ] T004 Add `StatusType = Literal["accepted","working","pr_opened","blocked","error","unknown","busy","acknowledged","session_expired"]` to `src/coordinare/protocol.py`
- [ ] T005 Add `ProtocolMessage(BaseModel)` with `action`, `session_id`, `payload` fields to `src/coordinare/protocol.py`
- [ ] T006 Add `ProtocolResponse(BaseModel)` with `status`, `session_id`, `reason`, `questions`, `pr_url`, `pr_node_id`, `progress` fields to `src/coordinare/protocol.py`
- [ ] T007 Add `generate_contracts(output_dir: Path) -> None` using `model_json_schema()` for both models to `src/coordinare/protocol.py`
- [ ] T008 Create `src/coordinare/transport/base.py` with `TransportError(RuntimeError)` and `TransportTimeoutError(TransportError)` (carrying `timeout: int`)
- [ ] T009 [P] Add `AgentTransport(Protocol)` with `async def send(self, message: ProtocolMessage, *, timeout_override: int | None = None) -> ProtocolResponse` to `src/coordinare/transport/base.py`
- [ ] T010 [P] Update `src/coordinare/transport/__init__.py` to export `AgentTransport`, `TransportError`, `TransportTimeoutError`
- [ ] T011 [P] Write `ProtocolMessage` and `ProtocolResponse` Pydantic validation unit tests in `tests/unit/protocol/test_protocol_models.py`
- [ ] T011a [P] Write `generate_contracts()` unit tests (writes two `.json` files to given output dir, both are valid JSON, files use expected naming) in `tests/unit/protocol/test_protocol_models.py`

**Checkpoint**: Protocol models and transport interface defined — transport implementations can now begin

---

## Phase 3: User Story 4 — Pluggable Transport Layer (Priority: P1)

**Goal**: Operator can select any registered transport in config; unimplemented transports fail fast at startup with a structured error, not a runtime traceback.

**Independent Test**: Set `agent_transport: ssh` in config, run coordinare, confirm structured `transport_not_implemented` log event and exit code 1 (no traceback).

- [ ] T012 [P] [US4] Create `SshTransport` stub in `src/coordinare/transport/ssh_transport.py` — `__init__` raises `NotImplementedError` with actionable message
- [ ] T013 [P] [US4] Create `KubernetesTransport` stub in `src/coordinare/transport/kubernetes_transport.py` — `__init__` raises `NotImplementedError` with actionable message
- [ ] T014 [US4] Add `agent_transport: Literal["subprocess","ssh","kubernetes"]`, `agent_executable: str`, `transport_timeout_seconds: int` (ge=1, le=300, default=30) to `src/coordinare/config.py`
- [ ] T015 [US4] Make `agent_host`, `agent_user`, `agent_command` optional (`str | None = None`) and remove `{card_context}` validator in `src/coordinare/config.py`
- [ ] T016 [US4] Add `_build_transport(config: ProjectConfiguration) -> AgentTransport` factory with `match config.agent_transport` in `src/coordinare/__main__.py`
- [ ] T017 [US4] Wrap `_build_transport` call in startup with `try/except NotImplementedError` → `logger.error("transport_not_implemented", ...)` + `sys.exit(1)` in `src/coordinare/__main__.py`

**Checkpoint**: Transport selection is pluggable; SSH and Kubernetes stubs produce structured startup errors

---

## Phase 4: User Story 1 — Dispatch a Card and Confirm Agent Acceptance (Priority: P1) 🎯 MVP

**Goal**: Coordinare dispatches a card to the agent via subprocess and receives a `ProtocolResponse` with `status: "accepted"` or `status: "busy"`.

**Independent Test**: With a mock agent subprocess that echoes `{"status":"accepted","session_id":"s1"}`, call `AgentService.dispatch_card(...)` and assert response status is `"accepted"`.

- [ ] T018 [US1] Create `SubprocessTransport.__init__(self, executable: str, timeout: int)` in `src/coordinare/transport/subprocess_transport.py`
- [ ] T019 [US1] Implement `SubprocessTransport.send()` body: `asyncio.create_subprocess_exec(executable, stdin=PIPE, stdout=PIPE, stderr=PIPE)` then `proc.communicate(input=msg_bytes)` wrapped in `asyncio.wait_for` in `src/coordinare/transport/subprocess_transport.py`
- [ ] T020 [US1] Add timeout branch: on `asyncio.TimeoutError` call `proc.kill()` + `await proc.wait()` then raise `TransportTimeoutError(timeout=effective_timeout)` in `src/coordinare/transport/subprocess_transport.py`
- [ ] T021 [US1] Add error branches: non-zero `returncode` → `TransportError`; empty stdout → `TransportError`; `ValidationError` from `ProtocolResponse.model_validate_json` → `TransportError` in `src/coordinare/transport/subprocess_transport.py`
- [ ] T022 [US1] Add `stderr` at DEBUG level and `duration_ms` log on every `send()` call in `src/coordinare/transport/subprocess_transport.py`
- [ ] T023 [US1] Create `AgentService.__init__(self, transport: AgentTransport)` and `dispatch_card(...)` method in `src/coordinare/services/agent_service.py` — payload MUST include all FR-010 required fields: `title`, `description`, `acceptance_criteria`, `card_id`, `column_name`; catches `TransportError` → `{"status": "error"}`
- [ ] T024 [US1] Add `check_health()` to `AgentService` sending health `ProtocolMessage` with `timeout_override=10`, catching errors → `{"status": "unknown"}` in `src/coordinare/services/agent_service.py`
- [ ] T025 [US1] Wire `AgentService(transport)` construction after `_build_transport` in `src/coordinare/__main__.py`
- [ ] T026 [P] [US1] Write `SubprocessTransport` unit tests (happy path, timeout kill+wait, non-zero exit, empty stdout, malformed JSON) in `tests/unit/transport/test_subprocess_transport.py`
- [ ] T027 [P] [US1] Write `AgentService.dispatch_card()` and `check_health()` unit tests with mock transport in `tests/unit/services/test_agent_service.py` — assert that the `ProtocolMessage.payload` sent by `dispatch_card()` contains all five FR-010 fields: `title`, `description`, `acceptance_criteria`, `card_id`, `column_name`

**Checkpoint**: Full dispatch path works end-to-end; US1 independently testable

---

## Phase 5: User Story 2 — Poll Agent Status During Long-Running Work (Priority: P1)

**Goal**: Graph `monitor_agent` node calls `check_status(session_id)` from the state's `agent_dispatch` dict; receives current `StatusType`; graph routes accordingly.

**Independent Test**: With a mock transport returning `{"status":"working","session_id":"s1"}`, call `AgentService.check_status("s1")` and assert `"working"` is returned.

- [ ] T028 [US2] Rename `check_status(self, card_id: str)` → `check_status(self, session_id: str)` in `AgentServiceProtocol` in `src/coordinare/graph/state.py`
- [ ] T029 [US2] Add `check_status(session_id: str)` to `AgentService`, catching all transport errors → `{"status": "unknown"}` in `src/coordinare/services/agent_service.py`
- [ ] T030 [US2] Update `monitor_agent.py` to read `session_id` from `state.get("agent_dispatch", {}).get("session_id", "")` and call `agent.check_status(session_id)` in `src/coordinare/graph/nodes/monitor_agent.py`
- [ ] T031 [US2] Add `check_status()` unit tests (all transport error branches → unknown) to `tests/unit/services/test_agent_service.py`
- [ ] T032 [US2] Update existing `test_monitor_agent.py` for renamed `check_status(session_id)` signature in `tests/unit/graph/test_monitor_agent.py`

**Checkpoint**: Status polling works; `session_id` flows correctly from state → service → transport

---

## Phase 6: User Story 3 — Relay Review Feedback to Agent (Priority: P2)

**Goal**: Coordinare sends reviewer feedback (questions answered) back to agent via `relay_feedback` action; agent acknowledges or errors.

**Independent Test**: With a mock transport returning `{"status":"acknowledged","session_id":"s1"}`, call `AgentService.relay_feedback(...)` and assert `"acknowledged"` is returned.

- [ ] T033 [US3] Add `relay_feedback(session_id: str, payload: dict)` to `AgentService` in `src/coordinare/services/agent_service.py` — payload MUST conform to FR-012 structure: `pr_url`, `comments` (ordered list of `{reviewer, body, file, line}` dicts); sends `relay_feedback` `ProtocolMessage`, catches `TransportError` → `{"status": "error"}`
- [ ] T034 [US3] Add `relay_feedback()` unit tests (success + transport error path) to `tests/unit/services/test_agent_service.py`

**Checkpoint**: Feedback relay path complete; all four `AgentService` operations covered

---

## Phase 7: User Story 5 — Subprocess Mock Agent for Integration Testing (Priority: P2)

**Goal**: `tests/fixtures/mock_agent.py` is a standalone executable that speaks the wire protocol; integration tests can run full cycles with no containers or network.

**Independent Test**: Run `python tests/fixtures/mock_agent.py` with `MOCK_AGENT_SCENARIO=happy_path`, pipe a dispatch `ProtocolMessage`, assert stdout is valid `ProtocolResponse` JSON with `status: "accepted"`.

- [ ] T035 [US5] Create `tests/fixtures/mock_agent.py` reading full stdin as JSON, validating with `ProtocolMessage.model_validate_json()`, selecting scenario from `MOCK_AGENT_SCENARIO` env var
- [ ] T036 [US5] Implement `happy_path` scenario with call-count state file (accepted → working → pr_opened progression) in `tests/fixtures/mock_agent.py`
- [ ] T037 [US5] Implement `blocked`, `error`, `busy`, `session_expired` scenarios in `tests/fixtures/mock_agent.py`
- [ ] T038 [US5] Add call-count tracking via temp file in `MOCK_AGENT_STATE_DIR` (default `/tmp/mock_agent_{scenario}/`) in `tests/fixtures/mock_agent.py`
- [ ] T039 [P] [US5] Write JSON Schema contract tests validating both `specs/004-agent-protocol/contracts/*.schema.json` files against live Pydantic output in `tests/contract/test_agent_protocol.py`
- [ ] T040 [P] [US5] Add contract test asserting `generate_contracts()` output matches checked-in schema files in `tests/contract/test_agent_protocol.py`
- [ ] T041 [US5] Write integration test for full `dispatch → status(working) → status(pr_opened)` cycle with `happy_path` mock agent in `tests/integration/test_agent_protocol_flow.py`
- [ ] T042 [US5] Add integration tests for error scenarios: malformed response, timeout, non-zero exit, unknown session in `tests/integration/test_agent_protocol_flow.py`
- [ ] T043 [US5] Add integration test asserting all 9 `StatusType` values exercised across scenarios in `tests/integration/test_agent_protocol_flow.py`
- [ ] T044 [US5] Add integration test asserting full `happy_path` cycle completes within 60s wall time in `tests/integration/test_agent_protocol_flow.py`

**Checkpoint**: All integration tests pass with mock agent; no containers or network required (SC-004)

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Remove dead code, validate observability, confirm quickstart

- [ ] T045 [P] Delete `src/coordinare/services/agent_ssh.py` (superseded by `SubprocessTransport` + `AgentService`; dead code per Constitution Principle I — git history preserves it)
- [ ] T045a [P] Remove `asyncssh` from `pyproject.toml` (no longer imported after T045 deletion); confirm no other `src/` files import it
- [ ] T046 [P] Confirm all `send()` calls emit `duration_ms` log and that health calls use `timeout_override=10` via log inspection in integration test run
- [ ] T047 Run quickstart.md validation: configure `agent_transport: subprocess`, launch coordinare with mock agent as `agent_executable`, observe `transport_not_implemented` for ssh variant

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Requires Phase 1 — BLOCKS all user stories
- **US4 (Phase 3)**: Requires Phase 2 (transport interface)
- **US1 (Phase 4)**: Requires Phase 2 (protocol models) + Phase 3 (config/factory)
- **US2 (Phase 5)**: Requires Phase 4 (`AgentService` exists; `check_status` is added to it)
- **US3 (Phase 6)**: Requires Phase 4 (`AgentService` exists; can run concurrently with US2)
- **US5 (Phase 7)**: Requires Phase 4 (`SubprocessTransport` working); contract tests require Phase 2
- **Polish (Phase 8)**: Requires all prior phases complete

### User Story Dependencies

| Story | Priority | Depends On | Can Run With |
|-------|----------|------------|--------------|
| US4 | P1 | Phase 2 | — |
| US1 | P1 | Phase 2, US4 | — |
| US2 | P1 | US1 | US3 |
| US3 | P2 | US1 | US2 |
| US5 | P2 | US1 | US2, US3 |

### Within Each Phase

- Protocol models (T003–T007) must be written before transport base (T008–T010)
- `AgentTransport` Protocol must exist before `SubprocessTransport` implements it
- `AgentService` (`dispatch_card`) must exist before `check_status` and `relay_feedback` are added
- Mock agent must exist before integration tests run

### Parallel Opportunities

- T002 runs in parallel with Phase 1 setup work; T009, T010, T011, T011a can all run in parallel after T007 is complete
- T003 → T004 → T005 → T006 → T007 are sequential (all write to the same file `protocol.py`)
- T012 and T013 (SSH and Kubernetes stubs) can be written in parallel
- T026 and T027 (transport and service unit tests) can run in parallel
- T031 and T034 (check_status and relay_feedback tests) can run in parallel
- T039 and T040 (contract tests) can run in parallel

---

## Parallel Example: Phase 4 (US1)

```bash
# After T018–T025 (SubprocessTransport + AgentService) are done, launch tests together:
Task: "SubprocessTransport unit tests in tests/unit/transport/test_subprocess_transport.py"
Task: "AgentService dispatch_card/check_health tests in tests/unit/services/test_agent_service.py"
```

---

## Implementation Strategy

### MVP First (US1 + US4 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks everything)
3. Complete Phase 3: US4 (pluggable factory + stubs)
4. Complete Phase 4: US1 (SubprocessTransport + AgentService dispatch)
5. **STOP and VALIDATE**: Dispatch a card via subprocess; confirm `accepted` or `busy` response
6. Proceed to US2, US3, US5 in order

### Incremental Delivery

1. Setup + Foundational → protocol models and transport interface defined
2. US4 → pluggable system with structured startup errors for unimplemented transports
3. US1 → first working dispatch flow (MVP!)
4. US2 → status polling works with `session_id`
5. US3 → feedback relay complete
6. US5 → full integration test coverage, no external dependencies
7. Polish → dead code removed, quickstart verified

---

## Notes

- [P] tasks write to different files with no incomplete-task dependencies
- Each user story is independently testable before proceeding to the next
- `mock_agent.py` must exit 0 always — transport error scenarios are exercised via monkeypatching in unit tests, not mock agent exit codes
- `MOCK_AGENT_STATE_DIR` must use `tmp_path` per-test fixture via `monkeypatch.setenv` to avoid parallel test races
- Verify `pytest` + `pytest-asyncio` pass before marking any phase complete
- Commit after each phase checkpoint
