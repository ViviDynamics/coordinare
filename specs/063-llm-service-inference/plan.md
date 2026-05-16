# Implementation Plan: LLM-Driven Service Inference (063)

## Architecture Overview

```
┌─────────────────────────── env-cache bootstrap ──────────────────────────┐
│                                                                          │
│  fetch repo → install toolchains → install project deps                  │
│                                                                          │
│  ┌─── NEW: service-inference pass ───────────────────────────────┐       │
│  │                                                               │       │
│  │  1. Check for .coordinare/score.json → if present, skip to (4) │       │
│  │  2. Compute prior cache_inputs hash → if unchanged, skip      │       │
│  │  3. Run LLM agent with read-only tools → emit services.json   │       │
│  │  4. Validate schema                                           │       │
│  │  5. Template services-{start,stop,health}.sh from JSON        │       │
│  │  6. Dry-run: start → health → stop in staging container       │       │
│  │  7. On failure: retry up to N, then fail build                │       │
│  │  8. On success: drop artifacts at /devenv/<sym>/services/     │       │
│  │                                                               │       │
│  └───────────────────────────────────────────────────────────────┘       │
│                                                                          │
│  seal cache                                                              │
└──────────────────────────────────────────────────────────────────────────┘

┌─────────────────── performer startup (runtime) ──────────────────────────┐
│  devenv-profile.sh sources env-cache activate.sh                         │
│  if [ -x /devenv/$SYM/services/services-start.sh ]                       │
│    /devenv/$SYM/services/services-start.sh                               │
│  exec backend CLI                                                        │
└──────────────────────────────────────────────────────────────────────────┘
```

## Key Files & Modules

### New files

- `src/coordinare/services/service_inference/__init__.py` — pass entrypoint, orchestrates steps 1–8.
- `src/coordinare/services/service_inference/agent.py` — LLM agent runner; constructs the prompt, exposes the tool-call surface, drives the conversation to convergence on a JSON output.
- `src/coordinare/services/service_inference/tools.py` — read-only tool implementations: `read_file`, `list_dir`, `which`, `probe_version`, `grep_repo`, `web_search`. Each is sandboxed to the staging container's filesystem (no writes, no project code execution).
- `src/coordinare/services/service_inference/schema.py` — pydantic models for `ServicesManifest`, `ServiceEntry`, `ExternalServiceEntry`. JSON schema export for the agent's structured-output binding.
- `src/coordinare/services/service_inference/templater.py` — deterministic `ServicesManifest → (start.sh, stop.sh, health.sh)` rendering. Pure function; no LLM.
- `src/coordinare/services/service_inference/validator.py` — runs the dry-run start/health/stop cycle inside the staging container, returns structured pass/fail.
- `agent/performer/services-templates/` — Jinja2 templates for the three shell scripts.

Filename convention: `.coordinare/score.json` (project root, optional, operator-authored) is the *input override* — when present, the LLM agent is skipped. `services.json` (under `/devenv/<sym>/services/`, always written) is the *output artifact* produced by either the agent or the override pass.

### Modified files

- `src/coordinare/services/env_cache_service.py` — wire the inference pass into the bootstrap pipeline between dependency-install and cache-seal.
- `agent/performer/devenv-profile.sh` — add the `services-start.sh` invocation guarded by existence + executability.
- `src/coordinare/config.py` — add `performers.env_cache.inference` config section: `enabled` (default false during Phase 2; flip to true after one symphony runs cleanly for a week), `web_search_enabled` (default false), `retry_budget` (default 3), `max_tool_calls` (default 50), `max_tokens` (default 100_000).
- `src/coordinare/services/claude.py` (or whichever LLM service hosts structured-output calls) — expose a structured-output helper if not already present.

### Test scaffolding

- `tests/unit/services/test_service_inference_templater.py` — pure-function tests for the templater: given a manifest, expected shell output.
- `tests/unit/services/test_service_inference_validator.py` — mocked subprocess tests for the dry-run cycle.
- `tests/unit/services/test_service_inference_agent.py` — agent tests with a stubbed LLM, asserting tool-call sandboxing and retry behaviour.
- `tests/integration/test_service_inference_e2e.py` — end-to-end with fixture projects (Rails, Go, Rust, Snowflake-stubbed). Marked `@pytest.mark.slow`; runs in CI nightly job, not on every PR.

