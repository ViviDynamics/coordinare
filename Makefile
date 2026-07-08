# Makefile — single, self-documenting entry point for coordinare dev / CI /
# operator tasks (spec 130). It is a THIN FAÇADE: every target delegates to an
# existing bin/ script or a .venv tool — no build/test logic is reimplemented
# here (bin/build stays the single source of truth for CI steps).
#
# Run `make` (or `make help`) to see everything. GNU make 3.81+ (macOS default).

.DEFAULT_GOAL := help
SHELL := /bin/bash

# The required pytest invocation. The env-unset MUST always be applied (project
# convention), so it lives in ONE place and every test target reuses it — no
# target can accidentally omit it. `make ci` uses bin/build for exact CI parity.
PYTEST := env -u COORDINARE_INFERENCE_MAX_TOKENS -u HERMES_CONTEXT_WINDOW .venv/bin/pytest
RUFF   := .venv/bin/ruff

.PHONY: help \
        lint fmt lint-fix \
        test test-unit test-contract test-all \
        build e2e docker ci build-all \
        run start stop performer-logs \
        install uninstall version validate-version \
        clean require-venv require-env

help: ## show this help
	@awk 'BEGIN {FS = ":.*##"} \
	  /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
	  /^[a-zA-Z0-9_.-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' \
	  $(MAKEFILE_LIST)
	@echo ""

# Internal guards (no `##` → hidden from help).
require-venv:
	@test -x .venv/bin/python || { \
	  echo "error: .venv not found (needed for lint/test targets)."; \
	  echo "  create it, e.g.:  uv sync --extra dev"; \
	  echo "               or:  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"; \
	  exit 1; }

require-env:
	@test -f .env || { \
	  echo "error: .env not found — refusing to launch coordinare."; \
	  echo "  config.yaml expands \$${VAR} placeholders at load time and would"; \
	  echo "  silently use empty strings without it. Create .env first."; \
	  exit 1; }

##@ Dev
lint: require-venv ## ruff lint over src/ and tests/
	$(RUFF) check src tests

fmt: require-venv ## ruff auto-fix over src/ and tests/
	$(RUFF) check --fix src tests

lint-fix: fmt ## alias for `fmt`

##@ Test
test: test-unit ## alias for `test-unit`

test-unit: require-venv ## run the unit test suite
	$(PYTEST) tests/unit -q

test-contract: require-venv ## run the contract test suite
	$(PYTEST) tests/contract -q

test-all: require-venv ## run the WHOLE tests/ tree (unit + contract)
	$(PYTEST) tests/unit tests/contract -q

##@ Build
build: ## local build = CI checks (lint + unit + coverage + performer tests)
	bin/build

e2e: ## build + Playwright browser (E2E) tests
	bin/build --e2e

docker: ## build + verify the performer Docker images
	bin/build --docker

ci: ## full CI parity — exactly what CI runs
	bin/build --all

build-all: ci ## alias for `ci`

##@ Run
run: require-env ## start the coordinare daemon (sources .env first)
	set -a && . ./.env && set +a && bin/run-coordinare

start: require-env ## start coordinare + performer in one command (sources .env)
	set -a && . ./.env && set +a && bin/start

stop: ## stop the coordinare daemon
	bin/stop

performer-logs: ## tail live stderr from the active performer
	bin/performer-logs

##@ Release
install: ## install coordinare as a login LaunchAgent (auto-start)
	bin/install

uninstall: ## remove the coordinare LaunchAgent and stop it
	bin/uninstall

version: ## bump/update the project version
	bin/update-version

validate-version: ## validate the version file
	bin/validate-version

##@ Clean
clean: ## remove caches only (pycache, pytest, ruff, coverage, *.pyc)
	@find src tests agent -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	@find src tests agent -type f -name '*.pyc' -delete 2>/dev/null; true
	@rm -rf .pytest_cache .ruff_cache .coverage htmlcov coverage.xml
	@echo "cleaned caches (source, .venv, .env, env-caches untouched)"
