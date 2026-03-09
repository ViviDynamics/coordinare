# Research: 012-performer

**Branch**: `012-performer` | **Date**: 2026-03-03

---

## Decision 1: Implementation Language

**Decision**: Python 3.12+

**Rationale**: The performer must be self-contained within the coordinare repo and independently deployable. Python is already the project language; all required libraries (pydantic, httpx, psutil, asyncio, structlog) are already justified or in use in coordinare. Staying monolingual eliminates an ecosystem boundary inside a single repo.

**Alternatives considered**:
- Shell script entrypoint — adequate for the protocol loop but too fragile for async subprocess management, timeout enforcement, metrics collection, and pydantic-validated JSON parsing.
- Node.js / Go — cross-language in a mono-repo adds a toolchain burden with no material benefit at this scale.

---

## Decision 2: opencode Invocation Mode

**Decision**: `opencode acp` mode (Agent Communication Protocol — nd-JSON over stdin/stdout)

**Rationale**: `opencode acp` runs opencode as a persistent, headless process that accepts nd-JSON messages on stdin and emits nd-JSON events on stdout. This is the correct integration mode because:
1. It keeps the opencode process alive for the duration of the performance — relay_feedback can be sent as new nd-JSON messages without re-launching the process or requiring `--continue --session <id>`.
2. It emits structured events (`session.idle`, `session.error`) that the performer can parse to detect completion or failure.
3. It does not start an HTTP server or TUI, so it is safe inside a container with no exposed ports.

**Alternatives considered**:
- `opencode run --format json "prompt"` — simpler; emits nd-JSON events; but does not natively support relay_feedback mid-session (would require re-launching with `--continue`). Rejected because it couples relay_feedback delivery to process lifecycle management.
- `opencode run` (default/TUI mode) — interactive; not safe for non-interactive containers. Rejected.

**opencode ACP event shapes (key events)**:
```json
{"type": "session.idle"}         // performance complete
{"type": "session.error"}        // unrecoverable backend error
{"type": "message.part.updated", "part": {"type": "text", "text": "..."}}
{"type": "tool.execute", "name": "...", "input": {...}}
```

**Token count availability**: opencode does not emit token counts in its documented stable event stream (as of early 2026). The `metrics.tokens_processed` field will be emitted as `null`/omitted. If a future opencode release adds `token_count` events, the OpenCodeAdapter can parse them without changing the performer's protocol layer.

---

## Decision 3: GitHub PR Creation

**Decision**: `httpx.AsyncClient` — POST to GitHub REST API v3

**Endpoint**: `POST https://api.github.com/repos/{owner}/{repo}/pulls`

**Minimal request body**:
```json
{
  "title": "<card title>",
  "head": "<branch name>",
  "base": "<default branch>",
  "body": "<card description + acceptance criteria>"
}
```

**Response fields used**:
- `html_url` → `pr_url` in the performer's status response
- `node_id` → `pr_node_id` in the performer's status response

**Rationale**: `httpx` is already in coordinare's dependency tree. The GitHub REST API is the simplest interface for PR creation. GraphQL is not needed since we only need to create (not query or mutate) a PR.

**PR title/body source**: The performer constructs the PR title from `card.title` and the body from `card.description` + `card.acceptance_criteria`. The opencode backend may produce richer commit messages, but the performer uses card fields as the canonical source since they are always available.

**Base branch detection**: The performer uses the GitHub API to get the repo's default branch (`GET /repos/{owner}/{repo}`) before creating the PR. This avoids hardcoding `main` or `master`.

---

## Decision 4: Process Metrics Library

**Decision**: `psutil>=5.9`

**Rationale**: psutil is the only cross-platform (Linux + macOS) library that can:
- Query RSS memory for an arbitrary PID (not just the current process)
- Return CPU percent for an arbitrary PID
- Enumerate descendant processes recursively

**Alternatives considered**:
- `resource` stdlib module — provides `getrusage()` for the current process only; cannot query a subprocess PID; no CPU percent. Rejected.
- `/proc` filesystem — Linux-only; not available in macOS development environments or non-Linux containers. Rejected.
- No metrics — violates FR-016. Rejected.

**psutil patterns used**:
```python
p = psutil.Process(proc.pid)
rss = p.memory_info().rss
cpu = p.cpu_percent(interval=None)  # call once at start to initialise, discard 0.0 result
child_pids = [c.pid for c in p.children(recursive=True)]
```

---

## Decision 5: Subprocess Lifecycle and Timeout Enforcement

**Decision**: `asyncio.create_subprocess_exec` with `start_new_session=True` + `os.killpg` for process tree termination

**Rationale**: The opencode ACP process may spawn child processes (Node.js workers, etc.). `proc.kill()` only sends SIGKILL to the direct child, leaving grandchildren orphaned. `start_new_session=True` places the subprocess in its own process group, allowing `os.killpg(os.getpgid(pid), signal.SIGKILL)` to terminate the entire tree atomically. psutil is used as a fallback when process group kill is unavailable (e.g., if the process group has already changed).

**Timeout pattern**:
```python
proc = await asyncio.create_subprocess_exec(
    *cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE,
    start_new_session=True,
)
async with asyncio.timeout(agent_timeout_seconds):
    await _stream_events(proc)
```

On `asyncio.TimeoutError`: kill process group, await `proc.wait()`, transition session to `error`.

---

## Decision 6: Protocol Conformance Strategy

**Decision**: Performer defines its own local pydantic models (`PerformerMessage`, `PerformerResponse`) that mirror `coordinare.protocol.ProtocolMessage` / `ProtocolResponse` exactly, plus an optional `metrics` extension field on `PerformerResponse`.

**Rationale**: Installing the full `coordinare` package inside the performer container would pull in LangGraph, FastAPI, and the entire orchestrator dependency tree — making the base image unnecessarily large and coupling the performer's deployability to the coordinare's release cycle. Local models keep the container lean.

**Schema drift prevention**: A contract test (`tests/unit/test_protocol_contract.py`) exports the JSON Schema of both `coordinare.protocol.ProtocolResponse` and `PerformerResponse`, then asserts that all fields present in the coordinare schema are present and type-compatible in the performer schema. This test runs in CI and will fail if the schemas diverge.

**metrics extension**: `PerformerResponse` adds `metrics: PerformerMetrics | None = None`. The coordinare's pydantic model ignores unknown fields by default, so existing coordinare code receives performer responses without breakage. The coordinare can adopt `metrics` in a future update by adding the field to its own `ProtocolResponse`.

---

## Decision 7: Session ID Format

**Decision**: `uuid.uuid4()` (stdlib — no new dependency)

**Rationale**: UUIDs are unique, collision-resistant, and universally understood. The session ID is opaque to the coordinare — it only uses it as a correlation key in status/relay_feedback messages.

---

## Decision 8: Stand (Workspace) Location

**Decision**: `tempfile.mkdtemp(prefix="performer-")` — OS-managed temp directory

**Rationale**: Each performance gets a unique temp directory. The OS temp filesystem is already ephemeral on most container runtimes. The performer calls `shutil.rmtree(stand_path, ignore_errors=True)` in a `finally` block on performance exit (FR-009). This guarantees cleanup regardless of outcome.

**Disk space edge case**: If the clone or code generation exhausts disk space, the OS will raise `OSError`. The performer catches this, cleans up as much as possible, and returns `{"status": "error", "reason": "disk space exhausted"}`.
