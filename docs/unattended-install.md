# Unattended installation

Use the [quickstart prerequisites](quickstart.md#prepare). Download and inspect
the script before using it in automation:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh --yes --no-init
export PATH="$HOME/.local/bin:$PATH"
```

`--yes` supplies consent to install uv without a terminal. The installer always
leaves init as an explicit next step. For a fixed release, add `--version` as
shown in the [release guide](releases.md).

Save this YAML mapping as `answers.yaml`, replacing the example values:

```yaml
repository: your-org/your-repo
project_number: 1
harness: codex
run_mode: local
max_concurrent_cards: 1
human_reviewers: [your-github-login]
```

Provide GitHub and model credentials through your automation's secret store as
`COORDINARE_INIT_GITHUB_TOKEN` and `OPENAI_API_KEY` for this example. Claude Code
uses `ANTHROPIC_API_KEY`. Local Codex and Claude Code can instead use an existing
harness login. You can also use the answer keys `github_token` and `api_key` in
a private file, but never commit it. Protect the answers file before use:

```sh
chmod 600 answers.yaml
coordinare init --non-interactive --answers answers.yaml
coordinare config validate
coordinare doctor
```

These commands save and check setup without starting the daemon. To start in
the foreground, add `--start` to init. To install and start a login service, use
`--start-at-login` instead; a supported login session is required. The two flags
are mutually exclusive. Without a terminal, init is already noninteractive;
`--non-interactive` also guarantees this in a terminal.

All setup answers have a flag and `COORDINARE_INIT_*` variable. Precedence is
flags, then answer environment variables, then the answers file, then saved or
detected defaults. Optional keys include `model`, `endpoint`, `api_key`,
`github_token`, `image_variant` and `image_tag`. Never pass credentials as flags:
they can appear in shell history and process listings.

For Docker performers, set `run_mode: docker`. Native providers need a model
API key, since host harness login files are not mounted. See
[Docker setup](docker-mode-setup.md) for image and endpoint requirements.
Use `--config-dir /absolute/path` to select a destination. When using that option,
pass the generated config path to validation, doctor and daemon startup too.
Existing setup files require `--force`, which creates private `.bak` backups.
