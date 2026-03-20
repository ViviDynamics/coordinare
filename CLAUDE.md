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
- Python 3.12+ + FastAPI + Starlette (already in `pyproject.toml`) — `StreamingResponse`, `HTMLResponse`, middleware; `asyncio`, `json`, `collections.deque`, `datetime`, `uuid` (stdlib); `structlog` (already present) (010-web-dashboard)
- In-memory only (`collections.deque(maxlen=20)` for cycle history; resets on daemon restart) (010-web-dashboard)
- Python 3.12+ + `asyncio` (stdlib), `unicodedata` (stdlib), `re` (stdlib), `tempfile` (stdlib), `shutil` (stdlib), `os` + `stat` (stdlib), `pathlib` (stdlib), `structlog` (existing), `pydantic` + `pydantic-settings` (existing) (011-agent-workspace)
- Ephemeral temp directories under `workspace_root` (default: system temp dir) (011-agent-workspace)
- Python 3.12+ + `pydantic>=2.9`, `httpx>=0.27`, `psutil>=5.9`, `structlog>=24.1` (new to performer package; all already in coordinare root) (012-performer)
- None — all state is in-memory; temporary git workspace in OS temp dir (`tempfile.mkdtemp`) (012-performer)
- Python 3.12+ + PyJWT[crypto]>=2.8 (new), httpx (existing), FastAPI (existing), pydantic-settings (existing), structlog (existing) (015-github-app-auth)
- N/A — token cached in-memory; resets on restart (015-github-app-auth)
- Python 3.12+ + FastAPI + Starlette (existing), asyncio stdlib, vanilla JavaScript (inline in dashboard HTML) (016-force-poll)
- N/A — trigger is ephemeral; no persistence required (016-force-poll)
- Python 3.12+ + structlog (existing), asyncio (stdlib) — no new dependencies required (017-fix-post-pr-workflow)
- N/A — in-memory state updates only; StateStore already persists all modified fields (017-fix-post-pr-workflow)
- Python 3.12+ + `pydantic>=2.9`, `pydantic-settings>=2.6`, `structlog>=24.1`, FastAPI (existing) — **no new dependencies required** (018-performer-personas)
- Existing `config.yaml` YAML file — extended with a `personas:` top-level key; no new persistence backend (018-performer-personas)
- Python 3.12+ + LangGraph ≥ 0.2 (existing), pydantic-settings (existing), structlog (existing), asyncio (stdlib) — **no new dependencies required** (019-performer-lifecycle)
- N/A — `CoordinareState` is in-memory; `performers:` key added to existing `config.yaml` (019-performer-lifecycle)

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
- 019-performer-lifecycle: Added Python 3.12+ + LangGraph ≥ 0.2 (existing), pydantic-settings (existing), structlog (existing), asyncio (stdlib) — **no new dependencies required**
- 018-performer-personas: Added Python 3.12+ + `pydantic>=2.9`, `pydantic-settings>=2.6`, `structlog>=24.1`, FastAPI (existing) — **no new dependencies required**
- 017-fix-post-pr-workflow: Added Python 3.12+ + structlog (existing), asyncio (stdlib) — no new dependencies required


<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
