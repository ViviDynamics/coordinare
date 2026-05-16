# Quickstart: Rails Symphony with Service Inference

End-to-end walkthrough for an operator standing up a Rails symphony that needs
Postgres (and Redis): clone the project, point coordinare at it, let
service-inference (spec 063) discover what's needed, and dispatch the first
card. Assumes a working coordinare install with a containerized env-bootstrap
performer (see [Containerized Performers](containerized-performers.md)).

The goal: by the end of this guide, an `rspec` card running inside a QA
performer finds a live Postgres on `localhost:5432` without you having written
a `services-start.sh` by hand.

## Prerequisites

- Coordinare running (`set -a && source .env && set +a && coordinare daemon`).
- A performer image with the bootstrap tooling — `coordinare-performer:full`
  works out of the box.
- `ANTHROPIC_API_KEY` exported in coordinare's environment (the inference
  agent uses it). If unset, the agent skips with reason `no_api_key` and you'll
  need to fall back to the manual override (see
  [Manual Override](service-inference-manual-override.md)).
- `env_cache_root` configured and writable on the coordinare host, with the
  same path mounted into the bootstrap performer's container.

## 1. Clone the project

Pick any Rails app that uses Postgres. For this walkthrough the fixture
is `Gemfile` + `config/database.yml`:

```bash
git clone git@github.com:your-org/rails-app.git
cd rails-app
cat Gemfile | grep -E "^gem '(rails|pg|redis)'"
# gem 'rails'
# gem 'pg'
# gem 'redis'

cat config/database.yml | head -5
# default: &default
#   adapter: postgresql
#   ...
```

No `.coordinare/` directory is needed — inference handles a vanilla Rails
layout.

## 2. Register the symphony with coordinare

Add (or extend) `config.yaml`:

```yaml
github_org: your-org
human_reviewers: [you@example.com]
github_token: ${GH_TOKEN}

env_cache_root: /var/lib/coordinare/envcache
container_devenv_root: /devenv

performer_endpoints:
  - id: bootstrap-env
    mode: ephemeral
    roles: []
    image: coordinare-performer:full
    readiness_timeout_s: 300
    env_cache_mount: true

  - id: qa-perf
    mode: persistent
    roles: [qa]
    image: coordinare-performer:full
    env_cache_mount: true

symphonies:
  - name: rails-app
    github_project_number: 42
    env_bootstrap_performer_id: bootstrap-env
    env_spec_files: [README.md, Gemfile, config/database.yml]
```

Two things worth calling out:

- **`env_spec_files`** drives bootstrap invalidation: when coordinare sees the
  SHA of any listed file change in the symphony's repo, it dispatches a fresh
  `env_bootstrap` job.
- Service inference adds its own `cache_inputs` on top of this — once the LLM
  runs, files like `Gemfile.lock` may also force regeneration. The two layers
  compose; you don't have to predict every input.

Reload the daemon:

```bash
set -a && source .env && set +a
coordinare reload  # or restart the daemon
```

## 3. Trigger the first env-cache build

Coordinare's env-cache watcher polls README/spec-file SHAs. To force an
immediate cycle, make a trivial edit to any file in `env_spec_files` (e.g.
bump a comment in `README.md`) and push to the symphony's default branch.
The watcher picks up the new SHA on its next poll and dispatches a job.

What happens behind the scenes:

1. Coordinare builds a `BootstrapJobPayload` and dispatches it to the
   `bootstrap-env` performer.
2. The performer container mounts `/devenv/rails-app` (from
   `env_cache_root/rails-app` on the host) read-write.
3. It runs the README/Gemfile install steps (`bundle install`, etc.).
4. **Service inference runs.** The agent reads `Gemfile`, `config/database.yml`
   and emits a `ServicesManifest`. Look for:
   ```
   service_inference.attempt agent_version=063-v1 attempt=1
   service_inference.manifest_valid services=[postgres,redis]
   service_inference.scripts_written path=/devenv/rails-app/services
   ```
5. The templater renders `services-{start,stop,health}.sh`; the validator
   dry-runs them; the cache is marked ready.

## 4. Verify the env cache

On the host:

```bash
ls /var/lib/coordinare/envcache/rails-app/services/
# services.json
# services-start.sh
# services-stop.sh
# services-health.sh

jq '.services[] | {name, port, version}' \
  /var/lib/coordinare/envcache/rails-app/services/services.json
# { "name": "postgres", "port": 5432, "version": "16" }
# { "name": "redis",    "port": 6379, "version": "7" }
```

On the dashboard, the `rails-app` env-cache panel should show:

| Field | Value |
|---|---|
| Service inference | `ok · postgres, redis · 1 attempt · 063-v1` |
| Inference at | a few seconds ago |

If you see `failed` or `skipped: <reason>` instead, jump to
[Troubleshooting](#troubleshooting).

## 5. Dispatch the first card

Create a card in the symphony's GitHub Project board (status: "Ready") that
maps to a QA performer role — e.g. a card titled "Run rspec smoke suite".

Coordinare's poller will:

1. Pick up the card.
2. Dispatch to `qa-perf`.
3. The performer container starts with `/devenv/rails-app` mounted read-only.
4. `devenv-profile.sh` sources the cache and runs
   `/devenv/rails-app/services/services-start.sh` — postgres + redis come up
   as child processes inside the container.
5. The QA agent runs `bundle exec rspec`. It connects to
   `postgres://localhost:5432` and the suite executes against the live DB.

Watch the performer logs (or the dashboard's job stream) for:

```
services-start.sh: postgres listening on 5432
services-start.sh: redis listening on 6379
services-health.sh: ok (postgres:5432, redis:6379)
```

The card moves to the next column on completion.

## Troubleshooting

**Dashboard shows `Service inference: skipped: no_api_key`**
The coordinare process can't see `ANTHROPIC_API_KEY`. Confirm
`set -a && source .env && set +a` ran before launching the daemon — config
placeholders silently empty otherwise. Restart coordinare and rebuild the cache.

**Dashboard shows `Service inference: failed · 3 attempts`**
The LLM produced a manifest but validation rejected it three times. Check the
performer logs for `service_inference.validation_failed` lines — they include
the failing script and exit code. Common causes: a `binary` not present in
the image, or `start_args` that exit before opening their port. The
fastest unblock is to commit a [Manual Override](service-inference-manual-override.md)
with a known-good service entry.

**Dashboard shows `Service inference: skipped: manual_override`**
Working as intended — a `.coordinare/score.json` was found in the project repo
and used verbatim. Delete it to re-enable LLM inference.

**Health check times out after the cache is ready**
The cache was built but at performer runtime `services-health.sh` exits
non-zero. Coordinare marks the cache `runtime_health_failed`; the next
bootstrap cycle forces a regeneration that bypasses the cache key. If the
problem persists, the issue is reproducible — exec into the performer and
run `services-health.sh` manually to see what's failing.

**Build picks the wrong services**
The LLM mis-read your repo. Easiest fix: commit
`.coordinare/score.json` with the right shape (see [Manual Override](service-inference-manual-override.md))
and the next bootstrap will skip the LLM entirely.

## Next steps

- Read [Manual Override](service-inference-manual-override.md) to learn how
  to pin services deterministically.
- See `specs/063-llm-service-inference/spec.md` for the full success
  criteria (SC-007 calls out exactly this Rails-fixture flow).
- Wire additional performer roles (e.g. dev, review) once the QA loop is
  green.
