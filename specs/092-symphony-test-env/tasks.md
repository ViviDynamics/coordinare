# Tasks: Symphony Test-Environment Injection

**Feature**: 092-symphony-test-env
**Branch**: `092-symphony-test-env`
**Plan**: [plan.md](plan.md) | **Spec**: [spec.md](spec.md)
**Authoritative design**: [docs/superpowers/specs/2026-06-16-symphony-test-env-injection-design.md](../../docs/superpowers/specs/2026-06-16-symphony-test-env-injection-design.md)

Tests are included: Constitution II (Testing Discipline) is NON-NEGOTIABLE and the spec
defines them explicitly. Within each phase, tests precede the implementation they cover.

**Commands**: tests `.venv/bin/pytest`; lint `.venv/bin/ruff check`.

## Phase 1: Setup

- [X] T001 Confirm no new dependencies are required (pydantic 2.x, structlog, the `coordinare_service_inference` package already present) and locate/verify the four injection seams the feature reuses: `validate(..., env=)` in `packages/service_inference/src/coordinare_service_inference/validator.py`, the inference main loop call to `validate(...)`, `_build_job_payload()` and the env-bootstrap payload builder in `src/coordinare/services/http_performer_service.py`, and the env-cache state model in `src/coordinare/state_store.py`. Record the exact symbols/line ranges in a scratch note for the implementing tasks.
  - **Verified seams**: `validate(scripts, *, working_dir=None, env=None, ...)` at `validator.py:45` (merges `{**os.environ, **(env or {})}` at line 150 — seam already accepts `env`); inference main-loop call `validate(scripts, working_dir=None)` at `__init__.py:227` (passes NO env — the thread point for T008); payload builders `_build_job_payload` at `http_performer_service.py:704` (regular; `secrets` dict built ~734) and `_build_env_bootstrap_payload` at `http_performer_service.py:849` (`secrets` ~889); `_inject_claude_code_secrets` helper at line 55. Env-cache state model in `state_store.py` (T017 target). **Naming correction**: design doc / earlier task drafts said `_build_regular_payload` — the real method is `_build_job_payload`. T009 retargeted accordingly.

## Phase 2: Foundational (blocking prerequisites for all user stories)

The config model and the loader are the shared core that both US1 and US2 call. MUST
complete before any user-story phase.

- [X] T002 [P] Write unit tests for `TestEnvConfig` in `tests/unit/test_config_test_env.py`: both-set rejected, neither-set rejected (empty block), `repo_path`-alone accepted, `host_path`-alone accepted, `repo_path` with `..` rejected, absolute `repo_path` rejected, unknown key rejected (`extra="forbid"`).
- [X] T003 Implement `TestEnvConfig` (fields `repo_path`/`host_path`, `extra="forbid"`, `@model_validator(mode="after")` exactly-one + `repo_path` containment) in `src/coordinare/config.py`, mirroring `CloserPrChecksConfig`/`PersonaScopeConfig`.
- [X] T004 Add `test_env: TestEnvConfig | None = None` to `SymphonyConfig` in `src/coordinare/config.py` and wire any cross-field/load-time check in `src/coordinare/config_validation.py` if not fully on the model; add a `SymphonyConfig` round-trip test (with and without the block) to `tests/unit/test_config_test_env.py`.
- [X] T005 [P] Write unit tests for the loader in `tests/unit/services/test_test_env_loader.py`: dotenv parse edge cases (single/double quotes one-layer-only, leading `export `, `#` comments, blank lines, `=` inside value, CRLF), host vs. repo source resolution, containment rejection of `../` and absolute `repo_path` at read time, missing-file error names path + field, no-shell-execution (a `$(...)`/backtick value returned literally), and a **performance assertion (SC-007)**: parsing a ≤50-entry file completes in under 10 ms.
- [X] T006 Implement `load_test_env(cfg: TestEnvConfig, *, repo_root: Path) -> dict[str, str]` in `src/coordinare/services/test_env_loader.py`: source resolution, read-time containment re-check, dotenv parsing per rules, literal values (no shell/substitution/interpolation), and a clear coordinare-side error naming the resolved path + field when a configured file is missing/unreadable. No logging of values.

