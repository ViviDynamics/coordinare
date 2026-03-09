# Tasks: Performer

**Input**: Design documents from `specs/012-performer/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓, quickstart.md ✓

**Organization**: Tasks grouped by user story for independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to
- All paths relative to repo root

---

## Phase 1: Setup

**Purpose**: Create the `agent/performer/` package scaffold. No user story work can begin until this is complete.

- [X] T001 Create `agent/performer/` directory tree: `src/performer/`, `src/performer/backends/`, `tests/unit/`, `tests/unit/backends/`, `tests/integration/`, `agent/performer/images/full/`; create `agent/performer/.dockerignore` containing: `.git`, `__pycache__`, `*.pyc`, `*.pyo`, `.venv`, `venv`, `dist`, `*.egg-info`, `tests/`, `.env*`, `*.log`
- [X] T002 Write `agent/performer/pyproject.toml` with package name `performer`, Python `>=3.12`, dependencies `pydantic>=2.9`, `httpx>=0.27`, `psutil>=5.9`, `structlog>=24.1`, `pydantic-settings>=2.6`; dev extras `pytest>=8`, `pytest-asyncio>=0.23`, `pytest-cov>=5`, `respx>=0.21`; entry point `performer = performer.main:main`; include `[tool.pytest.ini_options]` with `asyncio_mode = "auto"`; include `[tool.coverage.report]` with `fail_under = 90`
- [X] T003 [P] Write `agent/performer/src/performer/__init__.py` (empty), `agent/performer/src/performer/backends/__init__.py` placeholder, and `agent/performer/src/performer/__main__.py` containing `from performer.main import main; main()` — required for `python -m performer` invocation used throughout acceptance tests and quickstart
- [X] T004 [P] Write `agent/performer/tests/conftest.py` with shared pytest fixtures (tmp_path, event_loop policy) and `agent/performer/tests/unit/backends/__init__.py`

---

## Phase 2: Foundational

**Purpose**: Core models and interfaces that ALL user stories depend on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T005 Implement `PerformerMessage`, `PerformerResponse`, and `PerformerMetrics` pydantic models in `agent/performer/src/performer/protocol.py` — mirror `coordinare.protocol.ProtocolMessage`/`ProtocolResponse` field-for-field; add optional `metrics: PerformerMetrics | None = None` field to `PerformerResponse`
- [X] T006 [P] Implement `Settings` in `agent/performer/src/performer/config.py` using `pydantic-settings`: `AGENT_BACKEND: str = "opencode"`, `AGENT_TIMEOUT: int = 1800`; expose `get_settings()` cached singleton
- [X] T007 Implement `BackendAdapter` runtime-checkable `Protocol` and `BackendStatus` dataclass in `agent/performer/src/performer/backends/base.py` — protocol methods: `start(stand, score)`, `get_status() → BackendStatus`, `relay_feedback(feedback: str)`, `stop()`; `BackendStatus` fields: `state: Literal["working","blocked","done","error"]`, `questions: list[str]`, `error_reason: str | None`, `tokens_processed: int | None`
- [X] T008 Implement `Score`, `Stand`, and `Performance` dataclasses in `agent/performer/src/performer/models.py` (after T007 — `Performance.backend` references `BackendAdapter`); fields match data-model.md exactly; `Score` validates `repo_url` format and non-empty `github_token`; `Stand` holds `path: Path`, `branch: str`, `created_at: datetime`; `Performance` holds `session_id`, `stand`, `score`, `state`, `backend`, `pr_url`, `pr_node_id`, `open_questions`, `error_reason`, `started_at`

- [X] T036 [P] Write `agent/performer/tests/unit/test_protocol.py`: test `PerformerMessage` accepts all four action types and rejects unknown ones; test `PerformerResponse` serialises all defined status literal values correctly; test `PerformerMetrics` all fields are nullable/omittable; test that `PerformerResponse` with `metrics=None` serialises without the `metrics` key (constitution Principle II — public interface unit tests for protocol.py)
- [X] T037 Write `agent/performer/tests/unit/test_models.py` (after T008): test `Score` raises `ValidationError` for an invalid `repo_url` pattern and for a blank `github_token`; test `Score` accepts a valid GitHub HTTPS URL; test `Stand` holds `path`, `branch`, `created_at`; test `Performance` initial state is `accepted` (constitution Principle II — public interface unit tests for models.py)
- [X] T038 [P] Write `agent/performer/tests/unit/test_config.py` (after T006): test `Settings()` defaults — `AGENT_BACKEND` is `"opencode"`, `AGENT_TIMEOUT` is `1800`; test env var override sets `AGENT_BACKEND` and `AGENT_TIMEOUT` correctly; test `get_settings()` returns the same instance on two consecutive calls (singleton caching); test `get_settings.cache_clear()` causes a fresh `Settings` to be constructed (constitution Principle II — public interface unit tests for config.py)

**Checkpoint**: Foundation ready — all seven foundational files exist, type-check, and have unit tests before proceeding.

---

## Phase 3: User Story 1 — Perform a Score End-to-End (Priority: P1) 🎯 MVP

**Goal**: Card-in, PR-out. The performer receives a dispatch, clones the repo, runs opencode, pushes the branch, and opens a GitHub pull request.

**Independent Test**: `echo '<dispatch_payload>' | python -m performer` returns `{"status":"accepted","session_id":"..."}`, then subsequent `status` polls return `working` until the backend finishes and then return `pr_opened` with a real PR URL.

### Implementation for User Story 1

- [X] T009 [P] [US1] Implement `clone_repository(score: Score) → Stand` and `push_branch(stand: Stand, score: Score) → None` and `cleanup_stand(stand: Stand) → None` in `agent/performer/src/performer/workspace.py`; `clone_repository` uses `git clone --depth=1 https://x-access-token:{token}@github.com/{owner}/{repo}.git {tmpdir}` via `asyncio.create_subprocess_exec`; implement `_redact_tokens(text: str) → str` (regex replacing `x-access-token:[^@]*@` with `x-access-token:***@`) and apply it to all git stderr before logging or surfacing in errors; catch `OSError` with `errno.ENOSPC` during clone/write operations and raise `WorkspaceSetupError("insufficient disk space")`; `push_branch` detects non-zero exit on branch conflict and raises `BranchConflictError`; `cleanup_stand` calls `shutil.rmtree(stand.path, ignore_errors=True)`
- [X] T010 [P] [US1] Implement `get_default_branch(owner: str, repo: str, token: str) → str` and `create_pull_request(owner: str, repo: str, score: Score, branch: str, token: str) → tuple[str, str]` in `agent/performer/src/performer/github.py` using `httpx.AsyncClient`; `create_pull_request` returns `(html_url, node_id)`; PR title = `score.title`; PR body = `score.description` + formatted acceptance criteria; raises `GitHubAPIError` on non-2xx responses
- [X] T011 [US1] Implement `OpenCodeAdapter` in `agent/performer/src/performer/backends/opencode.py`: `start()` launches `opencode acp` with `asyncio.create_subprocess_exec(..., start_new_session=True)`, sends initial task as nd-JSON to stdin, spawns background `asyncio.Task` running `_event_reader_loop()` that parses nd-JSON lines and updates internal `_status: BackendStatus`; `get_status()` returns `self._status` (non-blocking); `relay_feedback(feedback)` writes nd-JSON message to `proc.stdin`; `stop()` calls `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` with `psutil` fallback; event reader maps: `session.idle` → `state="done"`, `session.error` → `state="error"`, `session.message` with a `needsInput` or `question` field → `state="blocked"` with questions populated; **NOTE**: the exact nd-JSON event type for blocked state in opencode ACP is NEEDS CLARIFICATION — consult opencode ACP docs before implementing; treat any unrecognised event as a no-op (keep current state)
- [X] T012 [US1] Create `UnsupportedBackendError(ValueError)` class and a stub `get_backend(name: str) → BackendAdapter` function in `agent/performer/src/performer/backends/__init__.py` that always raises `UnsupportedBackendError(name)` — minimal scaffold so T013/T014 can import the factory; T026 (Phase 6) completes the real implementation with the backend registry
- [X] T013 [US1] Implement the async message loop in `agent/performer/src/performer/main.py`: `async def run_loop() → None` reads JSON from `sys.stdin` line-by-line, parses each as `PerformerMessage`, dispatches to `handle_dispatch`, `handle_status`, `handle_relay_feedback`, or `handle_health`; on unknown action type write `PerformerResponse(status="error", reason=f"unknown action: {msg.action}")` and continue the loop (do not exit); writes `PerformerResponse.model_dump_json()` to `sys.stdout`, flushes; exits loop when terminal state reached or stdin closes; also add `def main() -> None: import asyncio; asyncio.run(run_loop())` — this is the entry point referenced in pyproject.toml and called by `__main__.py`
- [X] T014 [US1] Implement `handle_dispatch(msg: PerformerMessage, settings: Settings) → tuple[PerformerResponse, Performance]` in `agent/performer/src/performer/main.py`: parses `Score` from `msg.payload`, calls `clone_repository(score)` to create `Stand`, calls `get_backend(settings.AGENT_BACKEND)` and `backend.start(stand, score)`, generates UUID4 `session_id`, returns `PerformerResponse(status="accepted", session_id=...)` and the new `Performance`; the full backend lifecycle is wrapped in `asyncio.timeout(settings.AGENT_TIMEOUT)` in the caller
- [X] T015 [US1] Implement `handle_status(msg: PerformerMessage, perf: Performance | None) → PerformerResponse` in `agent/performer/src/performer/main.py`: validates `session_id`; if no active performance returns `session_expired`; calls `perf.backend.get_status()`; on `done` calls `push_branch` then `create_pull_request` and returns `pr_opened` with `pr_url` and `pr_node_id`; on `blocked` returns `blocked` with questions; on `error` returns `error` with reason; on `working` returns `working` with progress (metrics wired in T024)
- [X] T016 [US1] Implement `handle_relay_feedback(msg: PerformerMessage, perf: Performance | None) → PerformerResponse` in `agent/performer/src/performer/main.py`: validates session; extracts `feedback` string from `msg.payload`; calls `perf.backend.relay_feedback(feedback)`; returns `PerformerResponse(status="acknowledged", session_id=...)`
- [X] T017 [US1] Write unit tests for `workspace.py` in `agent/performer/tests/unit/test_workspace.py`: test clone creates a temp dir and checks out the branch (mock subprocess); test push succeeds on zero exit; test push raises `BranchConflictError` on non-zero exit with stderr matching branch-exists pattern; test cleanup removes the directory; test clone failure raises `WorkspaceSetupError`; test that the `github_token` value does NOT appear in any raised exception message or log output (token redaction assertion); test that an `OSError(errno.ENOSPC, ...)` during clone raises `WorkspaceSetupError("insufficient disk space")`
- [X] T018 [P] [US1] Write unit tests for `github.py` in `agent/performer/tests/unit/test_github.py`: test `create_pull_request` returns `(html_url, node_id)` (mock httpx with `respx`); test API 422 raises `GitHubAPIError`; test `get_default_branch` returns the default branch from the repo response
- [X] T019 [P] [US1] Write unit tests for `OpenCodeAdapter` in `agent/performer/tests/unit/backends/test_opencode.py`: test `start()` launches subprocess and sends nd-JSON; test `get_status()` returns `working` initially; test event `session.idle` transitions state to `done`; test `relay_feedback()` writes nd-JSON to stdin; test `stop()` kills process group; test timeout kills process and transitions to error
- [X] T020 [US1] Write unit tests for `handle_dispatch()` and `handle_relay_feedback()` in `agent/performer/tests/unit/test_main.py`: test dispatch returns `accepted` + UUID session_id; test dispatch with invalid payload returns `error`; test relay_feedback with no active session returns `session_expired`; test relay_feedback calls `backend.relay_feedback` and returns `acknowledged`; **assert** `accepted` response is written within 5.0 s of dispatch receipt using `time.monotonic()` (automated SC-003 budget verification — constitution Principle IV)

