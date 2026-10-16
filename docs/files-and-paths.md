# Configuration, secrets, state and logs

These are the defaults for release installation and a fresh `coordinare init`.
Absolute `XDG_CONFIG_HOME` and `XDG_DATA_HOME` values replace the corresponding
roots; relative values are ignored. Init prints its resolved config and secrets
paths. `--config-dir` changes the setup directory.

| Data | Default location | Notes |
|---|---|---|
| Commands | `~/.local/bin/coordinare`, `~/.local/bin/performer` | Add `~/.local/bin` to PATH |
| Tool environment | `~/.local/share/uv/tools/coordinare` | Installer fixes this under HOME |
| Managed Python | `~/.local/share/uv/python` | Shared with uv tools |
| uv download cache | `~/.cache/uv` | Kept on uninstall |
| Configuration | `~/.config/coordinare/config.yaml` | Uses credential references |
| Secrets | `~/.config/coordinare/coordinare.env` | Mode `0600`; loaded automatically |
| Setup backups | `config.yaml.bak`, `coordinare.env.bak` beside the originals | Repeated backups add another `.bak`; mode `0600` |
| Daemon state | `~/.local/share/coordinare/state.json` | Generated `state_file_path` |
| Run logs | `~/.local/share/coordinare/run-logs` | Generated `run_logs.dir` |
| Transcripts | `~/.local/share/coordinare/transcripts` | Generated `transcripts.dir` |
| Docker workspaces | Docker volume `coordinare-workspaces-<config directory hash>` | Mounted at `/workspace`; see `volume_name` in config; removal requires consent |
| Docker artifacts | `~/.local/share/coordinare/artifacts` | Host bind at `/artifacts` |
| Docker environment caches | `~/.local/share/coordinare/env-caches` | Generated `env_cache_root` in Docker mode |
| Service metadata | `~/.local/share/coordinare/service/registration.json` | Records the registered configuration |
| macOS service wrapper and log | `~/.local/share/coordinare/service/run.sh`, `~/.local/share/coordinare/service/coordinare.log` | Wrapper references the env file |
| macOS login registration | `~/Library/LaunchAgents/com.vividynamics.coordinare.plist` | Per user |
| Linux login registration | `~/.config/systemd/user/coordinare.service` | Respects absolute `XDG_CONFIG_HOME` |
| Linux service logs | User journal | Read with `coordinare service logs` |

A foreground daemon writes its operational log to the terminal. Run logs and
transcripts contain performer output; they are separate from the service log.
Explicit settings retained on an init rerun can point state and logs elsewhere.

Config discovery uses the first match: `--config`, `COORDINARE_CONFIG_PATH`,
`./config.yaml`, the XDG user config, then legacy `~/.coordinare/config.yaml`.
Run outside an old checkout or pass `--config` explicitly to select the new setup.
The adjacent `coordinare.env` loads before validation, doctor and daemon startup.
Already exported environment variables take precedence over that file.

Uninstall preserves these files. The installer's `--purge` removes only the
legacy `~/.coordinare` directory. See [uninstalling](uninstalling.md).