**Checkpoint**: Config surface validates and the loader parses correctly in isolation.

## Phase 3: User Story 1 — Configured test-env unblocks stateful-service QA (Priority: P1) 🎯 MVP

**Goal**: A configured `test_env` block makes its `KEY=VALUE`s present in all three
code-running contexts so the postgres gate passes and the manifest is accepted.

**Independent Test**: Configure a symphony with `test_env` pointing at a file defining the
postgres password var; run the start-phase dry-run; confirm the gate passes (no `exit 75`)
and the manifest is accepted; confirm the same vars reach the QA runtime and performers.

- [X] T007 [P] [US1] Write an integration test in `tests/integration/test_test_env_injection.py`: a configured `test_env.repo_path` defining `POSTGRESQL_PASSWORD` makes the start-phase dry-run pass the postgres gate and accept the manifest; with the var absent the dry-run still trips `exit 75` with the actionable message; also assert `host_path` source works equivalently.
- [X] T008 [US1] Thread the loaded test-env mapping into the inference start-phase dry-run. **Data-flow correction**: the dry-run executes INSIDE the bootstrap performer (`_perform_job` sets `os.environ` from `payload.secrets` at `agent/performer/src/performer/main.py:3343-3344` BEFORE `_run_service_inference` → `validate(scripts, working_dir=None)` at `coordinare_service_inference/__init__.py:227`, and the validator merges `{**os.environ, **(env or {})}` at `validator.py:150`). Coordinare cannot reach into that in-performer `validate()` call to pass `env=` directly, so the vars are delivered via the bootstrap payload's redacted `secrets` channel (T010) — the performer pushes them into `os.environ`, which the validator picks up. The `validate(..., env=)` seam itself is proven end-to-end by the T007 integration test.
- [X] T009 [US1] Merge the loaded `KEY=VALUE`s into the redacted `secrets` dict in `_build_job_payload()` in `src/coordinare/services/http_performer_service.py` so the env-cache QA runtime (where `services-start.sh` runs) and code-running performers receive them.
- [X] T010 [US1] Merge the loaded `KEY=VALUE`s into the env-bootstrap payload builder `_build_env_bootstrap_payload` in `src/coordinare/services/http_performer_service.py` (the builder distinct from `_build_job_payload`). This is the load-bearing dry-run fix (see T008).
- [X] T011 [US1] Enforce precedence in all merge sites in `src/coordinare/services/http_performer_service.py`: inject test-env vars first, then coordinare-owned operational secrets (`GITHUB_TOKEN`, backend API keys) so a test file can never clobber an operational credential; add a unit test asserting an operational key wins over a same-named test-env key. (Also excludes `test_env_vars` from the unredacted `metadata` at both builders.)
- [X] T012 [P] [US1] Add a documented `test_env` example (both `repo_path` and commented `host_path` forms) to `config.example.yaml`.

**Checkpoint**: US1 is independently shippable — the website postgres "Connection refused"
repro is fixed via a configured block.

## Phase 4: User Story 2 — Agent-discovered fallback when no block is configured (Priority: P2)

**Goal**: With no `test_env` block, the inference agent records a path-only
`test_env_source`; coordinare parses it through the same loader and persists the path with
the cache for later reloads.

**Independent Test**: Run inference on a repo with a conventional test-env file and no
`test_env` config; confirm the manifest carries a path-only `test_env_source`, the dry-run
injects the discovered vars, and the persisted cache records the same path for reload.

**Depends on**: Phase 2 (loader) and Phase 3 (injection call-sites).