**Checkpoint**: US1 complete — `python -m performer` accepts a dispatch, runs a mock backend, and returns `pr_opened` with PR URL.

---

## Phase 4: User Story 2 — Health Check Before Dispatch (Priority: P2)

**Goal**: Performer responds to `{"action":"health"}` within 2 seconds with `healthy` or `unhealthy` + reason.

**Independent Test**: `echo '{"action":"health","session_id":"","payload":{}}' | python -m performer` returns `{"status":"healthy"}` in under 2 seconds.

- [X] T021 [US2] Implement `handle_health(settings: Settings) → PerformerResponse` in `agent/performer/src/performer/main.py`: calls `get_backend(settings.AGENT_BACKEND)` — if it raises `UnsupportedBackendError` returns `PerformerResponse(status="unhealthy", reason=str(exc))`; otherwise returns `PerformerResponse(status="healthy")`; no I/O on the critical path — must complete in constant time
- [X] T022 [US2] Write unit tests for `handle_health()` in `agent/performer/tests/unit/test_main.py`: test valid `AGENT_BACKEND` returns `healthy`; test invalid backend name returns `unhealthy` with reason containing the backend name; test response is synchronous (no await needed); **assert** response time ≤ 2.0 s using `time.monotonic()` (automated SC-002 budget verification — constitution Principle IV)

