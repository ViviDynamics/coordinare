# Implementation Plan: Symphony Test-Environment Injection

**Branch**: `092-symphony-test-env` | **Date**: 2026-06-16 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/092-symphony-test-env/spec.md`
**Authoritative design**: [docs/superpowers/specs/2026-06-16-symphony-test-env-injection-design.md](../../docs/superpowers/specs/2026-06-16-symphony-test-env-injection-design.md)

## Summary

Spec 091 taught coordinare to install and host stateful services (postgres, redis) from
an inferred services manifest. A postgres entry declares an `init.password_env_var`
(a name, never a literal secret); the generated `services-start.sh` pre-checks the named
var is set, then runs `initdb --pwfile=<(printf '%s' "$VAR")`. The service-inference
`validation_phase=start` dry-run actually renders and executes that script. The dry-run
subprocess inherits only the validator's `os.environ`, where `POSTGRESQL_PASSWORD` is
absent — so the gate trips `exit 75`, the whole manifest is rejected, and the env-cache
keeps a stale `services-start.sh` that never boots postgres. The `website` symphony's QA
then fails with "Connection refused".

This feature introduces a first-class, symphony-level **test environment**: an optional
`test_env` config block on `SymphonyConfig` naming a dotenv-style file (exactly one of
`repo_path` resolved inside the cloned repo, or absolute `host_path`), and a pure
coordinare-owned loader `test_env_loader.load_test_env(cfg, *, repo_root)` that parses it
dotenv-style (no shell exec) into a `dict[str, str]`. Coordinare injects those vars into
every context that runs project code: the inference start-phase dry-run (via the existing
`validate(..., env=)` seam), the env-cache QA runtime, and other code-running performers
(via the existing payload `secrets` seam) — always injecting coordinare-owned operational
secrets *after* test-env vars so a test file can never clobber a credential. When no block
is configured, the inference agent emits a path-only `test_env_source` on
`ServicesManifest`; coordinare parses that path through the same loader and persists it
with the cache. The spec-091 secret invariant holds end to end: the manifest and logs
carry only env-var **names** and file **paths**, never literal values.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod on 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config + manifest models + validators); the
existing `coordinare_service_inference` package (validator/schema/prompt); `structlog`
(redacted observability); the existing `secrets` payload seam in
`http_performer_service.py`. No new external dependencies.
**Storage**: env-cache JSON snapshot via `state_store.py` (existing single-host
single-process). Extended only to persist the discovered fallback `test_env_source` path
alongside the cache. No new store. Test-env *values* are never persisted — runtime env
only, never baked into the cache image.
**Testing**: pytest (`.venv/bin/pytest`); lint `.venv/bin/ruff check`.
**Target Platform**: Linux performer containers (Debian-based) + the coordinare host
process (darwin/linux).
**Project Type**: single project (coordinare) + the in-repo `packages/service_inference`
package.
**Performance Goals**: Loader parses a small dotenv file (≤ a few KB, tens of keys) in
well under 10 ms; negligible against container/bootstrap latency. No hot path.
**Constraints**: Spec-091 secret invariant is non-negotiable — manifest and logs carry
only var **names** and file **paths**, never literal values; loaded values route through
the redacted `secrets` channel. `repo_path` must not escape the clone (containment
check). No shell execution / command substitution / interpolation in the parser.
**Scale/Scope**: Per-symphony single test-env file. One new sub-model, one new pure
loader service, one new optional manifest field, three injection call-sites, one
persisted path field.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Constitution v1.1.0 — five principles:

- **I. Code Quality First** — PASS. One small pydantic sub-model (mirrors
  `CloserPrChecksConfig`), one pure single-purpose function (`load_test_env`), one
  optional manifest field. No dead code; reuses the existing `validate(env=)` and
  `secrets` seams rather than adding parallel plumbing. Type-annotated throughout
  (`dict[str, str]`, `TestEnvConfig | None`).
- **II. Testing Discipline (NON-NEGOTIABLE)** — PASS. Tests precede/accompany code:
  loader unit tests (dotenv edge cases, containment rejection, host-vs-repo resolution,
  missing-file error, literal-`$(...)` no-exec), config unit tests (both-set / neither-set
  / each-alone, round-trip), manifest unit tests (`test_env_source` optional/path-only),
  and integration tests (dry-run passes with injected var; absent var still trips
  `exit 75`; redaction asserts no value in captured logs). Coverage MUST NOT decrease.
  Tests are deterministic and behavior-named.
- **III. UX Consistency** — PASS. Config surface mirrors existing symphony sub-blocks;
  error messages are coordinare-side, name the resolved path and the field that pointed at
  it (no buried `exit 75`). `config.example.yaml` documents the block.
- **IV. Performance by Design** — PASS. Success criteria are measurable (manifest accepted,
  services boot, QA reaches DB, no value in logs); the loader is off any hot path.
- **V. Clarity Before Action** — PASS. Zero `[NEEDS CLARIFICATION]` markers; the approved
  design doc resolved all open questions during brainstorming.

No violations → Complexity Tracking left empty.

## Project Structure

### Documentation (this feature)

```text
specs/092-symphony-test-env/
├── plan.md              # This file (/speckit.plan command output)
├── spec.md              # Feature specification (/speckit.specify output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   ├── test_env_config.md       # TestEnvConfig + SymphonyConfig.test_env schema
│   ├── load_test_env.md         # loader function contract
│   └── manifest_test_env_source.md  # ServicesManifest.test_env_source field
├── checklists/
│   └── requirements.md  # Spec quality checklist (already passing)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                         # ADD: TestEnvConfig sub-model + SymphonyConfig.test_env
├── config_validation.py              # cross-field wiring if not fully on the model
└── services/
    ├── test_env_loader.py            # NEW: load_test_env(cfg, *, repo_root) -> dict[str, str]
    └── http_performer_service.py     # inject test-env into _build_regular_payload + the
                                      #   env-bootstrap payload builder; thread env= into
                                      #   the inference validate(...) call

packages/service_inference/src/coordinare_service_inference/
├── schema.py                         # ADD: ServicesManifest.test_env_source (path only)
├── prompt.py                         # instruct agent to emit test_env_source when no
                                      #   config-provided test_env is in play
└── validator.py                      # UNCHANGED — already accepts env=

# env-cache state persistence (state_store.py / the env-cache state model)
#   — persist the discovered fallback test_env_source path with the cache

config.example.yaml                   # documented test_env example

tests/unit/
├── services/
│   ├── test_test_env_loader.py       # NEW: loader edge cases + containment + no-exec
│   ├── test_service_inference_schema.py   # EXTEND: test_env_source pins
│   └── ...
└── (config tests)                    # EXTEND: TestEnvConfig + SymphonyConfig round-trip
tests/integration/                    # dry-run injection + exit-75 + redaction
```

**Structure Decision**: Single-project coordinare layout plus the in-repo
`packages/service_inference` package — both already established. New code lands in the
existing `src/coordinare/services/` directory (loader) and `src/coordinare/config.py`
(sub-model), reusing the existing `validate(env=)` and payload `secrets` seams rather than
introducing new modules or transport layers. The manifest field lands in the existing
`schema.py`. No new top-level directories.

## Complexity Tracking

> No Constitution Check violations — section intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| (none)    | —          | —                                   |
