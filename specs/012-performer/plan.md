# Implementation Plan: Performer

**Branch**: `012-performer` | **Date**: 2026-03-03 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `specs/012-performer/spec.md`

---

## Summary

Implement the performer — the other side of the coordinare wire protocol. The performer is a Python 3.12 process that lives in `agent/performer/` within the coordinare repo, packaged as a Docker container. It reads `ProtocolMessage` JSON from stdin in a long-lived loop for one full performance (dispatch → status polls → terminal state), delegates coding work to a pluggable AI backend adapter (opencode by default using `opencode acp` mode), manages an ephemeral git workspace, opens a GitHub pull request when the backend finishes, and emits `PerformerResponse` JSON to stdout. It is stateless across performances — all state is in-memory and cleared when the process exits.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `pydantic>=2.9`, `httpx>=0.27`, `psutil>=5.9`, `structlog>=24.1` (new to performer package; all already in coordinare root)
**Storage**: None — all state is in-memory; temporary git workspace in OS temp dir (`tempfile.mkdtemp`)
**Testing**: `pytest`, `pytest-asyncio`, `pytest-cov` — same toolchain as coordinare
**Target Platform**: Linux container (Docker); development on macOS
**Performance Goals**:
- Health check response: ≤ 2 seconds (SC-002)
- Dispatch acceptance (session_id returned): ≤ 5 seconds (SC-003)
- Stand cleanup after performance: ≤ 30 seconds (SC-004)
**Constraints**: No persistent storage; no network access except GitHub API and git clone; stateless between performances
**Scale/Scope**: One dispatch at a time per container instance; horizontal scaling via multiple container instances

---

## Constitution Check

*GATE: Must pass before proceeding. Re-check after design.*

| Principle | Status | Notes |
|---|---|---|
| I. Code Quality | PASS | Modules have single responsibilities; type annotations required on all public interfaces; pydantic for validation boundaries |
| II. Testing Discipline | PASS | Unit tests for all modules; contract test for protocol schema conformance; integration test for end-to-end dispatch → PR; coverage minimum maintained |
| III. UX Consistency | N/A | No user-facing UI; error messages in protocol responses are human-readable and actionable (FR-005, FR-006) |
| IV. Performance by Design | PASS | SC-002/003/004 define measurable budgets; health check has no async I/O on the critical path |
| V. Clarity Before Action | PASS | All four clarification questions answered; no NEEDS CLARIFICATION markers remain |

**No constitution violations. Proceeding.**

---

## Project Structure

### Documentation (this feature)

```text
specs/012-performer/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
├── quickstart.md        ← Phase 1 output
├── contracts/
│   ├── performer-message.schema.json
│   └── performer-response.schema.json
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code

```text
agent/performer/                          ← new top-level directory in repo
├── Dockerfile                            ← base image (Python 3.12 + opencode CLI only)
├── images/
│   └── full/
│       └── Dockerfile                    ← full image (FROM base + Node.js LTS + Python 3.12 + build tools)
├── .dockerignore
├── README.md                             ← FR-014: build, extend, test, verify instructions
├── pyproject.toml                        ← performer's own package; performer[dev] extras for testing
└── src/
    └── performer/
        ├── __init__.py
        ├── main.py                       ← entrypoint: message loop (stdin → handler → stdout)
        ├── config.py                     ← Settings: AGENT_BACKEND, AGENT_TIMEOUT (pydantic-settings)
        ├── protocol.py                   ← PerformerMessage, PerformerResponse, PerformerMetrics (local pydantic models)
        ├── workspace.py                  ← Stand: git clone, checkout, push, shutil.rmtree cleanup
        ├── github.py                     ← PR creation via httpx; default branch detection
        └── backends/
            ├── __init__.py               ← get_backend(name: str) → BackendAdapter factory
            ├── base.py                   ← BackendAdapter Protocol; BackendStatus dataclass
            └── opencode.py               ← OpenCodeAdapter: opencode acp stdin/stdout nd-JSON

