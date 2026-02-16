# Contract: Docker Compose Runtime Invocation

**Feature**: 002-docker-cli-output

## Required Artifacts

- `Dockerfile` at repository root
- `docker-compose.yml` at repository root
- Config templates committed (`config.example.yaml`, `.env.example`)

## Runtime Commands

```bash
docker compose up --build coordinare
docker compose logs -f coordinare
docker compose down
```

## Required Behavior

- Compose service starts the same runtime entry behavior as shell mode.
- Container output preserves startup/activity/heartbeat/failure/shutdown semantics.
- Runtime errors result in non-zero container exit status.
- Known sensitive fields are redacted in output.

## Config Handling Rules

- Local runtime config instances are ignored by git.
- Local runtime config instances are excluded by docker build context ignore rules.

## Output Example

```text
startup: daemon startup complete (run_mode=compose)
activity: processing cycle completed (cycle=1 phase=running)
heartbeat: daemon heartbeat (cycle=1 phase=running)
shutdown: daemon stopped (graceful=true)
```
