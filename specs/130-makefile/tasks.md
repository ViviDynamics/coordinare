# Tasks: Full Makefile Capabilities

**Feature**: `130-makefile` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

Tests included (constitution NON-NEGOTIABLE): a static guard test asserts the Makefile
invariants. `[P]` = parallelizable (different files, no incomplete deps).

## Phase 1: Setup

- [x] T001 Green baseline: `env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest tests/unit tests/contract -q` + `.venv/bin/ruff check src tests`. Confirm the current `bin/` surface by listing `bin/` and reading `bin/build`'s usage header (source of truth for build/e2e/docker/all).

## Phase 2: Foundational (blocks both stories)

- [x] T002 Create the `Makefile` skeleton at repo root: `.DEFAULT_GOAL := help`, a single `PYTEST := env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest` variable, a `require-venv` guard recipe (actionable message + non-zero exit when `.venv/bin/python` absent), and a single `.PHONY:` line (populated as targets are added). GNU make 3.81-compatible; no `.ONESHELL`/4.x-only features.

## Phase 3: US1 — One discoverable entry point (P1) 🎯 MVP

**Goal**: `make`/`make help` prints a grouped, self-documenting target list; every listed
target delegates to the existing `bin/` script or `.venv` tool.

**Independent test**: `make` (no target) prints the grouped list and exits 0; `make lint`
runs ruff; `make build` runs `bin/build`.

- [x] T003 [US1] Write the guard test FIRST at `tests/unit/test_makefile.py`: assert the `Makefile` exists; every target in FR-003 is defined; all are listed in `.PHONY`; `make -n help` (or a text parse) yields grouped, non-empty descriptions; each `bin/`-backed target's recipe references the matching `bin/<script>`. (Fails until T004–T005 land.)
- [x] T004 [US1] Add the self-documenting `help` target to `Makefile`: parse `## <group>: <desc>` annotations and print them grouped (dev / test / build / run / release / clean). Bare `make` → help, exit 0.
- [x] T005 [US1] Add the delegating targets to `Makefile` (each annotated + added to `.PHONY`, each fronted by `require-venv` where it needs the venv): `lint`, `fmt`/`lint-fix` (ruff), `build`/`e2e`/`docker` (`bin/build [flags]`), `install`/`uninstall`, `run`/`start`/`stop`, `performer-logs`, `version`/`validate-version`, `clean` (remove only `__pycache__`, `.pytest_cache`, `.ruff_cache`, coverage artifacts, `*.pyc`). Make T003 pass.

## Phase 4: US2 — CI parity & safety guaranteed (P2)

**Goal**: test targets always carry the env-unset; `make ci` == `bin/build --all`;
`run`/`start` source `.env` or fail loudly; AGENTS.md points at the make targets.

**Independent test**: `make -n test-all` shows the env-unset; `make ci` mirrors `bin/build
--all`; `make run` with `.env` absent fails with a clear message.

- [x] T006 [US2] Extend `tests/unit/test_makefile.py`: assert every `test*` target's recipe uses `$(PYTEST)` (which carries both `-u` flags); `ci` and `build-all` recipes call `bin/build --all`; `run`/`start` recipes guard/source `.env`; `clean` removes only the cache globs (never `src`, `.venv`, `.env`, env-caches). (Fails until T007–T008.)
- [x] T007 [US2] Add the test + CI-parity + launch-safety targets to `Makefile`: `test`/`test-unit` (`$(PYTEST) tests/unit`), `test-contract` (`$(PYTEST) tests/contract`), `test-all` (`$(PYTEST) tests/unit tests/contract`); `ci`/`build-all` → `bin/build --all`; `run`/`start` fail with a clear message if `.env` is missing, else `set -a && . ./.env && set +a` before the launch script. Make T006 pass.
- [x] T008 [US2] Update `AGENTS.md` "Development" section to present `make help` / `make test` / `make test-all` / `make ci` as the canonical commands, keeping the raw `.venv/bin/...` fallbacks documented.

## Phase 5: Polish

- [x] T009 Manual smoke per [quickstart.md](./quickstart.md): `make help`, `make lint`, `make -n test-all | grep -- '-u COORDINARE_INFERENCE_MAX_TOKENS'`, `make -n clean`, and the missing-`.venv`/missing-`.env` guards.
- [x] T010 Full suite green (unit + **contract**) + `ruff check` clean; adversarial review before merge; confirm coverage not decreased.

## Dependencies

Phase 1 → Phase 2 → (US1, then US2). US2 depends on the skeleton (T002) and reuses the
`Makefile`/test file from US1, so it follows US1. Polish last. **MVP = US1** (a discoverable,
delegating entry point is independently valuable even before the parity/safety guarantees).

## Notes

- **Run the WHOLE `tests/` tree** (unit + contract) before pushing.
- The Makefile is a **thin façade** — recipes call `bin/` scripts / `.venv` tools; never
  reimplement `bin/build`'s steps (it stays the CI-parity source of truth).
- Single file for T004–T007 (`Makefile`) → those are sequential, not `[P]`. The guard test
  (`tests/unit/test_makefile.py`) is a separate file but its assertions track the Makefile, so
  write-test-then-implement per story.
