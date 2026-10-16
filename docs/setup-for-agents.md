# Set up Coordinare with a coding agent

Follow these steps in order. Use a release containing issue 201. This procedure
installs a per-user daemon and Docker performers, with one implementer and one
card at a time. It does not enable additional automated reviewer roles. Read
[the config reference](../config.example.yaml) before adding advanced settings.

## 1. Obtain facts and authorization

Ask the human for these facts together. Never ask them to paste credentials into
chat. Have them supply credentials through a private environment or secret store.

| Fact | Required value |
| --- | --- |
| Repository | GitHub `owner/name`, with permission to open branches and PRs |
| Board | Projects V2 number belonging to that owner, not a node id or repository issue number |
| Harness | `codex`, `claude_code` or `opencode` |
| Reviewers | Nonempty list of human GitHub logins responsible for reviewing PRs |
| GitHub token | Already authorized credential in `COORDINARE_INIT_GITHUB_TOKEN` or `GH_TOKEN` |
| Model access | For native Docker providers, `OPENAI_API_KEY` for Codex/OpenCode or `ANTHROPIC_API_KEY` for Claude Code; alternatively an approved OpenAI-compatible endpoint and model id |
| Host and release | Linux or macOS, x86_64 or arm64, and approved release tag or permission to use latest; use WSL on Windows |

The board Status field must contain To Do (or Ready), In Progress, In Review,
Blocked and Done. Classic token scopes checked by setup are `repo`, `project`
and `read:org`, with organization SSO authorization where required. Fine-grained
tokens and Apps need repository Contents, Issues and Pull requests read/write
and organization Projects read/write. Init requires a user identity from `/user`;
use a user token for this procedure. App installation credentials require advanced
configuration. Read probes cannot prove every write permission.

Never print, log, commit, or paste secrets into chat, shell history, command
arguments, config YAML or the answers file. Disable shell tracing and do not run
`env`, `printenv`, `gh auth token`, or display `coordinare.env`. Never widen token
scopes automatically. Stop and explain the required permission to the human.
Keep `dashboard_host: 127.0.0.1`; never expose it with a public bind, port forward
or tunnel. Do not change host security policy to make a check pass.

## 2. Install and verify prerequisites

Use a normal user account. Ensure `curl`, `sha256sum` or `shasum`, and a running
Docker runtime are installed using the host's supported installation method.
Docker installation and OS privileges require the human's authorization. Obtain
authorization to install uv if missing before running the installer. Check that
`docker info` exits 0 before proceeding. Python and uv need not be installed;
the release installer supplies an isolated Python 3.12 environment and bundled
performer. No repository checkout is needed.

```sh
set +x
umask 077
install_tmp=$(mktemp -d)
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o "$install_tmp/install.sh"
sh "$install_tmp/install.sh" --yes --no-init
export PATH="$HOME/.local/bin:$PATH"
coordinare --version
command -v performer
rm -rf "$install_tmp"
```

Run each command only after the previous one succeeds. All must exit 0. Review
the downloaded script before running it if required by your environment. For a
pinned install, download `releases/download/TAG/install.sh` instead and pass
`--version TAG`. `--yes` also consents to installing uv if absent; the agent
needs the human's authorization for that installation. `--no-init` is
accepted, but init is always a separate explicit step. The installer checks
release wheel checksums and does not start the daemon or edit shell profiles.
See [installer options](releases.md) for environment equivalents and removal.

## 3. Write nonsecret answers and initialize

Create a private working directory outside all repositories. Copy the
[filled answers example](examples/init-answers.yaml) there and edit its facts.
Replace `your-github-login` with the human reviewer's GitHub login, and use the
repository, board and other facts supplied by the human.
For Claude Code select `harness: claude_code` and `model: claude-sonnet-4-6`.
The example selects Docker, Codex and its default native model. For a self-hosted
model set `endpoint` to its approved OpenAI-compatible base URL and `model` to
the served model id. Container localhost is the container itself; a host service
needs a container-reachable address such as `host.docker.internal`. Never embed
credentials in URLs. Supply any API key privately through `COORDINARE_INIT_API_KEY`.

The [answers schema](schemas/init-answers.schema.json) validates the YAML mapping
after parsing. Every key is optional in the schema because other sources can
supply it. Unattended init must ultimately resolve a token, repository and board.
Use the explicit harness and reviewers from the human instead of detected defaults.
See [all answers, flags and environment variables](agent-setup-reference.md).

