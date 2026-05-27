# Implementation Plan: Claude Code Backend via LiteLLM Proxy

**Branch**: `073-claude-code-litellm` | **Date**: 2026-05-24 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/073-claude-code-litellm/spec.md`

## Summary

Route the existing `claude_code` performer backend through an operator-supplied LiteLLM proxy by adding two new performer config values (`LITELLM_PROXY_BASE_URL`, `LITELLM_PROXY_AUTH_TOKEN`) to the existing `Settings` (pydantic-settings) class. When both are set and the active backend is `claude_code`, the performer (a) starts an in-container response-translation shim (`claude_code_shim.py`) bound to `127.0.0.1:<ephemeral>` that forwards to the operator's upstream LiteLLM proxy and normalizes responses (drops `thinking` content blocks the claude CLI 2.1.148 parser rejects); (b) threads the shim's loopback URL into the subprocess env as `ANTHROPIC_BASE_URL`, plus the operator bearer as `ANTHROPIC_AUTH_TOKEN`, via the existing `env={**os.environ, ...}` merge in `agent/performer/src/performer/backends/claude_code.py`. The shim's lifecycle is bound to backend `start()`/`stop()` and fails closed — no silent fallback to direct-Anthropic. When unset, no related env var is injected and the shim does not start — preserving byte-identical default behavior. The auth token + request/response bodies stay out of all logs (FR-004/FR-011) using the same conventions as `ANTHROPIC_API_KEY`. Operator docs (LiteLLM proxy quickstart + Validated Model Matrix) ship alongside the code change.

## Technical Context

**Language/Version**: Python 3.11 (performer + coordinare; matches existing codebase)
**Primary Dependencies**: `pydantic-settings` (existing — extends `Settings` in `agent/performer/src/performer/config.py`); no new runtime dependencies
**Storage**: N/A — config is process-env only; no persistent state added
**Testing**: pytest (existing) — unit tests for config loading + env-injection logic; integration test asserting subprocess env dict diff vs. baseline
**Target Platform**: Linux container (performer image), same as today
**Project Type**: single (existing monorepo with `agent/performer/` subpackage)
**Performance Goals**: Zero added latency on the hot path — only a dict merge at subprocess spawn time (existing operation)
**Constraints**: Secret hygiene: auth token MUST NOT appear in logs, CI artifacts, or persisted card state (FR-004, SC-004). No-op when backend ≠ `claude_code` (FR-005). Byte-identical env baseline when unset (SC-002).
**Scale/Scope**: One performer process per host (existing); per-process config; no fan-out implications

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First**: PASS — change extends an existing `BaseSettings` class with two typed fields, a small conditional env-merge, and one new module (`backends/claude_code_shim.py`) implementing a single asyncio reverse-proxy task; no new abstractions beyond the shim itself.
- **II. Testing Discipline (NON-NEGOTIABLE)**: PASS — plan calls for unit tests (config field defaults + parsing; shim `thinking`-block stripping for JSON and SSE; shim secret hygiene), integration test (subprocess env dict baseline diff), a contract test (env var names the claude CLI honors), and a 5-turn smoke gate for SC-006. No coverage regression.
- **III. UX Consistency**: PASS — config surface matches existing pattern (env-overridable `BaseSettings` field); operator-facing docs follow existing docs structure under `docs/`; shim is invisible to operators (loopback only).
- **IV. Performance by Design**: PASS — the shim adds one loopback TCP round-trip per claude CLI request. Budget: shim adds ≤5 ms p99 added latency vs. direct upstream (loopback + content-block filtering is well under this). SC-002 (byte-identical baseline env when unset) remains the no-op-path budget.
- **V. Clarity Before Action**: PASS — spec has zero NEEDS CLARIFICATION markers; all assumptions documented; out-of-scope items listed; shim's fail-closed lifecycle (FR-010) and secret-hygiene constraints (FR-011) are explicit.

**Result**: No violations. No entries in Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/073-claude-code-litellm/
├── plan.md              # This file
├── research.md          # Phase 0: env-var contract resolution, secret handling
├── data-model.md        # Phase 1: config entities + validated model matrix doc shape
├── quickstart.md        # Phase 1: operator LiteLLM bring-up
├── contracts/
│   └── env-vars.md      # Phase 1: env-var contract between performer and claude CLI
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
agent/performer/
├── src/performer/
│   ├── config.py                       # +2 fields: LITELLM_PROXY_BASE_URL, LITELLM_PROXY_AUTH_TOKEN
│   └── backends/
│       ├── claude_code.py              # Inject env vars into self._tool_env; wire shim lifecycle
│       └── claude_code_shim.py         # New: asyncio loopback reverse proxy + thinking-block stripper
└── tests/
    ├── unit/
    │   ├── test_config.py              # New: config defaults + env override + secret repr
    │   ├── test_claude_code_env.py     # New: env-injection logic (set, unset, partial, wrong backend)
    │   └── test_claude_code_shim.py    # New: JSON + SSE thinking-block stripping, secret hygiene
    └── integration/
        └── test_claude_code_subprocess.py  # New: subprocess env dict diff baseline + with-proxy

docs/
└── operator/
    └── litellm-proxy.md                # New: quickstart + Validated Model Matrix
```

