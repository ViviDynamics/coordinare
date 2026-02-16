# Quickstart: Run Coordinare via Shell Script or Docker Compose

**Feature**: 002-docker-cli-output  
**Date**: 2026-02-16

## Prerequisites

- Python 3.12+
- Docker + Docker Compose plugin
- Access to required runtime credentials for coordinare

## 1. Prepare Runtime Configuration

Use committed templates as starting points:

```bash
cp config.example.yaml config.yaml
cp .env.example .env
```

Populate local values in `config.yaml` and `.env`.

Notes:
- Local config instances should remain untracked by git.
- Local config instances should be excluded from Docker build context.

## 2. Run Without Docker (Shell Script)

```bash
scripts/run-coordinare.sh --config config.yaml
```

Optional controls:

```bash
scripts/run-coordinare.sh --config config.yaml --log-level debug
scripts/run-coordinare.sh --config config.yaml --structured-output
```

Expected results:
- startup status visible in console
- major events + heartbeats at default level
- non-zero exit status on runtime errors

## 3. Run With Docker Compose

```bash
docker compose up --build coordinare
```

Follow logs:

```bash
docker compose logs -f coordinare
```

Stop services:

```bash
docker compose down
```

Expected results:
- equivalent runtime output semantics to shell mode
- non-zero container exit on runtime errors

## 4. Verify Output Behavior

In both run modes, verify:
- startup feedback appears promptly
- major activity and heartbeat events are visible
- debug level increases granularity
- known sensitive fields are redacted