```sh
setup_dir="${XDG_CONFIG_HOME:-$HOME/.config}/coordinare"
coordinare init --non-interactive --answers ./answers.yaml --config-dir "$setup_dir"
```

Use an absolute `XDG_CONFIG_HOME` if set. Never pass `--github-token` or `--api-key`.
The operator must already have supplied private environment credentials. Init
verifies GitHub access, board columns, runtime, release image and container
connectivity without generating model output. It pulls the installed release's
`full` image by default; budget roughly 10 GiB, or choose `base` for roughly
4 GiB without Chromium and extra security/test tools. Do not use `--skip-smoke`
for installation verification.

Init exits 0 on success, 1 on setup or preflight failure, and 2 for invalid CLI
arguments. It writes `config.yaml` and `coordinare.env` at the selected location.
The env file and backups are private mode 0600. Do not display them. Values are
loaded automatically; there is no need to source a checkout `.env`. Exported
values take precedence over saved env values. Default state and logs live under
`${XDG_DATA_HOME:-$HOME/.local/share}/coordinare`.

On a rerun, existing files require `--force`; it keeps `.bak` backups and preserves
unrelated settings. Init failures name the stage and a safe cause, followed by a
`Fix:` line. Library exception messages and Docker stderr are withheld to protect
credentials. Repair the reported cause first, then repeat with the same answers
and `--force`. For `container_smoke` with `ENOSPC`, free space on the Docker
filesystem; inspect usage with `docker system df`. Docker provisioning failures
occur before setup files are written; final preflight failures retain files for
repair. Do not start a service until the next step passes.

## 4. Verify with machine-readable checks

```sh
coordinare validate --config "$setup_dir/config.yaml" --json
coordinare doctor --config "$setup_dir/config.yaml" --json
```

Both exit 0 when no blocking check fails and 1 otherwise. Exit 2 means argparse
rejected the invocation and produces normal usage text. Successful parsing of
`--json` produces one JSON document, including config load failures. The report
schema, stable ids, status semantics, limits and fixes are documented in
[the machine interface](agent-setup-reference.md#json-reports).

Read `checks`, apply each `fix` within the human's authorization, and rerun both
commands. Do not retry deterministic failures without changing their cause.
Transient network warnings follow bounded retries: restore connectivity and
retry later. For route or model warnings, correct the named setting; ask the
human about unverified permissions. `ok: true` means no
blocking failures; it does not mean warnings are verified. `doctor.endpoint`
probes `GET {base_url}/models` for OpenAI-compatible APIs. Ollama uses
`GET {base_url}/api/tags`, or `/models` when its base URL ends in `/v1`.
2xx is `ok`; a reachable non-2xx response is `warn`
and names the expected route. The base URL usually ends in `/v1`. Connection
failures retain their existing status. `doctor.model` attempts model enumeration
for OpenAI-compatible endpoints, including LiteLLM, vLLM and Ollama: a listed id
is `ok`, a missing id is `warn` with a short list of served ids (and an
`ollama pull <model>` fix for Ollama), and unavailable
enumeration is `skip` (for example, a non-2xx response, an unreadable listing or
required authentication without an API key). Listing models generates no model
output. Require no unresolved connectivity or credential warnings before
calling the install verified. Doctor does not spend model tokens or prove output
quality. Docker doctor starts and removes temporary harness containers.

Use the explicit config path throughout so a checkout `config.yaml` cannot win
discovery. Doctor checks port availability; run it before the daemon. If checking
an already running install, stop its service before doctor and restart afterward.
Do not kill an unrelated process just to free the dashboard port.

## 5. Start and hand off

After passing checks, with authorization to start processing the board:

```sh
coordinare service install --config "$setup_dir/config.yaml"
coordinare service status
```

Install starts immediately and registers start at login. macOS needs a graphical
login session; Linux needs a systemd user session. Status exits 0 when it can
report status, including a stopped service, so inspect its running state. If a
user service is unavailable, use `coordinare --config "$setup_dir/config.yaml"`
in the foreground. The dashboard is `http://127.0.0.1:8090` by default. See
[service management](service.md) for stop, start, restart, logs and uninstall.

Tell the human the installed version, config path, check ids/statuses, unresolved
warnings and running state. Never include credentials or raw service logs. Offer
the [first card procedure](first-launch.md#try-one-card) as a separate authorized
test; it makes GitHub writes and consumes model budget.
