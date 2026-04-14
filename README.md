# Coordinare

An autonomous software development orchestrator that manages a full-cycle development workflow on GitHub Project boards. Coordinare polls a GitHub Project for cards, dispatches them through a configurable sequence of AI-powered performer roles, and advances each card from TODO through implementation, review, security scanning, QA validation, and documentation — all before a human sees the PR.

## How It Works

1. **Poll** — Coordinare watches a GitHub Project board for cards in the TODO column
2. **Assess** — Each card is evaluated for sufficient context (acceptance criteria, scope)
3. **Dispatch** — The card is dispatched through a sequential lifecycle of performer roles
4. **Monitor** — Each performer is polled until it completes or blocks
5. **Advance** — On success, the card advances to the next role automatically
6. **Review** — After all automated roles complete, the card enters human PR review
7. **Merge** — On human approval, the PR is squash-merged and the card moves to Done

## Performer Lifecycle

Cards progress through up to 8 configurable roles (unconfigured roles are skipped):

| Role | Stage | What it does | Terminal state |
|------|-------|-------------|---------------|
| Advocate | `advocate` | Scans issues, adds high-value cards to the board | — |
| Assessor | `assessing` | Evaluates card sufficiency, asks clarifying questions | — |
| Architect | `architecting` | Analyses codebase, commits a technical plan to the branch | `plan_committed` |
| Implementer | `implementing` | Writes code, opens a PR | `pr_opened` |
| Reviewer | `reviewing` | Reviews the PR, approves or requests changes | `approved` |
| Security | `security` | Scans for vulnerabilities, posts advisory comments | `security_passed` |
| QA | `qa` | Validates acceptance criteria, writes tests | `qa_passed` |
| Tech Writer | `documenting` | Commits CHANGELOG, README updates, docstrings | `docs_committed` |

