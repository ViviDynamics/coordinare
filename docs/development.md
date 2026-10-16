# Developing Coordinare

This checkout workflow is for maintainers changing the source. To run a release,
use the [Quick Start](../README.md#quick-start). Public pull requests remain closed
under the [contribution policy](../CONTRIBUTING.md).

## Checkout setup

Install Python 3.12 or newer, uv, git and Node 22 LTS. Clone and install both
packages into the checkout environment:

```sh
git clone https://github.com/ViviDynamics/coordinare.git
cd coordinare
uv sync --extra dev
uv pip install -e 'agent/performer[dev]'
make help
```

Install one supported harness CLI on PATH. For a checkout deployment, copy the
starter and environment template, then edit their GitHub, reviewer and credential
values. The starter enables only implementer and defaults to Codex. Other starter
harness names are `claude_code` and `opencode`. The copied starter uses a model API
key; guided init can also generate a setup using a local Codex or Claude login.
Use `config.example.yaml` as the complete configuration reference.

For a checkout using your own model server, copy
[`config.example.ollama.yaml`](../config.example.ollama.yaml) or
[`config.example.openai-compatible.yaml`](../config.example.openai-compatible.yaml)
instead of `config.minimal.yaml`. The
[`config.example.anthropic.yaml`](../config.example.anthropic.yaml) preset uses
the Anthropic API. Edit every `CHANGE ME` value. These model presets configure
implementer and reviewer; unconfigured roles are skipped, not failed.

```sh
cp config.minimal.yaml config.yaml
cp .env.example .env
```

Before running the daemon, read [developer onboarding](onboarding/README.md).
Source `.env` before direct validation or daemon commands so `${VAR}` references
resolve correctly. The Makefile run targets load it for you:

```sh
set -a && source .env && set +a
.venv/bin/python -m coordinare --config config.yaml config validate
.venv/bin/python -m coordinare --config config.yaml doctor
make run
```

Before dispatch, complete the [branch protection setup](../README.md#branch-protection)
so the Coordinare identity can squash-merge approved PRs.

Open <http://127.0.0.1:8090>. `bin/run-coordinare` uses the checkout's config and
loads its `.env`; `bin/run-performer` uses the performer project environment.
For a checkout login service, use `make install` and `make uninstall`.
For Kubernetes or Helm deployment, follow the
[chart README](../deploy/helm/coordinare/README.md).

## Run tests

The top-level Makefile is the canonical command entry point:

```sh
make test
make test-all
make test-js
make lint
make typecheck
make ci
```

Run `make test-all` before pushing. `make ci` delegates to `bin/build --all` and
includes browser tests and Docker builds. `bin/build` installs both packages'
dev dependencies, then runs lint, type checking, the ruff ratchet, tests and
coverage. `bin/build --e2e` adds browser tests; `bin/build --docker` adds Docker
builds. These require their corresponding runtimes.

## Project structure

```text
src/coordinare/           # Coordinare daemon
  graph/                 # LangGraph state machine
    nodes/               # Graph nodes (dispatch, monitor, classify, etc.)
  services/              # GitHub, notification, persona and other services
  dashboard/             # Live web dashboard (SSE) and static assets

agent/performer/         # Performer process and container
  src/performer/         # Wire protocol, backend adapters, git workspace
  tests/                 # Performer unit tests

specs/                   # Feature specifications (spec, plan, tasks for each feature)
tests/                   # Coordinare tests (unit, integration, contract, e2e)
```

## Key commands

| Command | Description |
|---------|-------------|
| `make help` | List all checkout tasks |
| `make run` | Start the daemon with `.env` loaded |
| `bin/run-coordinare` | Start the daemon using checkout config and `.env` |
| `bin/run-performer` | Run the performer locally (no Docker) |
| `make test` | Run unit tests |
| `make test-all` | Run the whole Coordinare tests tree with default markers |
| `make test-js` | Run dashboard JavaScript checks with Node |
| `make lint` | Run ruff over daemon source and tests |
| `make typecheck` | Run strict mypy with the repository ratchet |
| `bin/build` | Install dev dependencies; run lint, type checks, tests and coverage for both packages |
| `bin/build --e2e` | Also run Playwright browser tests |
| `make ci` / `bin/build --all` | Run full CI parity, including browser tests and Docker builds |
| `bin/check-agent` | Check performer health |
| `bin/performer-logs` | Stream performer stderr logs |
| `.venv/bin/python -m coordinare --config config.yaml config validate` | Validate checkout config with `.env` loaded |

The daemon health endpoint is <http://localhost:8080/health> by default.
`bin/check-agent` checks the performer transport; `bin/performer-logs` shows what
the active performer is doing. The dashboard activity feed distinguishes
"working" from "wedged" through progress and stall warnings.
[Notifications are optional](quickstart.md#notifications-are-optional): without
channels, watch the feed and daemon log for `card_stuck` warnings.