## Schema (excerpt)

```python
class ServiceEntry(BaseModel):
    name: str                       # "postgres", "redis", "elasticsearch"
    binary: str                     # absolute or PATH-resolvable
    version: str
    data_dir: str                   # under $XDG_RUNTIME_DIR by convention
    port: int
    why_needed: str                 # 1-line human explanation for diagnostics
    sources: list[str]              # paths the agent cited
    external_required: bool = False
    required_env_vars: list[str] = []

class ServicesManifest(BaseModel):
    services: list[ServiceEntry]
    cache_inputs: list[str]         # paths to hash for invalidation
    agent_version: str              # for reproducibility tracking
```

## Cache Key Composition

```
inference_cache_key = sha256(
    agent_version
    + sha256(concat(read(p) for p in prior_cache_inputs))
)
```

First run (no prior manifest):
```
inference_cache_key = sha256(
    agent_version
    + sha256(concat(read(p) for p in repo_tree_minus_gitignore))
)
```

Forced regeneration (runtime health failure):
```
inference_cache_key = sha256(
    agent_version
    + epoch_seconds()                  # forces miss
    + "forced-regen"
)
```

## Tool-Call Sandboxing

All read-only tools must:
- Resolve paths via `realpath` against the staging container's project root.
  Reject any result whose realpath sits outside the project root (catches both
  `..` traversal and symlink escape).
- For `web_search`, gated behind `web_search_enabled`; allow but log all
  queries; results are advisory only and never executed.
- For `probe_version`, only invoke binaries discovered via `which` and only
  with `--version` first, then `-v` on failure, then mark version unknown.
  Never accept arbitrary args.
- For `grep_repo`, cap result count at 100 lines per call.

## Retry & Failure Model

- Agent retries on validation failure receive the failure context as a new system message. The conversation history is preserved across retries so the agent can correct its prior output, but the **structured output target stays the same**.
- After `retry_budget` exhaustion, the bootstrap pipeline writes the final attempt to `/devenv/<sym>/services/services.json.rejected` for operator debugging and exits non-zero.
- A runtime health failure on an already-sealed cache triggers cache invalidation on the next bootstrap attempt; the operator sees a clear log line indicating self-healing kicked in.

## Phased Delivery

**Phase 1 — Schema, templater, validator (no LLM yet)**
Ship the deterministic half. Use `.coordinare/score.json` manual overrides only. Operators who care can hand-write JSON; the runtime path is exercised end-to-end. This de-risks the runtime integration before the LLM lands.

**Phase 2 — LLM agent + tool calls**
Add inference for projects without `score.json`. Behind `performers.env_cache.inference.enabled` flag, default off in this phase, on after one symphony has run it successfully for a week.

**Phase 3 — Cache invalidation via `cache_inputs`**
Wire the per-run input list into the env-cache key composition. Until this lands, inference re-runs on every cache build (acceptable for Phase 2 validation).

**Phase 4 — Forced regeneration on runtime health failure**
The self-healing path. Requires reliable runtime telemetry from `services-health.sh` back to the env-cache service.

## Resolved Decisions

1. **LLM provider/model**: Reuse `ClaudeService` with a separate budget envelope
   (`performers.env_cache.inference.max_tokens`). Inference calls are tagged
   `purpose=service_inference` so they're tracked independently from
   conducting/scoring traffic in cost telemetry. Model: same default as conducting
   (`claude-opus-4-7`) — override via config.
2. **Staging container**: Reuse the bootstrap performer's container. The dry-run
   start/health/stop cycle runs inside the half-built env-cache mount before
   sealing. Spawning a separate container was rejected: it doubles disk I/O and
   diverges from the runtime environment we're trying to validate.
3. **`web_search` gating**: Behind a separate `performers.env_cache.inference.web_search_enabled`
   flag, default false. Even at temperature=0, web results introduce nondeterminism
   that defeats `cache_inputs` invalidation logic.
