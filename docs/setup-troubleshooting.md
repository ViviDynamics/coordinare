# Setup troubleshooting

Start with the message printed by the installer or init. The quoted text below
comes from the shipped code. Do not paste tokens into issue reports.

## Installer

| Message | Action |
|---|---|
| `Installer error: curl is required. Install curl, then rerun this installer.` | Install curl through your operating system's package manager. |
| `Installer error: Checksum verification requires sha256sum or shasum.` | Install one of these checksum tools and retry. |
| `Installer error: Supported systems are Linux and macOS. On Windows, use WSL and run this installer there.` | Run the installer inside WSL on Windows. |
| `Installer error: No terminal for uv consent. Rerun with --yes or COORDINARE_YES=1, or install uv yourself.` | Supply consent explicitly in automation or install uv first. |
| `Installer error: Download failed. Check connectivity and the requested release assets.` | Check HTTPS connectivity and that the selected release contains its assets. |
| `Installer error: Invalid checksum manifest: malformed, duplicate or unsafe entry.` | Stop and download from a trusted release again. Do not bypass verification. |
| `Installer error: uv tool installation failed. Resolve the uv error above and rerun.` | Read uv's preceding error, fix the reported dependency or environment problem and retry. |

A checksum mismatch also stops installation before any tool is installed. Check
that the wheels and manifest came from the same trusted release.
If the shell cannot find `coordinare`, add the installed command directory:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Add the same line to your shell profile. The installer does not edit it.

## Guided setup

| Message | Action |
|---|---|
| `github_token is required. Set GH_TOKEN or log in with gh auth login.` | Supply a GitHub token at the hidden prompt or through the environment. |
| `repository must be owner/name. Supply --repository or run inside its git checkout.` | Enter the repository as owner/name. A checkout is optional. |
| `No open Projects found. Create a Projects V2 board with lifecycle Status columns.` | Create a Project under the repository owner with To Do or Ready, In Progress, In Review, Blocked and Done. |
| `Cannot list Projects. Grant project scope and authorize the token for organization SSO.` | Grant Projects access and complete any required SSO authorization. |
| `Choose a supported starter harness: codex, claude_code or opencode.` | Use one of those exact setup names; the Claude Code executable is `claude`. |
| `Setup files already exist. Use --force to keep backups and replace them.` | Review the existing files, then rerun with `--force` if replacement is intended. |
| `This configuration uses advanced orchestration. Edit it with coordinare config; init supports starter presets.` | Keep the advanced file and edit it yourself. `coordinare config` shows expanded settings; it is not an editor. |
| `This terminal cannot hide secret input. Supply credentials through environment variables or a private --answers file.` | Use a private answers file or secret environment variables. |
| `Setup failed. Check answers, file permissions and GitHub connectivity; no credential values are displayed.` | Check the answers mapping, destination permissions and network access. Credential values are deliberately omitted. |

Without a terminal, missing required answers are reported together. Use
[unattended setup](unattended-install.md). A missing local harness must be
installed and available on PATH. The installer already bundles the performer;
if its protocol check fails, verify PATH or reinstall the release.

In Docker mode, native model providers require an API key because host harness
logins are not mounted. If Docker's socket is inaccessible, grant your user
access or choose a usable Docker context. If the runtime is unreachable, start
Docker Desktop or Docker Engine. Free the reported disk budget before retrying.
For `No published image for this tag or platform. Choose a published release with --image-tag VERSION and check Docker's architecture.`,
select a fixed published image tag and supported platform. See
[Docker setup](docker-mode-setup.md) for registry, smoke and cleanup failures.
Do not treat `--skip-smoke` as proof that a harness is ready.

## Check and start again

Init's host preflight can fail after saving configuration. Repair the saved
settings or rerun init with backups. Image provisioning and Docker smoke failures
happen before setup files are written. Stop the daemon before checking port
availability, then run:

```sh
coordinare config validate
coordinare doctor
```

If an old working directory has `config.yaml`, it wins over the generated user
configuration. Change directory or supply the intended path explicitly:

```sh
coordinare doctor --config ~/.config/coordinare/config.yaml
coordinare --config ~/.config/coordinare/config.yaml
```

Use your actual path if XDG settings or `--config-dir` changed it. After a clean
check, start with `coordinare` or reinstall the [user service](service.md). A
service installation failure after init preserves setup. Linux requires a
systemd user session; macOS requires a graphical login session. WSL without
systemd can use a foreground process.

A service uses a fixed PATH containing the registered command directory,
`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`,
`/usr/sbin` and `/sbin`. It does not inherit your interactive shell PATH. If a
harness installed under nvm or another custom directory passes init but fails
at login, make its executable available on the service PATH, or use a foreground
process. Any interpreter needed by that executable must also be on the service
PATH.

For running services, inspect output with:

```sh
coordinare service status
coordinare service logs
```

The dashboard's activity feed shows progress and stalls even without notification
channels. Check the operational log and [stored run logs](files-and-paths.md)
when a card fails after setup succeeds.
