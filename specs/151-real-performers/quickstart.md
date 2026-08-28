# Quickstart: Board-Simulation Benchmark — Real Performers (151)

## What this gives you

Drive one fixture card through the **full lifecycle with real performers** (real model
dispatch through `/jobs`) while GitHub stays **fully faked at the performer's own
boundary** — the performer container clones/pushes a harness-local git remote and opens
its PR + polls checks against a harness-local REST endpoint. No packet reaches real
GitHub. Same 134 `run.json`, now recording real per-persona dispatches.

## Try the free stub run first (no Docker, no model, no keys)

Before any real-mode setup, confirm the harness works end-to-end deterministically:

```bash
# built-in tiny fixture:
python scripts/board_bench.py --run-dir runs/
# or the shipped example manifest:
python scripts/board_bench.py \
  --fixtures specs/151-real-performers/examples/fixture-manifest.yaml --run-dir runs/
```

This drives the card to `merged` with the stub implementer + real CI/merge, no cost.
Only once that works is it worth setting up the real lane below.

## Prerequisites for the real (`--real`) run — do these IN ORDER

`--fixtures` takes a **file you provide**; **omit it** to use the built-in tiny fixture,
or pass the shipped `specs/151-real-performers/examples/fixture-manifest.yaml` (or a copy
you edit). Passing a path that doesn't exist fails immediately with `FileNotFoundError`
before anything else runs — that's the most common first stumble.

1. **Coordinare dev environment** — `make` targets; `.venv` via `uv`. `git` on PATH (the
   harness runs `git daemon`).
2. **Any Docker.** Portable across Docker Desktop (macOS/Windows/WSL2) and native Linux.
   Performers launch on the default bridge and reach the harness-local fake services on
   the host via `host.docker.internal` (added with `--add-host=host.docker.internal:host-gateway`
   — `host-gateway` works on Docker 20.10+). The performer accepts that http GitHub URL
   only under the `ALLOW_HOST_GATEWAY_GITHUB` opt-in the bench sets; without it a
   non-loopback host silently falls back to real GitHub. Verify: `docker info` succeeds.
   (WSL2 note: Docker Desktop works here — no native-in-distro daemon needed.)
3. **Build the performer image** named by the config's `image:` field
   (`coordinare-performer:full` in `bench-real.yaml`). Use the repo's build helper —
   `bin/build --docker` (the CI source of truth; builds `coordinare-performer:base` +
   `:full`). Verify: `docker images | grep coordinare-performer`.
4. **Export the model API key** the config's backend needs. `bench-real.yaml` uses
   `backend: opencode` on the `anthropic-cloud` endpoint (`auth_env: ANTHROPIC_API_KEY`),
   so `export ANTHROPIC_API_KEY=…`. Keep the model cheap — the example pins
   `claude-haiku-4-5-20251001`. If you switch backend/endpoint, set that endpoint's key.
5. **Copy + adjust the config** (`bench-real.yaml`) if your image name, backend, or model
   differ from the defaults. Do NOT change `github_org`/`project_name` — they MUST stay
   `bench-org`/`bench-repo` (the FakeGitHubService identity).

Optional: the sibling `ViviDynamics/conductor-bench` repo supplies richer whole-lifecycle
fixtures; the shipped example needs no external repo.

## Run it (opt-in, real mode)

```bash
python scripts/board_bench.py --real \
  --config specs/151-real-performers/examples/bench-real.yaml \
  --fixtures specs/151-real-performers/examples/fixture-manifest.yaml \
  --run-dir runs/
```

The example config declares the **full** lifecycle (assessor → closer) with every gate
role (reviewer/security/qa) enabled. That coupling is deliberate (US4/T027): a card
reaches `IN_REVIEW` only after the real gates pass, which is what makes `gates_green`'s
`card_status == "IN_REVIEW"` approval proxy valid in real mode. Its ephemeral endpoint
sets `extra_hosts: [host.docker.internal:host-gateway]` + `env.BACKEND: opencode` (so
`entrypoint.sh` installs the backend CLI at container start) +
`env.ALLOW_INSECURE_REPO_URL: "1"` + `env.ALLOW_HOST_GATEWAY_GITHUB: "1"`, and
`github_org` / `project_name` MUST equal the FakeGitHubService identity
(`bench-org` / `bench-repo`).