When a role finds issues (reviewer requests changes, security finds vulnerabilities, QA fails criteria), the coordinare routes feedback back to the implementer or architect and re-runs the affected roles.

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- A GitHub repository with a [Project board](https://docs.github.com/en/issues/planning-and-tracking-with-projects) (V2)
- A GitHub token with `project`, `repo`, and `read:org` scopes

### 1. Clone and install

```bash
git clone https://github.com/ViviDynamics/coordinare.git
cd coordinare
uv sync
```

### 2. Configure

Copy the example config and fill in your values:

```bash
cp config.example.yaml config.yaml
```

Required fields:
- `project_name` — display name for your project
- `github_org` — your GitHub organisation
- `github_project_number` — the V2 Project number (visible in the project URL)
- `github_token` — a GitHub token (or use `COORDINARE_GITHUB_TOKEN` env var)
- `human_reviewers` — list of GitHub logins who will approve PRs

### 3. Set up environment

```bash
cp .env.example .env
# Edit .env with your secrets:
#   COORDINARE_GITHUB_TOKEN=ghp_...
#   ANTHROPIC_API_KEY=sk-ant-...  (for AI-powered assessment)
```

### 4. Authorize the GitHub App (or PAT) on branch protection

For coordinare to squash-merge PRs on your behalf, the identity it uses
(GitHub App recommended; PAT supported) must be allowed to push to the
protected branch.  Live testing has repeatedly surfaced this gotcha:
**rulesets and classic branch protection rules are TWO separate
systems** on the same branch, and the App has to be authorised in
whichever one is active (or both, if both exist).

**If you use a Ruleset** (`Settings → Rules → Rulesets`):
- Edit the ruleset covering `main`
- **Bypass list** → add the coordinare GitHub App (search by app name)

**If you use classic Branch Protection** (`Settings → Branches`):
- Edit the rule for `main`
- **"Restrict who can push to matching branches"** → add the coordinare
  app (required — the App literally cannot push otherwise)
- The "Allow specified actors to bypass required pull requests" list
  is NOT needed if a human is approving before coordinare merges

If both systems are configured on the same branch, both must allow
the App — GitHub enforces them independently.  When coordinare hits
this, it logs a permanent error `"You're not authorized to push to
this branch"` and surfaces the card as blocked with the GitHub error
message in `open_questions` so you can act on it directly.

### 5. Validate config

```bash
.venv/bin/python -m coordinare config validate
```

### 6. Run

```bash
# Local (no Docker)
bin/run-coordinare

# Or with Docker Compose
docker-compose up
```

### 7. Monitor

- **Dashboard**: http://localhost:8090 — live SSE dashboard with phase, card, health, and performer status
- **Health**: http://localhost:8080/health — JSON health check for load balancers
- **Logs**: Structured JSON logs (set `log_level: debug` for verbose output)

## Configuration

### Config Discovery

Coordinare searches for config in this order (first match wins):

1. `--config <path>` CLI flag
2. `COORDINARE_CONFIG_PATH` environment variable
3. `./config.yaml` in the current directory
4. `~/.coordinare/config.yaml` in your home directory

Any scalar field can be overridden via environment variable: `COORDINARE_<FIELD_NAME>` (uppercase).

### Performer Roles

To enable the multi-role lifecycle, add a `performers:` section to config.yaml:

```yaml
performers:
  implementer:
    backend: opencode
    transport: subprocess
  reviewer:
    backend: opencode
    transport: subprocess
  security:
    backend: opencode
    transport: subprocess
  qa:
    backend: opencode
    transport: subprocess
  tech_writer:
    backend: opencode
    transport: subprocess
```

Roles not listed are silently skipped. With no `performers:` section, coordinare falls back to implementer-only mode (backward compatible).

### Persona Instructions

Customise per-role AI instructions via `personas:` in config.yaml or the dashboard API:

```yaml
personas:
  implementer:
    instructions: "Always write tests first. Use type annotations."
  reviewer:
    instructions: "Focus on correctness and test coverage."
```

### Notifications

Configure Slack and email alerts for card transitions, blocks, and circuit breaker trips:

```yaml
notifications:
  channels:
    - name: slack-ops
      type: slack
      webhook_url: "${COORDINARE_SLACK_WEBHOOK_URL}"
```

See `config.example.yaml` for the full notification routing configuration.

## Development

### Run tests

```bash
bin/build              # Lint + unit tests + coverage + performer tests
bin/build --e2e        # Include Playwright browser tests
bin/build --all        # Everything including Docker image builds
```

### Project structure

```
src/coordinare/           # Coordinare daemon
  graph/                 # LangGraph state machine
    nodes/               # Graph nodes (dispatch, monitor, classify, etc.)
  services/              # GitHub, Claude, notification, persona services
  dashboard.py           # Live web dashboard (SSE)

agent/performer/         # Performer container
  src/performer/         # Wire protocol, backend adapters, git workspace
  tests/                 # Performer unit tests

specs/                   # Feature specifications (spec, plan, tasks for each feature)
tests/                   # Coordinare tests (unit, integration, contract, e2e)
```

### Key commands

| Command | Description |
|---------|-------------|
| `bin/run-coordinare` | Start the coordinare daemon |
| `bin/run-performer` | Run the performer locally (no Docker) |
| `bin/build` | Run full test suite and linter |
| `bin/check-agent` | Check performer health |
| `bin/performer-logs` | Stream performer stderr logs |
| `.venv/bin/python -m coordinare config validate` | Validate config |

## Architecture

Coordinare uses [LangGraph](https://github.com/langchain-ai/langgraph) to orchestrate a state machine that drives each card through the development lifecycle. The performer is a separate process (or container) that speaks a JSON stdin/stdout wire protocol and delegates actual coding work to an AI backend (OpenCode, Claude Code, etc.).

The coordinare and performer communicate via a transport layer (subprocess, SSH, or Kubernetes) and share no in-memory state. Each performer session is ephemeral — the performer clones the repo, does its work, and exits.

## License

See [LICENSE](LICENSE) for details.
