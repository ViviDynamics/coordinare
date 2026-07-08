# Quickstart: Makefile targets (spec 130)

The `Makefile` at the repo root is the single entry point for developer, CI, and operator
tasks. It delegates to the existing `bin/` scripts and `.venv` tools — nothing is
reimplemented.

## Discover everything

```bash
make            # same as `make help` — prints the grouped, described target list
make help
```

## Common flows

```bash
# dev
make lint              # ruff check src/ tests/
make fmt               # ruff --fix (alias: make lint-fix)

# test (env-unset is applied automatically — you never type it)
make test              # unit tests
make test-contract     # contract tests
make test-all          # the whole tests/ tree (unit + contract)

# build / CI parity
make build             # bin/build (lint + unit + coverage + performer tests)
make e2e               # bin/build --e2e
make docker            # bin/build --docker
make ci                # bin/build --all  (exactly what CI runs; alias: make build-all)

# run / operate (these source .env first, or fail loudly if it's missing)
make run               # bin/run-coordinare
make start             # bin/start
make stop              # bin/stop
make performer-logs    # bin/performer-logs

# release
make version           # bin/update-version
make validate-version  # bin/validate-version

# housekeeping
make clean             # remove caches only (__pycache__, .pytest_cache, .ruff_cache, coverage, *.pyc)
```

## Verify the guarantees

```bash
# CI parity: make ci runs the same steps as the project's full build
make ci ; echo "exit=$?"          # matches: bin/build --all ; echo "exit=$?"

# env-unset is baked in: the test recipe shows the required unset
make -n test-all | grep -- '-u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW'

# missing-venv guard is actionable (temporarily hide the venv)
mv .venv .venv.bak && make lint ; echo "exit=$?" ; mv .venv.bak .venv
# → prints a clear "create the venv" message and exits non-zero

# missing-.env guard on launch targets
mv .env .env.bak && make run ; echo "exit=$?" ; mv .env.bak .env
# → fails loudly instead of launching with empty config placeholders

# clean is safe (dry-run shows only cache globs, never src/.venv/.env)
make -n clean
```

## Guard test

`tests/unit/test_makefile.py` asserts the invariants statically (all targets present + all
`.PHONY`; `ci`/`build-all` → `bin/build --all`; every `test*` target carries the env-unset;
`clean` touches only caches). It runs as part of `make test` / CI.