`--real` runs `run_board(stub=False)`: the model-touching graph nodes are **not**
overridden, so real performers dispatch. The runner stands up the git daemon + fake
REST (bound `0.0.0.0`), threads **host-facing** `127.0.0.1` URLs into `git_base_url`
(the coordinare's own clone + rebase) and **container-facing** `host.docker.internal`
URLs into `performer_git_base_url` / `github_api_url` / `github_graphql_url` (what the
performer sees), dispatches the performer on the bridge with the host-gateway `--add-host`
+ `ALLOW_INSECURE_REPO_URL=1` + `ALLOW_HOST_GATEWAY_GITHUB=1`, probes reachability (fails
fast if the services aren't up), then drives the daemon to terminal under 134's cycle +
wall-clock budget.

Inspect — the tell for a real run is **>1** dispatch per card:

```bash
jq '.cards[] | {title, final_state, merged: .merge.merged,
     dispatches: [.dispatches[] | {stage, role, status}]}' \
  runs/<ts>-<confighash>/run.json
```

## Prove no real GitHub was touched (SC-003)

Run with egress to GitHub denied and confirm the card still terminates + an artifact is
still written:

```bash
# e.g. block api.github.com / github.com for the performer container, then:
python scripts/board_bench.py --real --config ... --fixtures ... --run-dir runs/
jq '.cards[0].final_state' runs/<ts>-<confighash>/run.json   # non-null terminal state
```

## The automated tests

```bash
make test        # unit + contract: fake-REST shapes, git-host injection,
                 # graphql/regex config, performer-boundary round-trip
make test-all    # adds 134's stubbed end-to-end (unchanged, must stay green)
```

The **real-model** end-to-end run is opt-in (needs Docker + a paid model) and lives in a
separate, non-unit lane — never in the free deterministic CI (SC-006, 134 parity).

## How it works (one paragraph)

`run_board(stub=False)` reuses 134's setup (materialize bare repo, seed cards into
`FakeGitHubService(approver=gates_green)`) and additionally starts a
`FakeGitHubServer`: `git daemon` + an aiohttp REST surface serving PR-create /
default-branch / check-runs, both bound `0.0.0.0` and delegating to the same
`FakeGitHubService` (one PR identity, one real-pytest CI). It gives the **host** a
`127.0.0.1` view (config `git_base_url` — the coordinare's own clone + rebase) and the
**container** a `host.docker.internal` view (config `performer_git_base_url` +
`github_api_url` + `github_graphql_url` — what the performer sees), and launches the
performer on the bridge with `--add-host=host.docker.internal:host-gateway` +
`ALLOW_INSECURE_REPO_URL` + `ALLOW_HOST_GATEWAY_GITHUB`; `github_graphql_url` reaches the performer via a new
`Score.github_graphql_url` field + `main.py` override, the same route `github_api_url`
already takes. It builds the graph with **no** stub overrides and runs the real daemon.
The performer clones, writes code, pushes, and `POST .../pulls` → `open_pr` mints `PR_N`;
the coordinare reads that identity back and later `check_mergeability`/`squash_merge` it. On
termination the runner records each real dispatch into 134's artifact, validates it, and
tears down the daemon, server, containers, and scratch.

## Gotchas

- Every injection field **defaults to production** (`https://github.com`,
  `api.github.com`, `https`-only repo_url). A run is "real bench mode" only when the
  harness sets all of them — nothing changes for production unless the bench opts in.
- The container reaches the fake over `host.docker.internal` (bridge + host-gateway); the
  host reaches it over `127.0.0.1`. If the services aren't up the run fails fast (by
  design), it does not hang. The performer accepts the `host.docker.internal` http URL
  only under `ALLOW_HOST_GATEWAY_GITHUB=1`; without that opt-in it silently falls back to
  real GitHub — the bench sets it, production never does.
- The fake REST/`FakeGitHubService` must never raise inside a graph node (aborts
  `daemon.start()`); best-effort degrade, same 134 invariant.
- `resolve_pr_review_threads` graphql is best-effort — a 404 from the fake is fine; the
  only requirement is it never reaches `api.github.com`.
- Cost stays a token×rate **estimate** (FR-012), not authoritative proxy USD.
</content>
