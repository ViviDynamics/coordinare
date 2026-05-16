# Feature Specification: LLM-Driven Service Inference for Env-Cache

**Feature Branch**: `063-llm-service-inference`
**Created**: 2026-05-13
**Status**: Draft
**Input**: User description: "Lean on LLMs + tool calling to 'just know' how to set up a services startup script into the env-cache. Avoid sidecars; the generated startup script must run all supportive services in-container."

## Background

Coordinare performers run inside containers that mount a per-symphony env-cache at `/devenv/<symphony>/`. Today the cache holds language toolchains and project dependencies but has no notion of **supportive services** (databases, caches, search indices, queues) that the project's test/lint suites need at runtime.

The visible failure mode: bot reviews that depend on a real database (e.g. RSpec for a Rails app) fail with "PostgreSQL on localhost:5432 was unavailable" because no service was ever started inside the performer container. Operators today have no clean way to declare these services per-symphony without inventing a docker-compose sidecar pattern, which we have explicitly rejected — performers must remain single-container.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Env-Cache Build Infers and Bakes In a Service Startup Script (Priority: P1)

An operator triggers an env-cache build for a Rails symphony whose tests need Postgres and Redis. During bootstrap, after `bundle install` succeeds, an inference pass examines the project (`Gemfile`, `config/database.yml`, `docker-compose.yml` as a hint, `.env.example`) and emits three plain shell scripts plus a JSON manifest into the env-cache. When a performer launches against that cache, `devenv-profile.sh` invokes `services-start.sh`, Postgres and Redis come up as child processes, and the QA performer's `rspec` run finds a live database on `localhost:5432`.

**Why this priority**: This is the core capability. Without it, every other story below is moot.

**Independent Test**: Build an env-cache for a fixture Rails app that needs Postgres. Inspect `/devenv/<sym>/services/services.json` — it must list `postgres` with a port and data-dir. Launch a performer against the cache; `services-health.sh` must exit 0 within 30s, and `psql -h localhost -p 5432 -c 'SELECT 1'` must succeed inside the container.

**Acceptance Scenarios**:

1. **Given** a project whose manifests reference Postgres, **When** env-cache bootstrap runs, **Then** the inference agent produces a `services.json` containing a `postgres` entry with `binary`, `version`, `data_dir`, `port`, and `cache_inputs` pointing at the manifest files it consulted.
2. **Given** a generated `services-start.sh`, **When** a performer container starts and `devenv-profile.sh` runs, **Then** every service in `services.json` is listening on its declared port before the performer's entrypoint hands control to the backend CLI.
3. **Given** a generated `services-stop.sh`, **When** a persistent performer shuts down, **Then** every started service receives SIGTERM and exits within 10s (SIGKILL fallback after that).

---

### User Story 2 — Language-Agnostic Cache Invalidation (Priority: P1)

A Go project doesn't have a `Gemfile.lock`. A Rust project doesn't have `package-lock.json`. A bare shell-script project has neither. The env-cache invalidation key for service-inference outputs MUST be derived from **the files the inference agent actually consulted**, not from a hardcoded list per language.

**Why this priority**: Hardcoding the trigger files re-introduces the language assumption that the LLM-driven design exists to eliminate.

**Independent Test**: Build env-caches for a Go project (uses `go.mod` + `internal/config/database.go`) and a Rust project (uses `Cargo.toml` + `config.toml`). Inspect each `services.json`; the `cache_inputs` field must list the project-appropriate files, not a fixed Ruby/Node set. Modify one of the listed files and trigger a rebuild — the cache must be invalidated. Modify a file *not* listed and trigger a rebuild — the cache must reuse.

**Acceptance Scenarios**:

1. **Given** a Go project, **When** inference runs, **Then** `cache_inputs` includes the Go-relevant paths the agent actually read (`go.mod`, app config files) and excludes Ruby/Node-only conventions.
2. **Given** a successful prior run, **When** a path listed in `cache_inputs` changes, **Then** the next env-cache build re-runs inference.
3. **Given** a successful prior run, **When** a path *not* listed in `cache_inputs` changes, **Then** the cache is reused without re-running inference.
4. **Given** no prior `services.json` exists, **When** inference runs for the first time, **Then** the cache key falls back to hashing the full repo tree (minus `.gitignore`'d paths).

---

### User Story 3 — Validation Loop Catches Bad Generation Before Bake (Priority: P1)

If the inference agent emits a `services.json` whose generated `services-start.sh` cannot actually bring services up — wrong binary path, missing data-dir init, port conflict — the env-cache build MUST fail loudly **before** the cache is sealed. A broken cache must never reach a performer at runtime.

**Why this priority**: Silent failure here means every performer launched against the bad cache wastes a full bootstrap cycle. Detecting at build time keeps the failure scoped to the build that introduced it.

**Independent Test**: Inject a fixture project where the LLM is forced to emit a `services.json` with a deliberately wrong Postgres binary path. The bootstrap MUST fail with a non-zero exit, the cache MUST NOT be sealed, and the operator-facing error MUST include both the failed `services-health.sh` output and the offending `services.json`.

**Acceptance Scenarios**:

1. **Given** a generated `services.json`, **When** the validation loop dry-runs `services-start.sh` in the staging container, **Then** `services-health.sh` must return 0 within 30s or validation fails.
2. **Given** a validation failure, **When** the agent's retry budget (default 3) is not yet exhausted, **Then** the failure log is fed back to the agent and inference re-runs.
3. **Given** retry budget exhausted, **When** validation still fails, **Then** the env-cache build fails with a non-zero exit and surfaces the last `services.json` + last health output to the operator.

---

### User Story 4 — Manual Override Via `.coordinare/score.json` (Priority: P2)

When the LLM gets it wrong repeatedly for an exotic stack, an operator can commit `.coordinare/score.json` to the project repo. If present, the inference pass is skipped and the operator-supplied JSON goes straight into the same templating + validation path. (Named `score.json` to fit the coordinare/symphony naming theme — it's the project's own score for how its services play.)

**Why this priority**: Escape valve. Without it, an operator whose project repeatedly defeats the LLM has no recourse short of patching coordinare.

**Independent Test**: Place a hand-written `.coordinare/score.json` in a fixture project. Build the env-cache. Logs must show inference was skipped; the operator's JSON must appear verbatim at `/devenv/<sym>/services/services.json`; validation must still run; performer must come up with the operator-declared services.

**Acceptance Scenarios**:

1. **Given** `.coordinare/score.json` exists in the repo, **When** bootstrap runs, **Then** the inference agent is not invoked.
2. **Given** an operator-supplied `score.json`, **When** validation runs, **Then** the same dry-run + health-check loop applies as for LLM-generated output. A bad manual override fails the build the same way a bad LLM output does.
3. **Given** `.coordinare/score.json` exists but is malformed JSON or violates the schema, **When** bootstrap runs, **Then** the build fails with a parser/schema error referencing the file path.

---

### User Story 5 — Externally Required Services Surface Clearly (Priority: P2)

Some services genuinely cannot run in-container — a real Kafka cluster, a managed Snowflake instance, anything requiring multi-node setup or a paid SaaS. The agent MUST mark these as `external_required: true` rather than fabricate a startup line. Bootstrap then fails with a clear, actionable message listing which env vars the operator must supply.

**Why this priority**: Without this, the agent will hallucinate startup commands for things it can't actually host, and validation will catch it as a generic failure rather than a structured "this can't be hosted, you need to provide it."

**Independent Test**: Inject a project whose config references a managed Snowflake URL. Run inference. `services.json` must contain an entry with `external_required: true` and a populated `required_env_vars` list. Bootstrap must exit non-zero with an operator-facing message that lists those env vars.

**Acceptance Scenarios**:

1. **Given** a project depending on a service the agent classifies as externally-required, **When** inference runs, **Then** the entry has `external_required: true` and lists `required_env_vars`.
2. **Given** an entry with `external_required: true`, **When** bootstrap continues, **Then** it fails with a structured error naming the service and the env vars to supply.
3. **Given** the operator supplies the required env vars in the performer config, **When** bootstrap re-runs, **Then** the externally-required entries are validated (env vars present) but no in-container service is started for them.

---

## Requirements

### Functional Requirements

#### Inference Pass

- **FR-001**: The env-cache bootstrap pipeline MUST gain an inference pass that runs after language-toolchain/dependency installation and before the cache is sealed.
- **FR-002**: The inference pass MUST be implemented as an LLM agent with read-only tool calls only: `read_file`, `list_dir`, `which`, `probe_version`, `grep_repo`, and `web_search`. The agent MUST NOT execute project code or write outside its designated output paths.
- **FR-003**: The agent's structured output MUST conform to a `services.json` JSON schema with fields: `services[]` (each having `name`, `binary`, `version`, `data_dir`, `port`, `why_needed`, `sources`, optional `external_required`, optional `required_env_vars`), and `cache_inputs[]`.
- **FR-004**: The three runtime scripts (`services-start.sh`, `services-stop.sh`, `services-health.sh`) MUST be generated **deterministically** from `services.json` by a templating step. The LLM MUST NOT emit shell directly.

#### Runtime Integration

- **FR-005**: `agent/performer/devenv-profile.sh` MUST source/exec `/devenv/<sym>/services/services-start.sh` after env activation, when present and executable. Absence MUST be a no-op (preserves backward compatibility with caches built before this feature).
- **FR-006**: Persistent performers MUST register a shutdown hook that runs `services-stop.sh` on container stop. Ephemeral performers MAY rely on container teardown.
- **FR-007**: `services-start.sh` MUST be idempotent — running it twice in the same container must not double-start daemons. Generated PIDs MUST be tracked in `$XDG_RUNTIME_DIR` (fallback `/tmp/coordinare-services`).

#### Cache Invalidation

- **FR-008**: The env-cache key contribution for service-inference MUST be the SHA-256 of the concatenated content of every path listed in the previous successful run's `cache_inputs`. The hardcoded language-specific file list MUST NOT exist.
- **FR-009**: On the first run (no prior `services.json`), the inference cache key MUST fall back to hashing the full repo tree minus `.gitignore`'d paths.
- **FR-010**: If a runtime `services-health.sh` invocation fails on an otherwise-valid cache, the next bootstrap MUST treat this as a forced regeneration trigger (the agent missed a file) and rebuild using the full-tree fallback key.

#### Validation

- **FR-011**: After templating, the bootstrap pipeline MUST dry-run `services-start.sh` → `services-health.sh` → `services-stop.sh` inside the staging container. All three MUST succeed for the cache to be sealed.
- **FR-012**: The agent's retry budget on validation failure MUST default to 3 and MUST be configurable via `performers.env_cache.inference.retry_budget`. On exhaustion, the build fails non-zero.
- **FR-013**: Failure output to the operator MUST include the final `services.json`, the failing script's stderr, and the validation phase that failed (start / health / stop).

#### Manual Override

- **FR-014**: If `.coordinare/score.json` is present at the repo root, the inference agent MUST be skipped and the file MUST be templated + validated directly. Schema violations MUST fail the build with a clear path reference.
- **FR-015**: A manual override MUST be allowed to declare its own `cache_inputs`. Absence of `cache_inputs` in a manual override MUST cause the bootstrap to hash `.coordinare/score.json` alone for invalidation.

#### External Requirements

- **FR-016**: Services the agent classifies as un-hostable MUST be emitted with `external_required: true` and populated `required_env_vars`. The templater MUST NOT generate start/stop commands for these.
- **FR-017**: At bootstrap completion, the build MUST verify that every `required_env_vars` entry is present in the performer's resolved environment (from `PerformerEndpointConfig.env` or operator-supplied secrets). Missing vars MUST fail the build with a structured error.

### Non-Functional Requirements

- **NFR-001**: The inference pass MUST be bounded in cost: max 50 tool calls, max 100k tokens combined across retries. Excess MUST abort the pass with a structured error.
- **NFR-002**: Inference output MUST be reproducible-on-input: given the same project files and the same agent version, the agent SHOULD converge on the same `services.json` (modulo LLM nondeterminism, mitigated by `temperature=0`).
- **NFR-003**: Generated scripts MUST be human-readable and reviewable in PRs — no obfuscation, no inline base64, comments explaining each service block.

## Out of Scope

- **Multi-container orchestration** (docker-compose, sidecars). Explicitly rejected; everything runs in the performer container.
- **Cluster services** (real Kafka, multi-node Postgres). These go through the `external_required` path.
- **Incremental cache updates.** Cache rebuilds are the unit of change; no partial regeneration.
- **Cross-symphony service sharing.** Each symphony's env-cache owns its own service lifecycle.

## Success Criteria

- A Rails fixture with Postgres + Redis dependencies builds an env-cache where RSpec runs to completion inside the performer container, with zero operator-supplied service configuration.
- A Go fixture with Postgres builds an env-cache that reuses cache when unrelated `.go` files change, and invalidates when `go.mod` or the inference-identified config file changes.
- A fixture targeting Snowflake fails the build with a message naming `SNOWFLAKE_URL` (or similar) as the missing env var, not with a generic "service failed to start."
- Existing symphonies without service dependencies see **zero behavior change**: no `services-start.sh` is generated, performer startup byte-for-byte matches today.
