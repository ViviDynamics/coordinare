# Research: Full Makefile Capabilities (spec 130)

Small feature; the only real decisions are which make idioms to use so the file is
portable, self-documenting, and safe. No open NEEDS CLARIFICATION.

## Decision: self-documenting `help` via `##` comment parsing

- **Decision**: Annotate each public target with a trailing `## <group>: <description>`
  comment and generate `help` by `grep`/`awk`-parsing the Makefile itself. Set
  `.DEFAULT_GOAL := help` so a bare `make` prints help.
- **Rationale**: Keeps the help text next to each target (can't drift), needs no external
  tool, and is the widely-used idiom. Grouping by the token before `:` gives the
  dev/test/build/run/release/clean sections (FR-002).
- **Alternatives considered**: a hand-maintained `help` recipe (drifts from the targets); a
  separate docs file (drifts, not discoverable from the CLI).

## Decision: GNU make 3.81 compatibility (macOS default)

- **Decision**: Restrict to features present in GNU make 3.81 — simple `:=`/`=` variables,
  `.PHONY`, `.DEFAULT_GOAL`, POSIX `sh` recipes. Avoid `.ONESHELL`, `$(file …)`, and GNU-4.x
  functions.
- **Rationale**: macOS ships GNU make 3.81 by default; requiring a newer make (via Homebrew
  `gmake`) would break the "just run `make`" promise (FR-012).
- **Alternatives considered**: requiring `gmake` (extra install friction); a shell task-runner
  like `just` (new dependency, out of scope).

## Decision: single `PYTEST` variable carrying the env-unset

- **Decision**: Define `PYTEST := env -u COORDINARE_INFERENCE_MAX_TOKENS -u
  HERMES_CONTEXT_WINDOW .venv/bin/pytest` once; every test target uses `$(PYTEST) <scope>`.
- **Rationale**: Guarantees the required unset on every test target with no way to omit it
  (FR-006, SC-004), and keeps the invocation in one place. A static test asserts the variable
  carries both `-u` flags.
- **Alternatives considered**: repeating the `env -u …` on each target (easy to forget one).

## Decision: delegate, never reimplement (`bin/build` stays authoritative)

- **Decision**: `build`/`e2e`/`docker`/`ci` targets call `bin/build [flags]`; `ci` ==
  `bin/build --all`. Operator targets call the matching `bin/` script verbatim.
- **Rationale**: `bin/build` is already the CI-parity source of truth; wrapping it (not
  copying its steps) is what prevents drift (FR-004, FR-007, SC-003, SC-005).
- **Alternatives considered**: expanding the individual lint/test/coverage/docker steps as
  separate make targets that CI-parity `ci` recomposes — rejected: it would duplicate
  `bin/build`'s orchestration and invite drift. (Individual `lint`/`test` targets still exist
  for convenience, but `ci` delegates to `bin/build --all`, not to those targets.)

## Decision: guards for missing `.venv` / `.env`

- **Decision**: A `require-venv` order-only style check (`test -x .venv/bin/python || { echo
  "…create the venv…"; exit 1; }`) fronts venv-dependent targets; `run`/`start` check for
  `.env` and fail with a clear message before sourcing it.
- **Rationale**: Turns cryptic "command not found" / silent empty-config launches into
  actionable errors (FR-008, FR-009).
- **Alternatives considered**: letting the underlying command fail (cryptic); auto-creating
  the venv (surprising side effect, out of scope).

## Decision: how to test a Makefile

- **Decision**: A static guard test in `tests/unit/test_makefile.py` reads the `Makefile`
  text and asserts the invariants: all documented targets present; a single `.PHONY` lists
  them; `ci`/`build-all` recipe calls `bin/build --all`; every `test*` target uses `$(PYTEST)`
  (which carries both `-u` flags); `clean` only removes cache globs (never `src`, `.venv`,
  `.env`, env-caches). Plus a `make -n help` / `make help` smoke in quickstart.
- **Rationale**: These are the properties whose regression would actually hurt (drift,
  missing env-unset, dangerous clean). They're cheaply and deterministically checkable from
  the file text, run in the normal unit suite, and don't require executing docker/e2e.
- **Alternatives considered**: executing every target in CI (slow, needs docker/network);
  no test (violates Testing Discipline; lets drift/clean-safety regress silently).