**Checkpoint**: US2 complete — health check returns within constant time regardless of backend state.

---

## Phase 5: User Story 3 — Status Polling During a Performance (Priority: P2)

**Goal**: Status polls during a running performance return `working` + `PerformerMetrics`; blocked/error paths return structured data.

**Independent Test**: Dispatch a mock performance, immediately poll status — verify `working` with `metrics.pid` present; let backend finish — verify `pr_opened`.

- [X] T023 [US3] Implement `collect_metrics(backend: BackendAdapter) → PerformerMetrics` in `agent/performer/src/performer/main.py` using `psutil`: initialise a `psutil.Process(os.getpid())` once at startup and call `cpu_percent(interval=None)` to prime it; on each call collect `pid=os.getpid()`, `child_pids` from `p.children(recursive=True)`, `memory_bytes` as sum of tree RSS, `cpu_percent` from primed process; wrap all psutil calls in `try/except psutil.NoSuchProcess` and set missing fields to `None`; include `tokens_processed` from `BackendStatus.tokens_processed`
- [X] T024 [US3] Wire `collect_metrics()` into `handle_status()` — attach metrics to `working` responses only; leave metrics as `None` for `blocked`, `error`, `pr_opened`, `session_expired` responses
- [X] T025 [US3] Write unit tests for `collect_metrics()` and metrics in status responses in `agent/performer/tests/unit/test_main.py`: test `working` response contains `metrics` with `pid` field; test `blocked` response has `metrics=None`; test `collect_metrics()` gracefully returns partial metrics when `psutil.NoSuchProcess` is raised mid-collection

