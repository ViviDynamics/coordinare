# Quickstart: Board Orchestrator Daemon

**Feature**: 001-board-orchestrator
**Date**: 2026-02-16

## Prerequisites

- Python 3.12+
- `uv` (recommended) or `pip` for dependency management
- GitHub account with a Projects v2 board
- GitHub Personal Access Token (fine-grained) with permissions: `projects:rw`, `issues:rw`, `pull_requests:rw`, `contents:read`
- SSH access to the agent host
- SMTP credentials for email notifications
- Slack incoming webhook URL

## Setup

### 1. Clone and Install

```bash
git clone <repo-url>
cd coordinare
git checkout 001-board-orchestrator

# Create virtual environment and install dependencies
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. Configuration

Copy the example config and fill in your values:

```bash
cp config.example.yaml config.yaml
```

**config.yaml** (minimal):
```yaml
project_name: "My Project"
github_org: "your-org"
github_project_number: 1
github_token: "ghp_..."
agent_host: "agent.example.com"
agent_user: "coordinare"
agent_command: "claude-code"
human_reviewers:
  - "reviewer1"
  - "reviewer2"
notification_email: "coordinare@vividynamics.com"
smtp_host: "smtp.example.com"
slack_webhook_url: "https://hooks.slack.com/services/..."
slack_channel: "#project-updates"
```

**Environment variable overrides** (FR-015):
```bash
export COORDINARE_GITHUB_TOKEN="ghp_..."
export COORDINARE_SMTP_PASSWORD="secret"
```

Environment variables use the `COORDINARE_` prefix and override config file values.

### 3. Verify GitHub Board Setup

Ensure your GitHub Projects board has these columns (FR-016):
- ToDo / Backlog
- Blocked
- In Progress
- In Review
- Done

### 4. Verify Agent Connectivity

```bash
ssh -i ~/.ssh/id_ed25519 coordinare@agent.example.com "{agent_command} health"
```

Expected response: `{"status": "healthy", ...}`

## Running

### Development (local)

```bash
# Start the coordinare daemon
python -m coordinare

# Or with explicit config path
python -m coordinare --config config.yaml
```

The daemon will:
1. Load configuration (file + env vars)
2. Connect to GitHub Projects and validate board columns
3. Verify agent SSH connectivity
4. Start polling the board at the configured interval
5. Expose health-check endpoint at `http://localhost:8080/health`

### Docker

```bash
docker build -t coordinare .
docker run -d \
  --name coordinare \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -e COORDINARE_GITHUB_TOKEN="ghp_..." \
  -e COORDINARE_SMTP_PASSWORD="secret" \
  -p 8080:8080 \
  coordinare
```

### Docker Compose

```yaml
services:
  coordinare:
    build: .
    volumes:
      - ./config.yaml:/app/config.yaml:ro
    environment:
      COORDINARE_GITHUB_TOKEN: ${GITHUB_TOKEN}
      COORDINARE_SMTP_PASSWORD: ${SMTP_PASSWORD}
    ports:
      - "8080:8080"
    restart: unless-stopped

  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: coordinare
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

## Development Commands

```bash
# Run tests
pytest

# Run tests with coverage
pytest --cov=src/coordinare --cov-report=term-missing

# Type checking
mypy src/

# Linting and formatting
ruff check src/ tests/
ruff format src/ tests/

# Run all quality checks
pytest && mypy src/ && ruff check src/ tests/
```

## Monitoring

### Health Check
```bash
curl http://localhost:8080/health
```

### Metrics (Prometheus)
```bash
curl http://localhost:8080/metrics
```

### Logs
The coordinare emits structured JSON logs (FR-020). Example:
```json
{
  "timestamp": "2026-02-16T12:00:00Z",
  "level": "info",
  "event": "card_transition",
  "card_id": "PVI_...",
  "from_status": "TODO",
  "to_status": "IN_PROGRESS",
  "issue_number": 42
}
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Startup fails with "field not found" | Board missing required columns | Add all 5 columns to GitHub Projects board |
| "Rate limit exceeded" in logs | Polling too aggressively | Increase `poll_interval_seconds` in config |
| Agent dispatch timeout | SSH connectivity issue | Verify SSH key and host with manual `ssh` command |
| Notifications not sending | SMTP/Slack credentials wrong | Check `COORDINARE_SMTP_*` env vars and webhook URL |
| Cards stuck in IN_PROGRESS | Agent not responding | Check agent health: `ssh agent-host "{command} health"` |
