# Phase 0 Research — Containerized Performer Execution

All `NEEDS CLARIFICATION` items in the plan's Technical Context were resolved during the spec's clarification session (see `spec.md` → `## Clarifications`). This document records the design decisions and the alternatives considered for each technology choice.

## 1. Performer HTTP server framework

- **Decision**: FastAPI + Starlette running under `uvicorn` inside the container.
- **Rationale**: Already used in coordinare (dashboard, health server, force-poll endpoint per spec 016). Native `async` support fits the single-slot job runner. Built-in OpenAPI generation gives us the contract for free and aligns with the contract-test gate. SSE streaming (FR-008) is a 10-line `StreamingResponse`.
- **Alternatives considered**:
  - `aiohttp` — fewer in-house users, no automatic OpenAPI.
  - Bare `asyncio.start_server` over a custom JSON framing — too low-level; reinvents middleware/auth.
  - gRPC — adds protobuf toolchain to every image, hurts the slim/base story.

## 2. Coordinare → performer client

- **Decision**: `httpx.AsyncClient` for both polling and SSE consumption (httpx supports `stream("GET", url)` for SSE).
- **Rationale**: Already a project dependency (used by GitHub clients, CDN upload, notification webhooks). Async-native, supports per-request timeouts and connection pooling.
- **Alternatives considered**:
  - `aiohttp` client — would add a second HTTP client to the project.
  - `requests` + thread pool — blocks event loop; rejected.

## 3. Container runtime integration

- **Decision**: Drive Docker via the local `docker` CLI through `asyncio.create_subprocess_exec` (`docker run`, `docker stop`, `docker inspect`). No Python Docker SDK dependency.
- **Rationale**: Coordinare already shells out to `git` the same way (spec 047). Avoids pulling in `docker-py` (heavy, optional C extensions, requires socket permissions even when unused). Keeps the dependency surface flat for operators who don't enable container mode.
- **Alternatives considered**:
  - `docker` Python SDK — extra dependency for a single feature; harder to mock in tests.
  - `podman` first-class support — deferred; CLI-compatible enough that the same code path works when `DOCKER_HOST` points at podman, but explicit support is out of scope per the v1 carve-out.

## 4. Authentication on the performer port