**Checkpoint**: US3 complete — coordinare can observe backend health via metrics on every `working` poll.

---

## Phase 6: User Story 4 — Configurable AI Backend (Priority: P3)

**Goal**: `AGENT_BACKEND` env var selects the backend; unsupported values surface as `unhealthy` on health check.

**Independent Test**: Run performer with `AGENT_BACKEND=opencode` — health returns `healthy`. Run with `AGENT_BACKEND=foobar` — health returns `unhealthy` with reason containing `foobar`.

- [X] T026 [US4] Complete `get_backend()` factory in `agent/performer/src/performer/backends/__init__.py`: replace the stub from T012 with `SUPPORTED_BACKENDS: dict[str, type[BackendAdapter]] = {"opencode": OpenCodeAdapter}`; ensure `UnsupportedBackendError` message includes the invalid name AND the sorted list of supported backend names (e.g. `"unsupported backend 'foobar'; supported: opencode"`)
- [X] T027 [US4] Write unit tests for the backend factory in `agent/performer/tests/unit/backends/test_base.py`: test `get_backend("opencode")` returns an `OpenCodeAdapter` instance; test `get_backend("foobar")` raises `UnsupportedBackendError` with `"foobar"` in the message; test `get_backend("opencode")` with `AGENT_BACKEND` env var set; test the health handler returns `unhealthy` with the invalid backend name in the reason

**Checkpoint**: US4 complete — switching `AGENT_BACKEND` is a single env var change; invalid values produce a clear error.

---

## Phase 7: User Story 5 — Two-Tier Image Strategy (Priority: P3)

**Goal**: Base image contains only the performer entrypoint + opencode. Full image adds Node.js LTS, Python 3.12 pip/venv, build-essential.