tests/                                    ← alongside src/ within agent/performer/
├── unit/
│   ├── test_main.py                      ← message loop dispatch, health, unknown action
│   ├── test_config.py                    ← env var parsing, defaults, invalid AGENT_BACKEND
│   ├── test_workspace.py                 ← clone, push, cleanup; error paths (bad token, branch conflict, disk full)
│   ├── test_github.py                    ← PR creation; default branch detection; API error handling
│   ├── test_protocol_contract.py         ← schema diff: PerformerResponse vs coordinare ProtocolResponse
│   └── backends/
│       ├── test_base.py                  ← BackendAdapter Protocol compliance check
│       └── test_opencode.py              ← OpenCodeAdapter: start, get_status, relay_feedback, stop; timeout
└── integration/
    └── test_performance.py               ← end-to-end: dispatch → working → pr_opened (bare git repo + mock opencode)
```

---

## Architecture

### Message Loop (`main.py`)

The performer's entrypoint reads JSON messages from stdin one at a time, dispatches to the appropriate handler, writes the JSON response to stdout, and loops. The loop exits when a terminal state is reached (pr_opened, error) or when stdin closes.

```
stdin → parse PerformerMessage
       ↓
  action == "dispatch"      → handle_dispatch() → returns "accepted" + session_id
  action == "status"        → handle_status()   → returns current session state
  action == "relay_feedback"→ handle_feedback() → forwards to backend; returns "acknowledged"
  action == "health"        → handle_health()   → validates config; returns "healthy"/"unhealthy"
       ↓
  write PerformerResponse to stdout
       ↓
  if terminal state (pr_opened | error): exit loop
```

The performance object is held in-memory on the main loop's stack. When the loop exits, the performance's `finally` block cleans up the stand via `shutil.rmtree`.

### Backend Lifecycle (within dispatch handling)

```
handle_dispatch():
  1. Parse Score from payload
  2. Create Stand (mkdtemp)
  3. Clone repository → Stand.path
  4. Load BackendAdapter via get_backend(config.AGENT_BACKEND)
  5. backend.start(stand, score)           ← launches opencode acp in background asyncio task
  6. Return "accepted" + uuid session_id   ← immediately, before backend finishes

  [subsequent status polls drive the loop]

handle_status():
  1. Validate session_id
  2. status = backend.get_status()
  3. If status.state == "done": push branch → create PR → return "pr_opened"
  4. If status.state == "blocked": return "blocked" + questions
  5. If status.state == "error": return "error" + reason
  6. Else: return "working" + progress + metrics

[in finally]:
  backend.stop()
  shutil.rmtree(stand.path, ignore_errors=True)
```

### OpenCodeAdapter (`opencode acp` mode)

The opencode ACP adapter launches `opencode acp` as a subprocess. It maintains a background asyncio task that reads nd-JSON events from the process's stdout and updates an internal `BackendStatus` object. The performer polls this status object on each `status` action — no blocking I/O on the polling path.

```
start():
  proc = asyncio.create_subprocess_exec("opencode", "acp",
      stdin=PIPE, stdout=PIPE, stderr=PIPE, start_new_session=True)
  send initial task as nd-JSON to proc.stdin
  launch _event_reader_task(proc.stdout) as asyncio background task

_event_reader_task():
  for each line in proc.stdout:
      event = parse_nd_json(line)
      if event.type == "session.idle": self._status = BackendStatus(state="done")
      if event.type == "session.error": self._status = BackendStatus(state="error", ...)
      if event.type == "message.part.updated": update self._progress

get_status() → self._status   (non-blocking)

relay_feedback(feedback):
  write nd-JSON message to proc.stdin

stop():
  os.killpg(os.getpgid(proc.pid), SIGKILL)   # kill entire process group
  await proc.wait()
```

### Timeout Enforcement

The performer's message loop wraps the backend lifecycle in an `asyncio.timeout(config.AGENT_TIMEOUT)` context. When the timeout fires, the backend is stopped, the stand is cleaned up, and the next `status` poll returns `{"status": "error", "reason": "backend timed out after Ns"}`.

### Metrics Collection (`psutil`)

On each `status` response, the performer calls psutil to collect live metrics:
- `psutil.Process(os.getpid()).memory_info().rss` — performer RSS
- Sum across all child PIDs for total tree RSS
- `p.cpu_percent(interval=None)` — non-blocking CPU percent (initialised at process start)
- `p.children(recursive=True)` — child PIDs

All metric collection is wrapped in `try/except psutil.NoSuchProcess` to handle races gracefully. Missing fields are set to `None`.

---

## Dockerfiles

### Base Image (`agent/performer/Dockerfile`)

```dockerfile
FROM python:3.12-slim

