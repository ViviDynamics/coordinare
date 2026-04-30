# Quickstart — Containerized Performer Execution

End-to-end recipe to verify a containerized performer runs a real card. Assumes Docker Engine ≥ 24 is reachable on the coordinare host and that the coordinare itself runs natively (not yet containerized).

## 1. Build an image variant

From the repo root:

```bash
# heaviest variant — every backend + browser tooling
docker build -f agent/performer/Dockerfile.full -t performer:full agent/performer/

# lightweight, single backend
docker build -f agent/performer/Dockerfile.slim --build-arg BACKEND=claude_code -t performer:slim-claude agent/performer/

# toolchain only (BYO CLI)
docker build -f agent/performer/Dockerfile.base -t performer:base agent/performer/
```

## 2. Verify image satisfies the contract

```bash
docker run --rm -p 8088:8088 -e PERFORMER_AUTH_TOKEN=devtoken performer:full
# in another shell
curl -s -H "Authorization: Bearer devtoken" http://localhost:8088/status | jq .
# expect: { "availability": "idle", "capabilities": { "backends": [...], "tool_flags": [...] }, "auth_enabled": true, ... }
```

The contract test suite (`agent/performer/tests/contract/`) automates this against the OpenAPI schema in `contracts/performer-http.openapi.yaml`.

## 3. Register a persistent performer with the coordinare

`config.yaml` excerpt:

```yaml
performer_endpoints:
  - id: claude-1
    mode: persistent
    image: performer:slim-claude
    endpoint: http://localhost:8088
    roles: [writer]
    auth_token: ${PERFORMER_AUTH_TOKEN}
    failure_threshold: 5
    readiness_timeout_s: 120
    secret_sources:
      init_payload: true
      env: true
      creds_file: false
    volumes:
      - host_path: /Users/jane/projects/myrepo/.tooling
        container_path: /opt/project-tools
        mode: ro
```

## 4. Or register an ephemeral performer

```yaml
performer_endpoints:
  - id: codex-eph
    mode: ephemeral
    image: performer:slim-codex
    port: 8089
    roles: [writer]
    auth_token: ${PERFORMER_AUTH_TOKEN}
```

The coordinare will start the container on demand, dispatch the job, and tear the container down on completion.

## 5. Verify dispatch end-to-end

1. Start the coordinare: `set -a && source .env && set +a && .venv/bin/python -m coordinare`.
2. Watch the dashboard. The `claude-1` registration should appear in the performer pool widget with `availability=idle` within one poll cycle.
3. Drop a card matching the `writer` role. The coordinare selects `claude-1`, the performer transitions to `busy`, and progress events stream in over SSE.
4. On completion, status returns to `idle` and the card advances normally.

## 6. Verify fallover

1. Add a second registration `claude-2` against a second persistent container on `:8090`.
2. Dispatch two cards back-to-back. Confirm logs show one job to `claude-1`, one to `claude-2`, no serialization (SC-002).
3. Stop one of the containers (`docker stop`). Within `failure_threshold` poll cycles, the coordinare should mark it `unreachable` and emit a notification (Slack/email/dashboard). Restart the container; recovery is automatic on the next successful `/status`.

## 7. Verify secrets precedence

1. Set `PERFORMER_GITHUB_TOKEN=fromenv` in the container environment.
2. Mount a creds file with `GITHUB_TOKEN=fromfile`.
3. Dispatch a card whose init payload includes `secrets.GITHUB_TOKEN=frominit`.
4. Performer logs (`info` level only — never the value) should report `secret_resolved source=init_payload`. The job uses `frominit`. Remove the init value → `fromenv` is used. Disable `env` in `secret_sources` → `fromfile` is used.

## 8. Verify capability mismatch is reported up front

1. Register a performer using `performer:slim-claude` against a role that requires `browser`.
2. The coordinare should surface a configuration error before dispatching any card to that registration (SC-006).

## 9. Verify subprocess performers still work

Any performer registration with `mode: subprocess` (the default) bypasses the pool entirely. SC-008 / FR-024 require zero behavioral change for those.

## 10. Reconfigure a registration mid-flight (drain-then-reapply)

Hot-reloading a registration whose `mode`, `image`, or `endpoint` changed while a job is in flight is rejected (T012a). To swap safely:

1. Mark the registration as draining: `curl -X POST -H "Authorization: Bearer $PERFORMER_AUTH_TOKEN" http://<host>:<port>/admin/drain` (or set `availability: draining` in `config.yaml` and reload). The coordinare stops dispatching new jobs to it.
2. Wait for the current job to reach a terminal state (`succeeded`, `failed`, or `cancelled`). Watch the dashboard's pool widget — `current_job_id` will clear.
3. Edit `config.yaml`, change `mode` / `image` / `endpoint` as needed, and reload.
4. Coordinare performs a fresh readiness check against the new configuration; on success, availability returns to `idle`.

Forcing a config swap while `current_job_id` is set produces a configuration error and the prior registration remains active.

## Rollback

Containerization is opt-in. Setting every performer's mode to `subprocess` (or omitting `image`/`endpoint`) reverts to pre-056 behavior with no migration step.