**Structure Decision**: Single project, existing `agent/performer/` subpackage. The change is localized to two existing files (`config.py`, `backends/claude_code.py`) plus new tests and one new operator-facing doc, and adds one new module (`backends/claude_code_shim.py`) for the in-container response-translation shim.

## Complexity Tracking

> No constitution violations — table intentionally empty.

## Drive-By Additions (not in original scope)

### `serialize_env_bootstrap` config flag (coordinare side)

**Motivation**: When the LiteLLM proxy routes claude_code's CLI traffic to a single-tenant LLM backend (e.g. one ollama server), the coordinare's default behavior of launching env_bootstrap in the background while consumer performers begin dispatching as soon as `activate.sh` is written produces concurrent calls to the same upstream model. On a single-tenant backend this manifests as request queuing, model-swap thrashing, or VRAM contention -- all of which surface to the shim as truncated streams and fail-closed errors (FR-010), looking like 073 bugs but caused by backend sharing.

**Change** (3 files, ~50 LOC including tests):
- `src/coordinare/config.py` -- add `serialize_env_bootstrap: bool = False` on `ProjectConfiguration` (sibling of `max_concurrent_cards`).
- `src/coordinare/graph/nodes/dispatch_performer.py` -- extend the existing env-cache gate (~line 566) with a second condition that also holds non-bootstrap dispatch when `serialize_env_bootstrap=True` AND `EnvCacheState.bootstrap_in_flight=True`. Logs `dispatch_performer.env_bootstrap_in_flight` with a clear `detail` field.
- `tests/unit/graph/nodes/test_dispatch_performer.py` -- two unit tests: flag-on holds dispatch even when `activate.sh` exists; flag-off (default) preserves existing behavior.

**Default is `False`** -- existing operators see no behavior change. Recommended setting for operators running claude_code against a shared single-tenant LiteLLM/ollama backend is:

```yaml
max_concurrent_cards: 1
serialize_env_bootstrap: true
```

This guarantees zero overlap between bootstrap and consumer performers on the upstream LLM, even during the bootstrap container teardown window.

**Why drive-by, not its own spec**: The failure mode was discovered while live-validating 073's quickstart with a local ollama. It's a one-flag config gate on existing state (`bootstrap_in_flight` already exists in `EnvCacheState`), the dispatch-gate site is the same one 073 already touches via the bootstrap-related code paths, and a separate spec would have shipped the same diff. Documented here so the addition is discoverable from this branch's plan.

### Post-review production fixes (commit `35f9d23`)

Three follow-on bugs surfaced while running the 073 stack against a single-tenant LiteLLM/ollama backend with `serialize_env_bootstrap: true`. All three landed on this branch because they are only reachable once a real proxy is in the path and the serialization gate is engaged:

1. **claude CLI subprocess liveness (gate-release fix)** — `JobRunner` was holding `serialize_env_bootstrap` past the CLI's exit because the bootstrap container's terminal-state detection waited on `proc.wait()` while a daemon thread that had already drained stdout never observed the OS-level exit. Fix: explicit `proc.poll()` liveness probe in the runner's wait loop releases the gate as soon as the CLI process is reaped. Without this, consumer performers stay blocked behind a long-completed bootstrap.

2. **`SERVICE_INFERENCE_TIMEOUT` cap on `_run_service_inference()`** — when the upstream LLM stalled (single-tenant ollama swapping models), the inference call could hang the entire performer indefinitely. Fix: wrap `_run_service_inference()` in `asyncio.wait_for(..., timeout=SERVICE_INFERENCE_TIMEOUT)` with surface-as-turn-failure semantics (same shape as FR-007).

3. **Persist `EnvCacheState.readme_sha` across coordinare restarts (schema v3)** — `EnvCacheState` was a live in-memory model; on coordinare restart the SHA was lost and bootstrap re-ran even when the symphony's tracked files were unchanged. Fix: introduce `EnvCacheStateSnapshot` (durable subset: `symphony_name`, `sanitised_name`, `cache_dir`, `readme_sha`, `last_bootstrap_at`, `last_bootstrap_succeeded`, `cache_dir_ready`), persist it on the `WorkflowSnapshot` as a new `env_cache: dict[str, EnvCacheStateSnapshot]` field keyed by symphony name, and overlay into the live `EnvCacheState` registry during daemon restore. Bumps `CURRENT_SCHEMA_VERSION` to **3**; `MIN_SUPPORTED_SCHEMA_VERSION` stays at 1 (v1/v2 snapshots load with empty `env_cache` and re-bootstrap on next run — graceful upgrade, no migration tooling needed). JSON schema contract at `specs/003-state-persistence/contracts/workflow-snapshot.schema.json` updated to declare the new field.