# Install git (required for clone/push) and curl (opencode install)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install opencode CLI
RUN curl -fsSL https://opencode.ai/install.sh | sh

# Install performer package
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "performer"]
```

### Full Image (`agent/performer/images/full/Dockerfile`)

```dockerfile
FROM coordinare-performer:base

# Add common language runtimes and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    # POSIX build utilities
    build-essential make \
    # Python 3.12 (already in base via python:3.12-slim — exposed here for project use)
    python3-pip python3-venv \
    # Node.js LTS (via NodeSource)
    && curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Entrypoint inherited from base — do not override
```

---

## Key Design Decisions

1. **Long-lived loop per performance**: The performer process stays alive for one full performance (dispatch through terminal state). Status polls and relay_feedback messages are handled within the same process. State is in-memory; no filesystem state store needed.

2. **opencode acp over opencode run**: The ACP mode (nd-JSON bidirectional stdin/stdout protocol) keeps the opencode process alive across relay_feedback calls. No process restart needed for feedback delivery.

3. **Local protocol models**: `PerformerMessage` and `PerformerResponse` are defined locally in `performer/protocol.py` — mirroring coordinare's schemas exactly — to keep the container lean. A contract test (`test_protocol_contract.py`) validates schema equivalence.

4. **metrics is an extension field**: `PerformerResponse.metrics` is not in coordinare's current `ProtocolResponse`. Pydantic drops unknown fields on deserialization, so no coordinare change is needed until it explicitly adopts the field.

5. **start_new_session=True for opencode**: Puts opencode in its own process group, enabling `os.killpg` to kill the entire process tree (opencode + any Node.js workers) on timeout or stop — no orphaned grandchildren.

6. **performer_image in coordinare config**: Spec US5 requires coordinare to use `performer_image` from config when creating K8s Jobs. This field does not yet exist in `coordinare/config.py`. Adding it is in-scope for this feature: add `performer_image: str = "coordinare-performer:full"` to `ProjectConfiguration`.

---

## Complexity Tracking

No constitution violations requiring justification.

---

## Phases

### Phase 1 — Scaffold & Protocol Foundation (unblocks everything)

- `agent/performer/` directory, `pyproject.toml`, `.dockerignore`
- `performer/protocol.py` — `PerformerMessage`, `PerformerResponse`, `PerformerMetrics`
- `performer/config.py` — `Settings` (AGENT_BACKEND, AGENT_TIMEOUT)
- `tests/unit/test_protocol_contract.py` — schema conformance
- `tests/unit/test_config.py`

### Phase 2 — Workspace & GitHub (US1 prerequisites)

- `performer/workspace.py` — clone, checkout, push, cleanup, branch-conflict detection
- `performer/github.py` — PR creation, default branch detection
- `tests/unit/test_workspace.py`
- `tests/unit/test_github.py`

### Phase 3 — Backend Abstraction + OpenCode Adapter (US1, US4)

- `performer/backends/base.py` — `BackendAdapter` Protocol, `BackendStatus`
- `performer/backends/opencode.py` — `OpenCodeAdapter` (opencode acp mode)
- `performer/backends/__init__.py` — factory + unsupported-backend error
- `tests/unit/backends/test_base.py`
- `tests/unit/backends/test_opencode.py`

### Phase 4 — Message Loop (US1, US2, US3, US4, US6)

- `performer/main.py` — dispatch, status, relay_feedback, health handlers; timeout watchdog; metrics collection; stand cleanup in `finally`
- `tests/unit/test_main.py`
- Add `performer_image` field to `src/coordinare/config.py`

### Phase 5 — Dockerfiles & Full Image (US5)

- `agent/performer/Dockerfile` (base image)
- `agent/performer/images/full/Dockerfile` (full image)

### Phase 6 — Integration Test + README (US1 validation, FR-014)

- `tests/integration/test_performance.py` — end-to-end dispatch → pr_opened using bare git repo + mock opencode ACP process
- `agent/performer/README.md`