- **Decision**: Optional shared bearer token per performer registration. Performer middleware rejects requests with a missing or mismatched token; the `disabled` state is logged at startup and exposed via `/status` so dashboards can flag it.
- **Rationale**: Resolves clarification Q1. Operationally simple, works identically across modes, fits the existing per-performer config block.
- **Alternatives considered**: No-auth-on-localhost-only (rejected — too easy to misconfigure on a shared host); mTLS (deferred — out of proportion for v1's single-host Docker scope); per-job HMAC tokens (deferred — adds a token-issuance service for marginal benefit over a static bearer).

## 5. Job protocol — polling AND streaming

- **Decision**: Both polling (`GET /jobs/{id}`) and streaming (`GET /jobs/{id}/stream`) are required of every conformant image. Coordinare uses streaming during in-flight monitoring and polling for terminal-state confirmation and recovery after a stream disconnect.
- **Rationale**: Resolves clarification Q2. Streaming gives low-latency progress in dashboards; polling gives a deterministic recovery path after a network blip without losing the final result.
- **Alternatives considered**: Polling-only (slower UX); streaming-only (fragile under intermittent networks).

## 6. Failure-exclusion threshold and notification

- **Decision**: Default 5 consecutive status-poll failures excludes a performer; recovery is automatic on the first successful status check. Both transitions emit a structured event consumed by the existing notification service (spec 006), dashboard, and Prometheus counters (spec 009).
- **Rationale**: Resolves clarification Q3. Five tolerates two transient blips plus a retry without flapping; tying recovery into the existing notification fan-out reuses Slack/email/console integrations already in production.
- **Alternatives considered**: 1 (too aggressive — flaps on a single dropped packet); 3 (acceptable but does not match the user's preference for a more tolerant default); per-performer-only with no default (forces every operator to configure something they don't yet have intuition for).

## 7. Container readiness timeout

- **Decision**: 120s default, configurable per performer registration. Applies to both ephemeral-startup and persistent-reconnect paths.
- **Rationale**: Resolves clarification Q4. Covers cold-start of the heaviest `full` image (Playwright + Chromium + Node + Python on a typical dev host benchmarked at ~80–95s). Slim variants will trip the timeout only in degenerate conditions.
- **Alternatives considered**: 30s/60s (too tight for `full`); no default (violates Clarity-Before-Action principle by leaving an unsafe blank).

## 8. Capability advertisement

- **Decision**: Performer advertises `{ "backends": ["claude_code", ...], "tool_flags": ["git", "node", ...] }` from a fixed enumeration. v1 enumeration: `git`, `node`, `python`, `browser`, `lint`, `format`, `test_runner`, `ripgrep`, `jq`, `shell`. Performers ignore unknown flags (forward-compat).
- **Rationale**: Resolves clarification Q5. The fixed set is small enough for the coordinare to validate role requirements deterministically, large enough to cover what current personas need (write/doc/test/QA-visual), and extensible later without protocol break.
- **Alternatives considered**: backend-only (loses the QA browser-tool check); free-form labels (non-deterministic match); full binary inventory (over-specifies, brittle).

## 9. Image variants — what each ships

- **Decision**:
  - `performer:base` — Debian-slim + Python 3.12 + Node 22 + git + jq + ripgrep + bash. No agent CLIs. Smallest variant; meant for BYO-CLI.
  - `performer:slim-<backend>` — base + the named backend's CLI only. One image per supported backend (4 today: `claude_code`, `codex`, `junie`, `opencode`). Browser binaries omitted unless `BROWSER=true` build arg supplied.
  - `performer:full` — base + every supported backend + Playwright + Chromium + linters/formatters/test runners commonly used across the supported languages.
- **Rationale**: Matches FR-014..FR-016 and SC-005. The slim variant defaults to no browser to keep the image small, with an opt-in build arg for QA personas that need only one backend plus a browser.
- **Alternatives considered**: A single image with feature flags — defeats the size goal. One slim per (backend × browser) combination — combinatorial explosion.

## 10. Project-specific tooling delivery

- **Decision**: Volume mount at run time. The coordinare's performer registration accepts an optional `volumes:` list (host path → container path, ro/rw). Documented in the BYO contract (FR-017, FR-018).
- **Rationale**: Keeps base images stable and operator-controllable. Aligns with the spec's Assumptions section.
- **Alternatives considered**: Layered build (operator extends base image) — supported as a parallel path via BYO; documented but not the primary mechanism.

## 11. Secret precedence and detection

- **Decision**: Resolution order at job-init time: `init_payload[k]` → `env[k]` → `creds_file[k]`. The performer's secret resolver returns the first non-None value and emits a `secret_missing` error referencing the secret name (not the value) when none of the enabled sources supplies it. Each source is independently disable-able per performer.
- **Rationale**: Matches FR-019..FR-023 and SC-007.
- **Alternatives considered**: Last-wins instead of first-wins — surprising; rejected. Single-source-only — rejected by clarification.

## 12. Process model inside the container

- **Decision**: Single PID-1 `uvicorn` process running the FastAPI app. The job runner spawns the existing performer main loop as an async coroutine, not a subprocess, so cancellation is `asyncio.CancelledError` rather than signal-based. Backends that need to shell out (e.g. running `pytest`) keep using `asyncio.create_subprocess_exec` as today.
- **Rationale**: Simplest model that supports cancel-honored semantics (FR-007). Avoids zombie child processes inside the container.
- **Alternatives considered**: Separate worker process — adds IPC complexity for no behavioral gain at single-slot concurrency.

## 13. Coexistence with subprocess performers

- **Decision**: `PerformerPool` only tracks performers whose mode is `ephemeral` or `persistent`. Subprocess performers continue down the existing dispatch path with no pool involvement; their state, history, and observability surfaces remain unchanged (FR-024, FR-026, SC-008).
- **Rationale**: Zero blast radius on the existing happy path.
- **Alternatives considered**: Unifying subprocess and HTTP under one pool — tempting, but would require simulating an HTTP status surface for subprocess performers; rejected as over-engineering for v1.
