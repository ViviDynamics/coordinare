# Feature Specification: Full Makefile Capabilities

**Feature Branch**: `130-makefile`
**Created**: 2026-07-08
**Status**: Draft
**Input**: User description: "add full makefile capabilities into this project"

## User Scenarios & Testing *(mandatory)*

A single, self-documenting top-level `Makefile` becomes the ergonomic entry point for
every common developer, CI, and operator task. It is a **thin façade**: each target
delegates to the existing `bin/` scripts or the `.venv` tools, so the scripts remain the
single source of truth and there is no logic to keep in sync.

### User Story 1 - One discoverable entry point (Priority: P1)

A contributor (human or agent) clones the repo and runs `make` (or `make help`) and
immediately sees a grouped, self-documenting list of every task they can perform — with a
one-line description each — instead of having to discover the scattered `bin/` scripts and
raw `.venv/bin/...` incantations from tribal knowledge. Running any listed target does the
right thing by delegating to the underlying script/tool.

**Why this priority**: Discoverability is the whole point. A single `make help` that maps
the entire task surface is independently valuable even before any parity guarantees; it is
the MVP.

**Independent Test**: Run `make` with no arguments on a fresh checkout → a grouped target
list prints and exits 0. Run each target and confirm it invokes the corresponding existing
capability (e.g. `make lint` runs the same ruff check documented in AGENTS.md; `make build`
runs `bin/build`).

**Acceptance scenarios**:

1. **Given** a fresh checkout, **When** the developer runs `make` (no target), **Then** a
   grouped (dev / test / build / run / release / clean), self-documenting list of targets
   with one-line descriptions prints and the command exits 0.
2. **Given** the venv tools exist, **When** the developer runs `make lint`, **Then** the
   project's ruff check over `src/` and `tests/` runs and its exit code is propagated.
3. **Given** the developer wants to fix lint, **When** they run `make fmt` (a.k.a.
   `make lint-fix`), **Then** ruff auto-fix runs over `src/` and `tests/`.
4. **Given** any documented capability (build, e2e, docker, install, run, start, stop,
   performer-logs, version, validate-version), **When** the developer runs the matching
   `make` target, **Then** it delegates to the existing `bin/` script and propagates its
   exit code.
5. **Given** a checkout with no `.venv`, **When** the developer runs a target that needs it,
   **Then** a clear, actionable error is shown (how to create the venv) rather than a cryptic
   "command not found".

### User Story 2 - CI parity and safety are guaranteed, not remembered (Priority: P2)

The test and run targets encode the project's easy-to-forget conventions so a contributor
cannot get them wrong: the suite always runs with the required environment-variable unset,
`make ci` runs exactly what CI runs, and any target that launches coordinare sources the
project environment (or fails loudly if it is absent).

**Why this priority**: This turns the Makefile from convenient into *safe* — it removes two
recurring footguns (running the suite the wrong way; launching coordinare with unexpanded
config placeholders). Valuable, but only after the entry point (US1) exists.

**Independent Test**: Run `make test-all` and confirm it invokes pytest over the whole
`tests/` tree with the required variables unset. Run `make ci` and confirm it runs the same
steps as the project's full local build. Run a coordinare-launch target with the environment
file absent and confirm it fails with a clear message instead of silently misconfiguring.

**Acceptance scenarios**:

1. **Given** the test targets, **When** the developer runs `make test`, `make test-unit`,
   `make test-contract`, or `make test-all`, **Then** pytest runs with the required
   environment-variable unset applied automatically (the developer never types it), over the
   correct scope (unit, contract, or the whole tree).
2. **Given** CI parity is required, **When** the developer runs `make ci` (a.k.a.
   `make build-all`), **Then** it runs exactly the same steps as the project's full local
   build (`bin/build --all`) with no drift.
3. **Given** a coordinare-launch target (`make run` / `make start`), **When** the project
   environment file is present, **Then** the environment is sourced before launch so config
   placeholders expand; **When** it is absent, **Then** the target fails loudly with a clear
   message rather than launching with empty values.
4. **Given** the contributor docs, **When** a developer reads the "Development" section of
   `AGENTS.md`, **Then** it points at `make help` / `make test` / `make ci` as the canonical
   commands (with the raw fallbacks still documented).

### Edge Cases

- **`make` variant on macOS**: the file targets GNU make (the default `make` on macOS is GNU
  make 3.81); it must not rely on features unavailable there, and should degrade with a clear
  message if run under an incompatible `make`.
- **Target name collides with a real file/directory** (e.g. a future `build/` dir): every
  target is declared `.PHONY` so it always runs.
- **Underlying `bin/` script takes flags** (e.g. `bin/build --e2e`): the Makefile exposes
  dedicated targets rather than trying to forward arbitrary arguments.