- [X] T013 [P] [US2] Extend `tests/unit/services/test_service_inference_schema.py`: `test_env_source` is optional (default `None`), accepts a repo-relative path string, is omitted by default, and surfaces in `manifest_json_schema()`; existing invariants (`extra="forbid"`, `password_env_var` name-only, POSIX patterns, NUL guards) still hold. **Note**: the `extra="forbid"` invariant lives on `ServiceInit`/`ServiceEntry`, not `ServicesManifest` (which uses pydantic's default `extra="ignore"`); the existing 091/063 invariant tests on those classes already cover it and still pass. Added a NUL-guard test on `test_env_source` mirroring `data_dir`.
- [X] T014 [US2] Add `test_env_source: str | None = None` (path only) to `ServicesManifest` and surface it as an optional string property in `manifest_json_schema()` in `packages/service_inference/src/coordinare_service_inference/schema.py`. Added `_test_env_source_no_nulls` field validator (parity with `ServiceEntry.data_dir`).
- [X] T015 [US2] Update `packages/service_inference/src/coordinare_service_inference/prompt.py` to instruct the agent to emit `test_env_source` (path only, never values) only when no config-provided test-env file is in play and a recognizable test-env file exists. **Framing note**: the agent cannot see coordinare config, so the prompt instructs it to emit the discovered path whenever a recognizable test-env file exists; coordinare enforces config-over-discovered precedence (T018). Path-only / never-values is emphasized.
- [X] T016 [P] [US2] Write tests for env-cache persistence of the discovered path in `tests/unit/test_env_cache_test_env_source.py`: the discovered `test_env_source` path is persisted in the env-cache state, round-trips through `state_store.py`, and a configured `test_env` block takes precedence over a persisted discovered path. **FR-017 assertion**: the persisted env-cache state stores only the path (`test_env_source`) — never any loaded `KEY=VALUE` — so a reused cache reloads variables from the source file at runtime rather than carrying baked-in values.
- [X] T017 [US2] Persist the discovered `test_env_source` path (path only, never values) on the env-cache state model in `src/coordinare/state_store.py`. **Implementation note**: persisted on `EnvCacheStateSnapshot` in `state_store.py` AND mirrored on the live `EnvCacheState` in `src/coordinare/models/env_cache.py`; `_persist_env_cache` in `daemon.py` copies the path onto the snapshot. Old snapshots load with `None` (no migration).
- [X] T018 [US2] Wire the fallback in `src/coordinare/services/http_performer_service.py`: when no `test_env` block is configured, parse the manifest/persisted `test_env_source` through the same `load_test_env` (as a repo-relative source) for the dry-run, and reload the same path for the QA-runtime and performer payload contexts. Config block always wins over discovered path. **Impl note**: the consumer/QA-runtime + performer producer lives in `dispatch_performer.py` (where `card_context` is assembled), calling the shared `env_cache.resolve_test_env_vars`; `http_performer_service.py` is the consumer that routes `card_context["test_env_vars"]` → redacted `secrets`. Bootstrap dispatch self-loads in `env_cache._do_dispatch` via the BootstrapJobPayload secrets seam.
- [X] T019 [P] [US2] Add an integration test in `tests/integration/test_test_env_fallback.py`: no config + discoverable file → manifest `test_env_source` is path-only, dry-run injects discovered vars, persisted path reloads for later contexts; with no source providing the needed var, `exit 75` still fires.

**Checkpoint**: Zero-config symphonies get deterministic discovery; config still preferred.

## Phase 5: User Story 3 — Secret invariant and error attribution preserved (Priority: P2)

**Goal**: Manifests/logs carry only names + paths (never values); a missing configured file
yields a clear coordinare-side error naming path + field.

**Independent Test**: Point `test_env` at a non-existent file → distinct error naming path
and field; inspect captured logs of a successful run → only keys and source, never values.

**Depends on**: Phase 2 (loader error path) and Phase 3 (injection call-sites that log).

- [X] T020 [P] [US3] Write a redaction test in `tests/integration/test_test_env_redaction.py`: across a full configured run, no literal test-env value appears in any captured log line, manifest, or persisted state; structured log events emit only keys (var names) and source (`repo_path`/`host_path`/discovered). **FR-017 assertion**: no loaded test-env value is baked into the persisted env-cache state or cache image — values exist only in the live runtime environment, so a reused cache stays correct.
- [X] T021 [US3] At every injection call-site in `src/coordinare/services/http_performer_service.py`, emit structured `structlog` events that log only the loaded **keys** and the **source** label — never values; route loaded values exclusively through the redacted `secrets` channel.
- [X] T022 [P] [US3] Write a missing-file error-attribution test in `tests/integration/test_test_env_missing_file.py`: a configured `test_env` block whose file is absent raises the distinct coordinare-side error naming the resolved path and the originating field — not a silent empty dict and not a buried `exit 75`.
- [X] T023 [P] [US3] Add a manifest-never-values assertion to `tests/unit/services/test_service_inference_schema.py` (or the fallback test): a persisted manifest contains only env-var names and file paths, never a literal value.

**Checkpoint**: Secret invariant and error attribution verified end to end.

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T024 Run the full suite and lint: `.venv/bin/pytest` (confirm coverage does NOT decrease) and `.venv/bin/ruff check src/coordinare/services/test_env_loader.py src/coordinare/config.py src/coordinare/services/http_performer_service.py packages/service_inference/src/coordinare_service_inference/schema.py packages/service_inference/src/coordinare_service_inference/prompt.py`.
- [X] T025 [P] Execute the [quickstart.md](quickstart.md) website-postgres verification end to end (configured `test_env.repo_path` with `POSTGRESQL_PASSWORD`): manifest accepted, postgres boots, QA reaches the DB, grep logs/manifest for the literal value → zero hits. *(Validated live on the website symphony: test-env injection fires keys-only across all three contexts, DB-backed Unit tests pass in CI, coordinare log shows 0 `global_config` crashes and 0 "Connection refused", and grep of logs/manifest for the literal secret value returns zero hits.)*
- [X] T026 [P] Confirm CLAUDE.md "Active Technologies" entry for 092 is accurate (added by the agent-context script) and the design doc cross-link in plan.md/spec.md resolves.

## Dependencies & Execution Order

- **Phase 1 (Setup)** → **Phase 2 (Foundational)** → user-story phases.
- **Phase 2** blocks everything: T003 before T004 (same file ordering + field depends on model); T002 before T003; T005 before T006; T006 depends on T003 (`TestEnvConfig`).
- **Phase 3 (US1, P1)** depends on Phase 2. T007 (test) before T008–T011. T008→T009→T010→T011 are sequential (all edit `http_performer_service.py`). T012 is independent.
- **Phase 4 (US2, P2)** depends on Phase 2 + Phase 3 injection sites. T013 before T014; T016 before T017; T018 depends on T006 + T014 + T017.
- **Phase 5 (US3, P2)** depends on Phase 2 (error path) + Phase 3 (logging call-sites). Largely verification + redaction discipline; can run alongside Phase 4.
- **Phase 6** last.

**Story independence**: US1 is independently shippable (the MVP fix). US2 and US3 build on
US1's loader + injection sites, as the spec states.

## Parallel Execution Examples

- **Foundational tests** in parallel: T002 and T005 (different files).
- **Within US1**: T007 (integration test, new file) and T012 (`config.example.yaml`) in parallel; the `http_performer_service.py` edits (T008–T011) are strictly sequential.
- **Within US2**: T013 and T016 (different test files) in parallel; T019 once T014/T017/T018 land.
- **Within US3**: T020, T022, T023 (separate test files) in parallel; T021 is the single source edit.

## Implementation Strategy

- **MVP = Phase 1 + Phase 2 + Phase 3 (US1)**: delivers the website postgres fix via a
  configured `test_env` block. Stop-and-ship point.
- **Increment 2 = Phase 4 (US2)**: zero-config agent discovery + persistence.
- **Increment 3 = Phase 5 (US3)**: redaction + error-attribution hardening (much is
  already enforced by the loader/schema; this phase verifies and tightens call-site logging).
- **Phase 6**: full-suite gate, quickstart verification, docs.

## Task Summary

- **Total tasks**: 26
- **Setup**: 1 (T001)
- **Foundational**: 5 (T002–T006)
- **US1 (P1, MVP)**: 6 (T007–T012)
- **US2 (P2)**: 7 (T013–T019)
- **US3 (P2)**: 4 (T020–T023)
- **Polish**: 3 (T024–T026)
- **Parallelizable [P]**: T002, T005, T007, T012, T013, T016, T019, T020, T022, T023, T025, T026