**Independent Test**: `docker run coordinare-performer:base which node` exits non-zero. `docker run coordinare-performer:full node --version` succeeds. Both images respond to health check.

- [X] T028 [US5] Write `agent/performer/Dockerfile` (base image): `FROM python:3.12-slim`; install `git curl ca-certificates`; install opencode CLI via `curl -fsSL https://opencode.ai/install.sh | sh`; `COPY pyproject.toml README.md src/` and `RUN pip install --no-cache-dir .`; `ENTRYPOINT ["python", "-m", "performer"]`; no language runtimes beyond Python 3.12
- [X] T029 [P] [US5] Write `agent/performer/images/full/Dockerfile` (full image): `FROM coordinare-performer:base`; install `build-essential make python3-pip python3-venv`; add NodeSource LTS repo and install `nodejs`; do NOT override `ENTRYPOINT`
- [X] T030 [P] [US5] Add `performer_image: str = "coordinare-performer:full"` field to `ProjectConfiguration` in `src/coordinare/config.py`; add corresponding entry to coordinare's config YAML schema documentation comment; add a unit test in `tests/unit/test_config.py` (coordinare's existing test file) verifying the field parses correctly with the default and from env/YAML override

**Checkpoint**: US5 complete — both image tiers build successfully and are distinctly sized.

---

## Phase 8: User Story 6 — Stateless Isolation Between Performances (Priority: P3)

**Goal**: Stand is cleaned up after every performance regardless of outcome; no state leaks between runs.

**Independent Test**: Run two sequential dispatches in the same process; verify the first stand's `tmpdir` is deleted before the second dispatch begins.

- [X] T031 [US6] Verify the `finally` block in the message loop in `agent/performer/src/performer/main.py` calls `backend.stop()` then `cleanup_stand(stand)` unconditionally after every performance — including on `asyncio.TimeoutError`, `BranchConflictError`, `GitHubAPIError`, and unexpected exceptions; add `asyncio.TimeoutError` handling that transitions session to `error` state before cleanup
- [X] T032 [US6] Write unit tests for isolation in `agent/performer/tests/unit/test_main.py`: test that `cleanup_stand` is called after `pr_opened` terminal state; test that `cleanup_stand` is called after `error` terminal state; test that `backend.stop()` is called even when `push_branch` raises an exception; test that a second dispatch in the same loop starts with a fresh `Stand` (different `tmpdir` path); **assert** that `cleanup_stand` is invoked within 30 s of the terminal state using `time.monotonic()` (automated SC-004 budget verification — constitution Principle IV)

**Checkpoint**: US6 complete — stand cleanup is guaranteed; credentials never persist across performance boundaries.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Protocol compliance verification, end-to-end integration test, and operator documentation.

- [X] T033 Write protocol contract test in `agent/performer/tests/unit/test_protocol_contract.py`: add `"coordinare @ file://../../"` (PEP 508 local path — pip-compatible) to the `dev` optional dependency list in performer's `pyproject.toml`; import `coordinare.protocol.ProtocolResponse` and `performer.protocol.PerformerResponse`; assert that all fields present in `ProtocolResponse.model_json_schema()` exist in `PerformerResponse.model_json_schema()` with compatible types; assert `PerformerResponse` adds `metrics` field not present in coordinare's schema
- [X] T034 Write end-to-end integration test in `agent/performer/tests/integration/test_performance.py`: create a bare git repo in `tmp_path`; implement mock opencode ACP subprocess as `agent/performer/tests/fixtures/mock_opencode_acp.py` — a standalone Python script that reads nd-JSON from stdin, sleeps 0.1 s, writes `{"type":"session.idle"}` to stdout, then exits; mock GitHub API PR creation with `respx`; run the full message loop with a `dispatch` message followed by `status` polls; assert final response is `pr_opened` with `pr_url` and `pr_node_id`; assert stand directory is deleted after the loop exits
- [X] T035 Write `agent/performer/README.md` per FR-014: sections — (1) Build base image, (2) Build full image, (3) Extend base image for custom stack (Dockerfile example), (4) Run a local test performance (dispatch payload example + status poll commands), (5) Verify protocol compliance (run the contract test), (6) Environment variables reference table
- [X] T039 [P] Verify two-tier image size separation: run `docker inspect --format='{{.Size}}' coordinare-performer:base` and `coordinare-performer:full`; assert `full_size > base_size + 150_000_000` (≥ 150 MB gap representing Node.js LTS runtime; actual measured gap ~167 MB); document the sizes in Phase 9 output (automated SC-007 budget verification — constitution Principle IV)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — **blocks all user stories**
- **Phase 3 (US1)**: Depends on Phase 2 — no dependencies on other US phases
- **Phase 4 (US2)**: Depends on Phase 2 + Phase 3 (handle_health lives in main.py)
- **Phase 5 (US3)**: Depends on Phase 3 (extends handle_status)
- **Phase 6 (US4)**: Depends on Phase 2 + Phase 3 (extends factory and health)
- **Phase 7 (US5)**: Depends on Phase 1 only (Dockerfiles) + Phase 2 (performer_image in coordinare config is independent)
- **Phase 8 (US6)**: Depends on Phase 3 (verifies finally-block behaviour in main.py)
- **Phase 9 (Polish)**: Depends on all phases complete

