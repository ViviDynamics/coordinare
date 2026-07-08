# Implementation Plan: Full Makefile Capabilities

**Branch**: `130-makefile` | **Date**: 2026-07-08 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/130-makefile/spec.md`

## Summary

Add one top-level `Makefile` that is a **thin, self-documenting façade** over the existing
`bin/` scripts and `.venv` tools. Every target delegates to the capability that already
exists (no reimplementation); `make help` (the default goal) prints a grouped, described
target list. Test targets bake in the required pytest env-unset; `make ci` == `bin/build
--all`; coordinare-launch targets source `.env` or fail loudly. `AGENTS.md`'s Development
section is repointed at the make targets.

## Technical Context

**Language/Version**: GNU make (macOS default 3.81; Linux GNU make) — POSIX `sh` recipes.
**Primary Dependencies**: existing `bin/` scripts (`build`, `install`, `uninstall`, `start`,
`stop`, `run-coordinare`, `performer-logs`, `update-version`, `validate-version`) and the
project `.venv` (`ruff`, `pytest`). No new dependencies.
**Storage**: N/A (no persisted state; the Makefile is stateless).
**Testing**: a `tests/unit/` test that asserts Makefile invariants statically (targets exist,
are `.PHONY`, `ci` maps to `bin/build --all`, test targets carry the env-unset, `clean` only
touches caches), plus manual `make help` / `make lint` smoke per quickstart.
**Target Platform**: developer + CI shells on macOS and Linux.
**Project Type**: single project (repo-root tooling file).
**Performance Goals**: `make help` prints in well under 1s; targets add negligible overhead
over calling the underlying script directly.
**Constraints**: must not depend on features absent from macOS `make` 3.81; must not
reimplement `bin/` logic; must not change any `bin/` script or CI workflow.
**Scale/Scope**: one `Makefile` (~20 targets) + one AGENTS.md edit + one guard test.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Code Quality First** — ✅ a Makefile that delegates (no duplicated logic) is the
  simplest thing that works; targets are single-responsibility; self-documenting `help`.
- **II. Testing Discipline (NON-NEGOTIABLE)** — ✅ the invariants that matter (target set,
  `.PHONY`, CI-parity mapping, env-unset presence, clean-safety) are covered by a static
  guard test in `tests/unit/`. There is no Python unit surface (the artifact is a Makefile);
  behavior is verified by asserting the Makefile's declared targets/recipes and by the
  quickstart smoke. Coverage does not decrease (net-new test, no prod code removed).
- **III. No hidden logic** — ✅ every recipe is a visible one-line delegation.
- **Minimal dependencies** — ✅ zero new dependencies; `make` is already present.

No violations → Complexity Tracking not required.

## Project Structure

### Documentation (this feature)

```text
specs/130-makefile/
├── plan.md              # This file
├── research.md          # Phase 0 — make-idiom decisions (help pattern, .PHONY, guards)
├── quickstart.md        # Phase 1 — how to use + how to verify the Makefile
├── checklists/
│   └── requirements.md  # spec quality checklist (already written)
└── tasks.md             # Phase 2 (/speckit.tasks)
```

`data-model.md` and `contracts/` are **N/A** for this feature — there is no data model and no
API/inter-service contract; the "interface" is the human-facing `make` target names, which
are captured in the spec (Key Entities) and the quickstart.

### Source Code (repository root)

```text
Makefile                 # NEW — the single façade (all targets .PHONY, help = default goal)
AGENTS.md                # EDIT — Development section points at make targets
bin/                     # UNCHANGED — build, install, start, stop, run-coordinare, …
tests/unit/
└── test_makefile.py     # NEW — static guard test (targets/.PHONY/CI-parity/env-unset/clean-safety)
```

**Structure Decision**: Single repo-root tooling file. The Makefile lives at the repository
root (where `make` is run); the guard test lives with the rest of the unit suite so it runs
under `make test` / CI like everything else.

## Target design (delegations — the single source of truth stays in `bin/`)

| Target | Category | Delegates to |
|---|---|---|
| `help` (default) | — | self-parse of `## ` target comments |
| `lint` | dev | `.venv/bin/ruff check src/ tests/` |
| `fmt` / `lint-fix` | dev | `.venv/bin/ruff check --fix src/ tests/` |
| `test` / `test-unit` | test | env-unset `.venv/bin/pytest tests/unit` |
| `test-contract` | test | env-unset `.venv/bin/pytest tests/contract` |
| `test-all` | test | env-unset `.venv/bin/pytest tests/unit tests/contract` |
| `build` | build | `bin/build` |
| `e2e` | build | `bin/build --e2e` |
| `docker` | build | `bin/build --docker` |
| `ci` / `build-all` | build | `bin/build --all` |
| `install` / `uninstall` | release | `bin/install` / `bin/uninstall` |
| `run` / `start` / `stop` | run | source `.env` then `bin/run-coordinare` / `bin/start` / `bin/stop` |
| `performer-logs` | run | `bin/performer-logs` |
| `version` / `validate-version` | release | `bin/update-version` / `bin/validate-version` |
| `clean` | clean | remove `__pycache__`, `.pytest_cache`, `.ruff_cache`, coverage, `*.pyc` |

- **Env-unset** = `env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW`, held in a
  single `PYTEST` variable so all test targets share it (FR-006, SC-004).
- **`.venv` guard**: a small `require-venv` check prints an actionable message if
  `.venv/bin/python` is absent (FR-009).
- **`.env` guard**: `run`/`start` fail with a clear message if `.env` is missing (FR-008).
- **`.PHONY`**: a single `.PHONY:` line lists every target (FR-005).

## Phase 0 / Phase 1 outputs

- **research.md**: the make idioms chosen (self-documenting `help` via `##` comment parsing;
  `.DEFAULT_GOAL := help`; `.PHONY` for all; `--warn-undefined-variables`; POSIX-portable
  recipes for 3.81) with rationale and alternatives.
- **quickstart.md**: `make help`, the common flows (`make test`, `make ci`, `make run`), and
  how to verify parity/safety.

## Complexity Tracking

No constitution violations — section intentionally empty.
