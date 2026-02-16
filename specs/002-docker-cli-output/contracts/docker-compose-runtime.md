# Contract: Docker Compose Runtime

**Feature**: 002-docker-cli-output  
**Consumer**: Developers/operators running coordinare via Docker Compose

## Compose Artifacts

- `Dockerfile` MUST exist at repository root.
- `docker-compose.yml` MUST exist at repository root.

## Service Contract

Service: `coordinare`

Expected behavior:
- Service starts the same application runtime path as shell mode.
- Logs are emitted to container stdout/stderr and viewable via `docker compose logs`.
- Runtime failure causes non-zero container exit status.

## Required Inputs

- Runtime config file path/volume mapping
- Environment values required by application config

## Logging Contract

- Default output MUST be human-readable.
- Optional structured output MUST be supported.
- Configured sensitive fields MUST be redacted.

## Operational Commands

- Start: `docker compose up --build coordinare`
- Follow logs: `docker compose logs -f coordinare`
- Stop: `docker compose down`
