# coordinare Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-02-16

## Active Technologies
- Python 3.12+ + `httpx` (Slack webhook delivery — already present, not `slack-sdk`), `aiosmtplib` (email delivery — already present), `pydantic` + `pydantic-settings` (config model), `structlog` (structured logging), `prometheus-client` (metrics) — all existing; **no new dependencies required** (006-notification-alerting)
- In-memory only (`NotificationHistory` as a plain list on `NotificationService`; lazy time-based eviction). No DB. (006-notification-alerting)
- Python 3.12+ + `pydantic-settings` (existing), `pydantic` v2 (existing), `structlog` (existing), `argparse` (stdlib — existing) (008-config-management)
- N/A (config is loaded from YAML file or env vars; no persistence) (008-config-management)

- Python 3.12+ + LangGraph (>=0.2), anthropic (>=0.40), gql[aiohttp], asyncssh, FastAPI, structlog, slack-sdk, aiosmtplib, prometheus-client, pydantic-settings (001-board-orchestrator)

## Project Structure

```text
src/
tests/
```

## Commands

cd src [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] pytest [ONLY COMMANDS FOR ACTIVE TECHNOLOGIES][ONLY COMMANDS FOR ACTIVE TECHNOLOGIES] ruff check .

## Code Style

Python 3.12+: Follow standard conventions

## Recent Changes
- 009-metrics-observability: No new dependencies — extends existing structlog (contextvars), prometheus-client (new metric families), fastapi (new /live route), pydantic-settings (two new config fields); adds docs/dashboards/ and docs/runbooks/ directories
- 008-config-management: Added Python 3.12+ + `pydantic-settings` (existing), `pydantic` v2 (existing), `structlog` (existing), `argparse` (stdlib — existing)
- 006-notification-alerting: Added Python 3.12+ + `httpx` (Slack webhook delivery — already present, not `slack-sdk`), `aiosmtplib` (email delivery — already present), `pydantic` + `pydantic-settings` (config model), `structlog` (structured logging), `prometheus-client` (metrics) — all existing; **no new dependencies required**


<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
