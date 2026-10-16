# Coordinare

An autonomous software development orchestrator for GitHub Project boards.
Coordinare polls a Project for cards and dispatches them through configurable AI
performer roles. It can advance each card from TODO through implementation,
review, security scanning, QA validation and documentation before human PR review.

> **Security**: coordinare executes AI-generated code, holds a GitHub token with
> write access, and feeds public issue and comment text into model prompts. Read
> the **[threat model](docs/security/threat-model.md)** before exposing it to any
> network. The dashboard has no authentication and is loopback-only by design.

## How It Works

1. **Poll**: Coordinare watches a GitHub Project board for cards in the TODO column
2. **Assess**: Each card is evaluated for sufficient context (acceptance criteria, scope)
3. **Dispatch**: The card is dispatched through a sequential lifecycle of performer roles
4. **Monitor**: Each performer is polled until it completes or blocks
5. **Advance**: On success, the card advances to the next role automatically
6. **Review**: After all automated roles complete, the card enters human PR review
7. **Merge**: On human approval, the PR is squash-merged and the card moves to Done

## Performer Lifecycle

Cards progress through configurable roles (unconfigured roles are skipped):

| Role | Stage | What it does | Terminal state |
|------|-------|-------------|---------------|
| Advocate | `advocate` | Answers inbound issues from the documentation, escalates the rest |: |
| Curator | `curator` | Proposes ready issues to the board backlog for a human to promote |: |
| Assessor | `assessing` | Evaluates card sufficiency, asks clarifying questions |: |
| Architect | `architecting` | Analyses codebase, commits a technical plan to the branch | `plan_committed` |
| Implementer | `implementing` | Writes code, opens a PR | `pr_opened` |
| Reviewer | `reviewing` | Reviews the PR, approves or requests changes | `approved` |
| Security | `security` | Scans for vulnerabilities, posts advisory comments | `security_passed` |
| QA | `qa` | Validates acceptance criteria, writes tests | `qa_passed` |
| Tech Writer | `documenting` | Commits CHANGELOG, README updates, docstrings | `docs_committed` |
| Closer | `closing_review` | Final pass: verifies prior feedback was addressed, resolves review threads | `approved` |

When a role finds issues (reviewer requests changes, security finds vulnerabilities, QA fails criteria), the coordinare routes feedback back to the implementer or architect and re-runs the affected roles. The review → implement loop is bounded by `max_feedback_cycles` (default `5`); when exceeded, the card needs a human (it is parked in the Blocked column) with an actionable break-case comment that `@`-mentions `human_reviewers` so they can triage.

## Quick Start

### 1. Prepare