### User Story Dependencies

- **US1 (P1)**: Can start after Foundational — no dependency on other stories
- **US2 (P2)**: Can start after US1 (handle_health lives in main.py alongside US1 handlers)
- **US3 (P2)**: Can start after US1 (extends handle_status already implemented)
- **US4 (P3)**: Can start after US1 (extends factory; health handler from US2 must exist)
- **US5 (P3)**: Dockerfiles independent; coordinare `performer_image` field independent
- **US6 (P3)**: Can start after US1 (verifies isolation guarantee in main.py)

### Parallel Opportunities Within Phases

- **Phase 1**: T003 ∥ T004
- **Phase 2**: T006 ∥ T007 (after T005; T008 after T007); T036 ∥ T037 ∥ T038 (after T005, T008, and T006 respectively — all touch different test files)
- **Phase 3**: T009 ∥ T010 (workspace and github are independent); T017 ∥ T018 ∥ T019 (unit tests in different files)
- **Phase 7**: T029 ∥ T030 (full Dockerfile and coordinare config field are independent)

---

## Parallel Example: User Story 1

```text
# T009 and T010 can run simultaneously (different files):
Task A: "Implement workspace.py (clone, push, cleanup)"
Task B: "Implement github.py (PR creation, default branch)"

# After T011 and T012 complete, T013-T016 are sequential (same file: main.py)

# T017, T018, T019 can run simultaneously (different test files):
Task A: "Write tests/unit/test_workspace.py"
Task B: "Write tests/unit/test_github.py"
Task C: "Write tests/unit/backends/test_opencode.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 (T009–T020)
4. **STOP and VALIDATE**: `echo '<dispatch>' | python -m performer` → working → pr_opened
5. The MVP performer can accept a score and deliver a pull request

### Incremental Delivery

1. Setup + Foundational → scaffold ready
2. US1 → card-in PR-out (MVP — the entire point of the system)
3. US2 → health check (coordinare can route before dispatching)
4. US3 → status metrics (coordinare gains visibility into long-running sessions)
5. US4 → backend swap (operator can switch AI agent without rebuild)
6. US5 → container images (operators can pull and run without building)
7. US6 → isolation guarantees (verified, tested)
8. Polish → contract test, integration test, README

### Total Task Count

- Phase 1 (Setup): 4 tasks
- Phase 2 (Foundational): 7 tasks
- Phase 3 (US1): 12 tasks
- Phase 4 (US2): 2 tasks
- Phase 5 (US3): 3 tasks
- Phase 6 (US4): 2 tasks
- Phase 7 (US5): 3 tasks
- Phase 8 (US6): 2 tasks
- Phase 9 (Polish): 4 tasks
- **Total: 39 tasks**

---

## Notes

- `[P]` tasks touch different files and have no incomplete dependencies — safe to run concurrently
- `[Story]` labels enable tracing each task to its acceptance scenario in spec.md
- All tasks include exact file paths — no ambiguity about where code goes
- Commit after each logical group (e.g., after T009+T010, after T011+T012)
- Validate each user story independently before proceeding to the next
- The contract test (T033) installs `coordinare` as a local editable dev dependency (`coordinare @ file://../../`) — run `pip install -e ".[dev]"` in the performer package before executing it
