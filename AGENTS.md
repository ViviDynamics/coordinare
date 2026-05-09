# coordinare Development Guidelines

## Stack

Python 3.12+, FastAPI, LangGraph, pydantic v2, pydantic-settings, structlog, httpx, gql[aiohttp], prometheus-client, asyncio (stdlib throughout).

## Commands

```sh
.venv/bin/pytest tests/unit/          # run unit tests
.venv/bin/ruff check src/ tests/      # lint
.venv/bin/ruff check --fix src/ tests/ # auto-fix lint
```

Always `set -a && source .env && set +a` before launching the coordinare daemon — `config.yaml` expands `${VAR}` placeholders at load time and silently uses empty strings without the env.

## Architecture

**`CoordinareState`** is a plain `dict[str, Any]` threaded through everything. Keys of note:
- `env_cache`: `dict[str, EnvCacheState]` — per-symphony bootstrap tracking
- `performer_services`: `dict[str, <service>]` — keyed by performer ID
- `symphony_github_services`: `dict[str, GitHubService]` — keyed by symphony name
- `performer_endpoints`: `dict[str, PerformerEndpointState]` — HTTP performer pool

**`Daemon`** drives the main poll loop in `daemon.py`. Each cycle iterates symphony configs and dispatches work via LangGraph nodes.

**Dispatch nodes** live in `src/coordinare/graph/nodes/`. They receive state, call performer services, and return state updates.

**Config** loads from `config.yaml` (with `${ENV_VAR}` expansion) via `CoordinareConfiguration` in `src/coordinare/config.py`. `SymphonyConfig.effective_config(global_config)` merges symphony-level overrides onto global defaults.

## Code Conventions

- `from __future__ import annotations` at the top of every module.
- Pydantic models that use `Path` or `datetime` as field types must import them at **runtime** (not under `TYPE_CHECKING`) — pydantic v2 calls `get_type_hints()` at class creation. Suppress the TC003 lint warning with `# noqa: TC003`.
- `TYPE_CHECKING` blocks are fine for everything else (protocols, heavy imports, circular refs).
- Background `asyncio.Task` references must be stored (e.g. in a `set` on the owning object with `add_done_callback(set.discard)`) to prevent GC — ruff RUF006 enforces this.
- Use `inspect.signature()` to check for optional parameters before passing them when calling across protocol boundaries (avoids breaking non-HTTP service implementations).
- Structured logging via `structlog.get_logger(__name__)`. Log keys use `snake_case.dot.separated` event names.

## Testing

- Unit tests in `tests/unit/test_<NNN>_<feature>.py`.
- Async tests use `@pytest.mark.asyncio`.
- Tests that call config discovery must mock `Path.cwd()` — a real `config.yaml` exists in the project root and will be picked up otherwise.
- `VolumeMount.container_path` is a `PurePosixPath`; use `str()` when comparing to string literals in tests.

## Workflow

- Feature branches named `<NNN>-<feature-name>` matching the spec directory.
- Specs live in `specs/<NNN>-<name>/` (spec.md, plan.md, tasks.md, data-model.md, contracts/).
- Run `/speckit.analyze` before `/speckit.implement`.
- PR target is `main`; squash merge.

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