Use Linux or macOS on x86_64 or arm64. On Windows, use WSL. You need `curl`
and either `sha256sum` or `shasum`. The installer supplies Python 3.12 through
[uv](https://docs.astral.sh/uv/) and asks before installing uv if it is missing.

Have these ready:

- A GitHub repository and a [Project board](https://docs.github.com/en/issues/planning-and-tracking-with-projects)
  (V2) owned by the same user or organization. Its Status field needs To Do
  (or Ready), In Progress, In Review, Blocked and Done.
- A GitHub token with access to the repository and Project. Classic PATs need
  `repo`, `project` and `read:org`; authorize organization SSO if required.
  See [token permissions](docs/security/threat-model.md#github-token-permissions)
  for fine-grained permissions and branch protection requirements.
- One harness: Codex (`codex`), Claude Code (`claude`) or OpenCode (`opencode`).
  For local mode, install git and the CLI on PATH and authenticate the CLI. Codex and Claude
  Code can reuse a harness login; OpenCode's native provider requires
  `OPENAI_API_KEY`. You can supply a model API key during setup instead.
  [Docker mode](docs/docker-mode-setup.md) installs the harness in containers
  and requires an API key for native providers.

### 2. Install

The installer downloads release wheels, verifies their checksums and installs
Coordinare and its performer in an isolated user tool environment. It does not
need sudo or a source checkout.

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh | sh
```

To inspect the script before running it:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh
```

Checksums detect corruption. Trust still depends on HTTPS and the release
publisher. Read the [threat model](docs/security/threat-model.md) before granting
repository write access. Agents execute generated code, and public issue text
reaches their prompts. Keep the unauthenticated dashboard on loopback.

Commands are installed in `~/.local/bin`. Add it to this shell's PATH and your
shell profile if needed:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

### 3. Set up and start

Run from a directory without an old `config.yaml`, which would take precedence
when starting without an explicit config path:

```sh
coordinare init
```

Setup asks for your GitHub token, repository, Project number, local or Docker
mode, harness, model, optional endpoint and API key, concurrent cards and human
reviewers. It detects existing credentials and installed harnesses. Secret input
is hidden. Defaults enable only the implementer, one card at a time, with your
GitHub login as the human reviewer. Other roles are optional.

Init writes `~/.config/coordinare/config.yaml` and private
`~/.config/coordinare/coordinare.env` by default. It checks GitHub access, board
columns, harness readiness, model access, writable paths and the dashboard port.
These checks do not generate model output. Fix any reported failures before
starting. See [setup troubleshooting](docs/setup-troubleshooting.md).

After successful preflight, accept `Start at login` to install and start a user
service on macOS or Linux. If you decline, accept `Start Coordinare now` for a
foreground process. Keep that terminal open. If you decline both, start later:

```sh
coordinare
```

The daemon loads `coordinare.env` automatically. Open **<http://127.0.0.1:8090>**,
or the dashboard URL printed by setup. Put an issue on the board in To Do or
Ready to dispatch it. Try the [small first card](docs/first-launch.md#try-one-card)
and review its PR as the configured human reviewer.

To recheck setup, stop the foreground process with Ctrl+C, or stop the user
service first. Doctor checks port availability and can report the running
daemon's port as occupied:

```sh
coordinare doctor
```

See the [full quickstart](docs/quickstart.md), [self-hosted models](docs/quickstart-selfhosted.md),
[guided setup](docs/init.md), [unattended installation](docs/unattended-install.md),
[user services](docs/service.md), [upgrading](docs/upgrading.md),
[uninstalling](docs/uninstalling.md) and [file locations](docs/files-and-paths.md).
For Kubernetes and Helm, use the [chart README](deploy/helm/coordinare/README.md).
For source changes and checkout setup, see
[Developing Coordinare](CONTRIBUTING.md#developing-coordinare).

## Configuration

### MCP Management Layer

An external AI agent can drive the full operator surface (config, symphonies,
personas, lifecycle controls, live state) over MCP: no browser, no shell on the
daemon host. Run `python -m coordinare.mcp` next to the daemon, or set
`mcp.enabled: true` in the Helm chart for a sidecar. See `docs/mcp.md` for the
architecture, security notes, and the env vars, and
`specs/551-mcp-management-layer/quickstart.md` for a walkthrough.

### Config Discovery

Coordinare searches for config in this order (first match wins):

1. `--config <path>` CLI flag
2. `COORDINARE_CONFIG_PATH` environment variable
3. `./config.yaml` in the current directory
4. `$XDG_CONFIG_HOME/coordinare/config.yaml`, defaulting to `~/.config/coordinare/config.yaml`
5. `~/.coordinare/config.yaml` in your home directory (legacy fallback)

Any scalar field can be overridden via environment variable: `COORDINARE_<FIELD_NAME>` (uppercase).

### Branch protection

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
  app (required: the App literally cannot push otherwise)
- The "Allow specified actors to bypass required pull requests" list
  is NOT needed if a human is approving before coordinare merges

If both systems are configured on the same branch, both must allow
the App: GitHub enforces them independently.  When coordinare hits
this, it logs a permanent error `"You're not authorized to push to
this branch"` and surfaces the card as needing a human, with the GitHub error
message in `open_questions` so you can act on it directly.

### Performer Roles

To enable the multi-role lifecycle, add a `performers:` section to config.yaml (roles run in the canonical order shown: unlisted roles are silently skipped):

```yaml
performers:
  assessor:
    backend: opencode
    transport: subprocess
  architect:
    backend: opencode
    transport: subprocess
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
  closer:
    backend: opencode
    transport: subprocess
```

With no `performers:` section, coordinare falls back to implementer-only mode (backward compatible). See `config.example.yaml` for per-role `backend`, `mode` and `transport` overrides. Models are selected through
`modes`, `model_endpoints` and `endpoints`; inline performer `model` is rejected.

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

Notifications ship **off by default** (`channels: []`, `routing: []`) so a freshly copied
config validates without any Slack webhook or SMTP credentials. Opt in by configuring Slack
and/or email alerts for card transitions, blocks, and circuit breaker trips:

```yaml
notifications:
  channels:
    - name: slack-ops
      type: slack
      webhook_url: "${COORDINARE_SLACK_WEBHOOK_URL}"
```

See `config.example.yaml` for the full, commented notification routing reference.

## Developing Coordinare

Maintainer checkout setup, test commands and project layout are in
[CONTRIBUTING.md](CONTRIBUTING.md#developing-coordinare).

## Architecture

Coordinare uses [LangGraph](https://github.com/langchain-ai/langgraph) to orchestrate a state machine that drives each card through the development lifecycle. The performer is a separate process (or container) that speaks a JSON stdin/stdout wire protocol and delegates actual coding work to an AI backend (OpenCode, Claude Code, etc.).

The coordinare and performer communicate via a transport layer (subprocess, SSH, or Kubernetes) and share no in-memory state. Each performer session is ephemeral: the performer clones the repo, does its work, and exits.

The `solo` preset runs the performer as a local subprocess on the daemon's host.
It needs no container runtime and provides no container isolation. For Docker
performers, use [Docker mode](docs/docker-mode-setup.md). For Kubernetes, follow
the [chart README](deploy/helm/coordinare/README.md).

## License

Coordinare is **source-available** software, licensed under the
[Elastic License 2.0](LICENSE). You get the source, you can run and change it,
and one specific commercial use is reserved to us.

**What you may do.** Run, copy, modify, and self-host coordinare free of charge,
including for your own commercial work. Use it at your job, inside your company,
on client projects, on anything you are building. You do not need our permission
and you owe us nothing.

**What you may not do.** Provide coordinare to third parties as a hosted or
managed service, where that service gives its users access to a substantial set
of coordinare's features. This is about providing access to coordinare itself, not
about using coordinare to serve your own customers: building software for a client
with coordinare is fine, while giving that client logins to your coordinare is not.
The restriction applies whether or not money changes hands, so a free login is
still provision of a hosted service. Reselling coordinare is likewise not
permitted. The [LICENSE](LICENSE) governs; this paragraph is a plain-English
summary and does not attempt to settle every edge case.

**Want to offer coordinare to your own customers?** That is a conversation worth
having rather than a wall. Commercial licensing is available, including for
running coordinare on someone else's behalf. Get in touch at
[vividynamics.com/contact](https://vividynamics.com/contact).

**Future versions** of coordinare may be offered under different terms. Whatever
you received under the Elastic License 2.0 stays available to you under those
terms.

## Optional observability export

Set `observability.otlp.endpoint` or `OTEL_EXPORTER_OTLP_ENDPOINT` to export
redacted event history as OTLP logs. Export is off by default and cannot stall
local event storage. Prometheus metrics are enabled by default on the existing
health listener. Set `observability.metrics_enabled: false` to disable `/metrics`;
restrict that port to trusted scrapers.
The [chart guide](deploy/helm/coordinare/README.md#optional-observability-export)
covers standard OTEL settings, protocol dependencies, delivery limits and a
Collector example feeding Loki, Grafana and Prometheus.

## Contributing

Bug reports, feature requests, and feedback are genuinely welcome, and they
inform what gets built. Please
[open an issue](https://github.com/ViviDynamics/coordinare/issues).

Public pull requests are **not accepted**. Coordinare is an autonomous agent
system that holds repository write credentials and executes AI-generated code, so
all implementation happens inside the organization to keep the supply chain
closed. Pull requests from outside the organization are closed unmerged. This is
a deliberate security posture rather than a comment on anyone's code.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full policy, and
[SECURITY.md](SECURITY.md) for how to report a vulnerability privately.
