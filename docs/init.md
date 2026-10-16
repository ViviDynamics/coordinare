# Guided setup

For an ordered unattended procedure, use [setup for coding agents](setup-for-agents.md).
The [answers schema and machine interface](agent-setup-reference.md) document
every answer and the JSON validation/preflight reports.

Install Coordinare, then run:

```sh
coordinare init
```

Setup detects a GitHub token from `COORDINARE_GITHUB_TOKEN`, `GH_TOKEN`,
`GITHUB_TOKEN` or `gh auth token`, and detects the current GitHub git remote.
It verifies token scopes, repository access, project Status columns and the
selected harness without making a model call. Installed harnesses are preferred
as defaults. Every prompt shows its default. Secrets use hidden input; an
existing credential is reused without displaying it.

Local mode requires the performer package and a supported harness CLI on the host.
Docker mode installs the harness inside each performer container.
The starter uses `solo` for local processes or `containers` for Docker performers.
The presets supply model defaults and enable the implementer only. Concurrency
defaults to one and the authenticated GitHub user is the default human reviewer.
Codex and Claude Code can use an existing harness login when no API key is
supplied. OpenCode's native provider requires `OPENAI_API_KEY`. Use `--model`
and `--endpoint` to choose a different model or OpenAI-compatible endpoint.
Docker mode checks the runtime, pulls the installed version's published image,
writes explicit container mounts and runtime secret delivery, and runs a bounded
throwaway container to check harness readiness and GitHub/model reachability. Use
`--skip-smoke` to omit that probe on slow machines. Native providers require a model API
key since host harness login files are not mounted. Self-hosted endpoints can omit it. Use `--image-variant base` or
`full` (default), and `--image-tag VERSION` for a development install with no
published image. See [Docker mode setup](docker-mode-setup.md) for verification,
registry access, networking and upgrades.

Setup writes `config.yaml` and `coordinare.env` under
`$XDG_CONFIG_HOME/coordinare`, defaulting to `~/.config/coordinare`. Secrets and
their backups have mode `0600`. The config contains credential references, and
the dedicated env file loads automatically before validation, doctor and daemon
startup. Exported environment values take precedence. State, run logs and
transcripts live under `$XDG_DATA_HOME/coordinare`, defaulting to
`~/.local/share/coordinare`. Existing checkout configs retain priority.

The shared preflight runs at the end and prints each failure with its fix. A
failure exits with code 1 and retains the written files for repair. Docker provisioning
and smoke failures occur before files are written, preserving any previous setup. Successful
setup prints the start command and dashboard URL and offers to start at login.
Accepting calls the shared service installer and starts the background daemon.
Declining offers a foreground start. Pass `--start` for foreground startup or
`--start-at-login` for service installation without the final prompts. These
flags are mutually exclusive. See [user services](service.md).

## Setup from an agent or CI

No terminal means no prompts. Use `--non-interactive` to require this behavior
even in a terminal. Every answer has a flag and a `COORDINARE_INIT_*` environment
variable. Flags override environment answers, which override the answers file.
Saved values and detected defaults fill the rest. Missing required answers are
reported together.

```sh
export GH_TOKEN=your_token
export OPENAI_API_KEY=your_key
coordinare init --non-interactive \
  --repository your-org/your-repo --project-number 1 --harness codex
```

Or supply a YAML mapping with `coordinare init --answers answers.yaml`:

```yaml
repository: your-org/your-repo
project_number: 1
harness: codex
max_concurrent_cards: 1
human_reviewers: [your-github-login]
run_mode: local
```

Optional answer keys are `github_token`, `api_key`, `model`, `endpoint`,
`image_variant` and `image_tag`.
Keep an answers file containing credentials private. Environment credentials
avoid putting secrets on the command line or in shell history. Equivalent env
answers include `COORDINARE_INIT_REPOSITORY`, `COORDINARE_INIT_PROJECT_NUMBER`,
`COORDINARE_INIT_HARNESS` and `COORDINARE_INIT_RUN_MODE`.

Use `--config-dir /absolute/path` for a different destination; the printed start
command includes its explicit config path. Existing config or env files require
`--force`. Forced reruns use saved answers as defaults, keep unrelated settings,
and create `.bak` copies, adding another `.bak` suffix on subsequent reruns.