### Test diagnostics fix (commit `e7fda84`)

`agent/performer/tests/conftest.py:wait_for_status` was opaque on timeout (curl stderr empty, no container state visible). Rewrote with: (a) early-exit detection via `docker inspect -f {{.State.Running}}` each poll cycle — fails fast when the container has already exited rather than waiting the full timeout; (b) `docker logs --tail 100` dumped into every failure message (early-exit OR timeout); (c) `curl -sS -f` (was `-s -f`) so curl reports errors. All four call sites in `test_dockerfile_{base,slim,full}.py` updated to pass `container_id=`. No test-logic change — flakes are now diagnosable rather than silent.

### Sandbox directory allowlist fix (env_bootstrap unblock)

**Symptom**: With the 073 stack live against a single-tenant LiteLLM/ollama backend, the `env_bootstrap` role on the `website` symphony looped indefinitely on `mkdir`-blocked errors from the claude CLI — every tool call targeting `/devenv/website-3ab3e0/...` came back as a sandbox refusal, and qwen3.6:35b (a known limitation: contract-bound coding-agent roles) retried the same forbidden path instead of pivoting.

**Root cause**: Claude Code enforces two independent sandbox layers. `--dangerously-skip-permissions` / `--permission-mode bypassPermissions` only gate the tool-use *approval prompts* (which don't fire in `--print` mode anyway). The *directory-write allowlist* is separate and pinned to the CLI's `cwd` — for the performer that is `stand.path` = `/tmp/performer-<id>`. Bootstrap targets `/devenv/<symphony>-<hash>` (per spec 060's reusable env cache), which lives outside `cwd` and is therefore mkdir-blocked even with permissions skipped.

**Fix** (in `agent/performer/src/performer/backends/claude_code.py`): thread `score.env_cache_path` from `ClaudeCodeBackend.start()` into a new `self._extra_dirs` list, which `_launch()` translates into one `--add-dir <parent>` and one `--add-dir <env_cache_path>` argument on the CLI invocation. Non-bootstrap roles see `env_cache_path = ""` and `_extra_dirs` stays empty — zero behavior change for the default path. `--add-dir` is the official escape hatch for this case (not a workaround); it widens the directory-write allowlist without touching the tool-approval gate.

**Verification**: after rebuild, captured LiteLLM SSE responses show 13+ healthy LLM round-trips with `tool_result_is_error: false`, model successfully listing `/devenv/website-3ab3e0/{rbenv/versions,node_modules,project}/`, reading `Gemfile.lock`, etc. No more loop-on-blocked pattern.

### Reader-loop chunked-read fix

**Symptom**: A claude-ephemeral implementor died mid-job with `asyncio.LimitOverrunError: Separator is found, but chunk is longer than limit` raised from `_event_reader_loop`. The CLI had emitted a single stream-json line larger than `asyncio.StreamReader`'s default 64 KiB internal buffer (a large `tool_use` payload), and `readline()` refuses to return when no newline appears within that window.

**Root cause**: `_event_reader_loop` was calling `await stdout.readline()`, which is hard-capped by the StreamReader's limit. The cap is per-line, not per-event — any single oversized event terminates the reader regardless of how much stdout has actually been drained.

**Fix** (in `agent/performer/src/performer/backends/claude_code.py:_event_reader_loop`): switched to fixed-size chunked reads (`await stdout.read(65536)`) accumulating into a local `bytearray` and splitting on `\n` ourselves. No per-line cap; arbitrarily large events stream through intact. Idle-timeout (`CLAUDE_CODE_IDLE_TIMEOUT`) is preserved by wrapping the chunk read in `asyncio.wait_for`. `_stdout_capture` mirror now writes chunk-level (previously line-level) — same bytes, fewer write calls. EOF path flushes any trailing partial line before exit.

**Verification**: rebuilt `performer:full`, ran a real implementor job (`2c8b1d8b`, "Add robots.txt and llms.txt") on the claude-ephemeral performer end-to-end — terminal_state=succeeded after ~7m, zero LimitOverrunError, zero idle-timeout, zero reader-loop warnings. A follow-on claude-ephemeral job (`cb3a2b1e`) also succeeded with no reader issues. Unit tests in `agent/performer/tests/unit/backends/test_claude_code.py` updated to mock `stdout.read` instead of `stdout.readline` (77/77 pass).
