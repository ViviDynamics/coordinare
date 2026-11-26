# Default every persona to the nare backend

Issue #382. Part of #381. Stacked on #360, which makes the `nare` backend run.

## Scope
In: every place that picks a backend when the operator names none now picks
`nare`: the role default, presets and init, the env_bootstrap, tech_writer and
intake fallbacks, the bootstrap payload, dry run, launch preflight and the
performer's `AGENT_BACKEND`. Starter configs and the quick start say so, and
`config.example.nare.yaml` shows both of nare's rails.

Out: installing nare on the host from `install.sh` (filed separately; containers
already carry it), and env bootstrap's own conversation loop (#376).

## Assumptions
- An explicit `backend:` or `harness:` always wins; nothing rewrites a written choice.
- The starter rail for nare is anthropic with `ANTHROPIC_API_KEY`, nare's own default.
- `harness` becomes optional in a preset and defaults to the first `HARNESS_DEFAULTS` entry.

## Tasks
- [x] 1. Role, preset, init and daemon fallbacks: `tests/unit/test_382_default_nare.py`.
- [x] 2. A nare bootstrap gets its rail from the endpoint: `test_a_nare_bootstrap_gets_its_rail_from_the_endpoint`.
- [x] 3. Tests that pinned opencode as the default now pin nare.
- [x] 4. Starter configs, `config.example.nare.yaml`, README and first-launch docs.
- [x] 5. env_bootstrap on nare reaches its cache: nare 2026.11.111 (`--add-root`, ViviDynamics/nare#319), the cache's parent passed as an added root: `test_env_bootstrap_file_tools_reach_the_cache`, `test_an_env_bootstrap_run_writes_into_its_cache`.
