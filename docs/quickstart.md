# Quickstart: install, set up, dispatch a card

Coordinare runs agents with repository write credentials. Read the
[threat model](security/threat-model.md) before using it on a repository you care
about. Public issue and comment text reaches model prompts. The dashboard has
no authentication; keep it on loopback. Review generated changes yourself.

## Prepare

Use Linux or macOS on x86_64 or arm64, or WSL on Windows. Have `curl` and
`sha256sum` or `shasum` available. You do not need Python or uv installed:
the installer supplies Python 3.12 and asks before installing uv.

Create a GitHub repository and a Projects V2 board under the same owner. The
board's Status field needs To Do (or Ready), In Progress, In Review, Blocked and
Done. Have a token ready. Classic PATs need `repo`, `project` and `read:org`, plus
organization SSO authorization where required. See
[token permissions](security/threat-model.md#github-token-permissions) for
fine-grained permissions. Preflight checks read access and classic scopes;
GitHub enforces write permissions and branch protection when writes happen.

Choose one starter harness: `codex`, `claude_code` or `opencode`. Local mode needs
git and the corresponding `codex`, `claude` or `opencode` CLI installed on PATH. Codex and
Claude Code can use a harness login or API key. OpenCode's native provider needs
`OPENAI_API_KEY`. [Docker mode](docker-mode-setup.md) installs the harness inside
performer containers and requires a model API key for native providers.
For your own model server, follow [self-hosted setup](quickstart-selfhosted.md).

## Install

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh | sh
```

Or download, inspect, then run:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh
```

The installer verifies all five release wheels against `SHA256SUMS` before
installing them. Checksums detect corruption; HTTPS and the release publisher
establish trust. Coordinare and performer share an isolated uv tool environment.
The installer preserves config and state and leaves initialization to you.

If needed, add the commands to PATH in this shell and your shell profile:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

## Configure and start

Use a directory without an old `config.yaml` so later config discovery selects
the generated user configuration.

```sh
coordinare init
```

Setup detects tokens from `COORDINARE_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`
or `gh auth token`. It can detect a GitHub repository from the current git remote;
a checkout is optional. It verifies repository access and lets you select a
Project. It asks for run mode, harness, model, optional endpoint and API key,
concurrency and human reviewer logins. Docker mode also asks for an image variant.

The `solo` and `containers` presets both enable only implementer and default to
one concurrent card. Your authenticated GitHub login is the default reviewer.
No Slack or email setup is required. Add roles and notifications later using the
[full configuration reference](../config.example.yaml).

Setup writes configuration and a private `coordinare.env`, runs preflight, then
prints a start command and dashboard URL. After successful preflight, accept
`Start at login` to start a background user service. If you decline, accept
`Start Coordinare now` for a foreground process. Keep that terminal open.
To start later:

```sh
coordinare
```

Open **<http://127.0.0.1:8090>**, or the URL printed by init. For a custom
`--config-dir`, use the explicit `--config` command printed by init.
Generated secrets load automatically; do not source a checkout `.env`.

If preflight fails, fix the named problem. Files written before host preflight
are retained. Docker provisioning and smoke failures happen before setup files
are written. To check again, first stop any running daemon so its dashboard port
is free, then run:

```sh
coordinare config validate
coordinare doctor
```

Doctor checks configured endpoints, models, GitHub, harnesses, paths and port.
Metadata probes do not generate model output or prove model quality. Warnings
can allow startup when connectivity cannot be verified; deterministic failures
return a nonzero exit code. See [first launch](first-launch.md) for preflight
behavior and [troubleshooting](setup-troubleshooting.md) for setup failures.

## Dispatch a card

Add an issue to the board in To Do or Ready. Use a clear title and acceptance
criteria, or copy the [first card](first-launch.md#try-one-card). Watch the
activity feed, inspect the PR and review it as a configured human reviewer.
Unconfigured roles are skipped. Required checks and approval gates still apply.

## Next steps

- [Run at login and view service logs](service.md)
- [Docker performers](docker-mode-setup.md)
- [Unattended installation and answers files](unattended-install.md)
- [Upgrade](upgrading.md), [uninstall](uninstalling.md), [file locations](files-and-paths.md)
- [Kubernetes and Helm](../deploy/helm/coordinare/README.md)
- [Developing Coordinare from a checkout](../CONTRIBUTING.md#developing-coordinare)