- **A target's underlying tool is missing** (`.venv`, `docker`, a `bin/` script): fail with
  an actionable message, not a raw shell error.
- **`make clean` must not delete source or tracked artifacts** — only caches/build detritus
  (`__pycache__`, `.pytest_cache`, `.ruff_cache`, coverage output, `*.pyc`).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The project MUST provide a single top-level `Makefile` that serves as the
  entry point for developer, CI, and operator tasks.
- **FR-002**: Running `make` with no target, and `make help`, MUST print a self-documenting
  list of all targets with a one-line description each, grouped by category (dev / test /
  build / run / release / clean), and exit 0.
- **FR-003**: The Makefile MUST provide targets covering the full existing task surface:
  `lint`, `fmt`/`lint-fix`, `test`, `test-unit`, `test-contract`, `test-all`, `build`,
  `e2e`, `docker`, `ci`/`build-all`, `install`, `uninstall`, `run`, `start`, `stop`,
  `performer-logs`, `version`, `validate-version`, and `clean`.
- **FR-004**: Every target MUST delegate to the existing `bin/` script or `.venv` tool that
  already implements that capability; the Makefile MUST NOT reimplement build/test logic
  (`bin/build` remains the single source of truth for CI steps).
- **FR-005**: All targets MUST be declared `.PHONY`.
- **FR-006**: The test targets MUST always invoke pytest with the project-required
  environment-variable unset applied (so a contributor cannot run the suite the wrong way),
  and `test-all` MUST cover the whole `tests/` tree (unit + contract) in one invocation.
- **FR-007**: `make ci` (a.k.a. `make build-all`) MUST run exactly the same steps as the
  project's full local build (`bin/build --all`) with no divergence.
- **FR-008**: Targets that launch coordinare (`run`, `start`) MUST source the project
  environment file before launch, or fail with a clear message if it is absent (never launch
  with unexpanded/empty configuration placeholders).
- **FR-009**: When a required tool is missing (e.g. `.venv`), the affected target MUST fail
  with a clear, actionable message rather than a cryptic shell error, and MUST propagate the
  underlying command's non-zero exit code on failure.
- **FR-010**: `make clean` MUST remove only caches/build detritus (`__pycache__`,
  `.pytest_cache`, `.ruff_cache`, coverage artifacts, `*.pyc`) and MUST NOT delete source,
  tracked files, the venv, or env caches.
- **FR-011**: The `AGENTS.md` "Development" section MUST be updated to reference the make
  targets (`make help` / `make test` / `make ci`) as the canonical commands, while keeping
  the raw command fallbacks documented.
- **FR-012**: The Makefile MUST work with GNU make on macOS and Linux and MUST NOT depend on
  features unavailable in the macOS default `make`.

### Key Entities

- **Target**: a named make goal (e.g. `test-all`) with a category, a one-line help
  description, and a delegation to exactly one underlying `bin/` script or `.venv` tool.
- **Category**: the grouping shown in `make help` (dev / test / build / run / release /
  clean).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A newcomer can discover and run any project task using only `make help` — with
  zero prior knowledge of the `bin/` scripts or the raw `.venv` commands.
- **SC-002**: 100% of the existing `bin/` task surface listed in FR-003 is reachable through
  a `make` target.
- **SC-003**: `make ci` produces the same pass/fail outcome as `bin/build --all` on the same
  checkout (no drift between local and CI).
- **SC-004**: The test targets run the suite with the required environment-variable unset
  every time, with no way for a contributor to omit it.
- **SC-005**: No target reimplements logic already owned by a `bin/` script — each is a thin
  delegation (verifiable by inspection: a target body is a call to a script/tool, not a
  reimplementation of its steps).
- **SC-006**: A failing underlying command causes the corresponding `make` target to exit
  non-zero (failures are never masked).

## Assumptions

- GNU make is available (macOS default `make` is GNU make 3.81; Linux distros ship GNU make).
- The existing `bin/` scripts and `.venv` layout are unchanged by this feature; the Makefile
  wraps them as-is.
- The required pytest environment-variable unset is `env -u COORDINARE_INFERENCE_MAX_TOKENS -u
  HERMES_CONTEXT_WINDOW` (per project convention), and the project environment file is `.env`.
- "Full CI parity" means matching `bin/build --all`; the GitHub Actions workflows themselves
  are out of scope and unchanged.

## Out of Scope

- Rewriting, removing, or changing the behavior of any `bin/` script.
- Changing the GitHub Actions CI workflow definitions.
- Introducing or migrating to a new build system (uv/poetry/tox as a task runner).
- Windows / `nmake` support (GNU make on macOS + Linux only).
