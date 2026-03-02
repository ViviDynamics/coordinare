# coordinare Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-02-16

## Active Technologies
- Python 3.12+ + `stamina>=24.2.0` (NEW — add to pyproject.toml), `tenacity` (already installed via langgraph), `prometheus-client>=0.21`, `structlog>=24.1`, `pydantic>=2.9`, `pydantic-settings>=2.6` (005-resilience)
- N/A — circuit breaker state is in-memory, resets on restart (per spec assumption) (005-resilience)
- Python 3.12+ + `httpx` (Slack webhook delivery — already present, not `slack-sdk`), `aiosmtplib` (email delivery — already present), `pydantic` + `pydantic-settings` (config model), `structlog` (structured logging), `prometheus-client` (metrics) — all existing; **no new dependencies required** (006-notification-alerting)
- In-memory only (`NotificationHistory` as a plain list on `NotificationService`; lazy time-based eviction). No DB. (006-notification-alerting)
- Python 3.12+ + `pydantic-settings` (existing), `pydantic` v2 (existing), `structlog` (existing), `argparse` (stdlib — existing) (008-config-management)
- N/A (config is loaded from YAML file or env vars; no persistence) (008-config-management)
- Python 3.12+ + `structlog` (contextvars), `prometheus-client` (metrics), `fastapi` (health server), `pydantic-settings` (config extensions) — all existing; no new dependencies (009-metrics-observability)
- N/A (metrics held in prometheus-client registry; health state held in-memory `HealthRegistry`; no persistence) (009-metrics-observability)

- Python 3.12+ + LangGraph (>=0.2), anthropic (>=0.40), gql[aiohttp], FastAPI, structlog, slack-sdk, aiosmtplib, prometheus-client, pydantic-settings (001-board-orchestrator)
- Python 3.12+ + Pydantic v2 (existing), `asyncio.create_subprocess_exec` (stdlib), `structlog` (existing); `asyncssh` removed — replaced by pluggable transport architecture (004-agent-protocol)

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
- 009-metrics-observability: Added Python 3.12+ + `structlog` (contextvars), `prometheus-client` (metrics), `fastapi` (health server), `pydantic-settings` (config extensions) — all existing; no new dependencies
- 005-resilience: Added Python 3.12+ + `stamina>=24.2.0` (NEW — add to pyproject.toml), `tenacity` (already installed via langgraph), `prometheus-client>=0.21`, `structlog>=24.1`, `pydantic>=2.9`, `pydantic-settings>=2.6`
- 004-agent-protocol: Replaced `AgentSSHService` with pluggable transport architecture (`SubprocessTransport` + `AgentService`); removed `asyncssh` dependency; added `src/coordinare/protocol.py`, `src/coordinare/transport/`, `src/coordinare/services/agent_service.py`; `check_status(card_id)` renamed to `check_status(session_id)`; config fields: `agent_transport`, `agent_executable`, `transport_timeout_seconds`


<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
