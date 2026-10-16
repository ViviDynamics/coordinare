# Upgrading

Let active work finish, then stop the foreground daemon with Ctrl+C or stop its
user service:

```sh
coordinare service stop
```

Rerun the installer to replace Coordinare and its bundled performer with the
latest release. Configuration and state are preserved.

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh | sh
```

Or download and inspect the script first:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh
```

To select a specific release, use its numeric tag:

```sh
sh install.sh --version 2026.10.84 --yes --no-init
```

The example tag is also used in installer tests. `--yes` consents to installing
uv if missing. `--no-init` is accepted for automation; init is always a separate
step. Reinstalling does not guarantee identical third party dependency versions.

For Docker performers, update the pinned image after upgrading:

```sh
coordinare init --force --run-mode docker
```

If init originally used a custom directory, add `--config-dir` with that same
directory to the image update command. Use `--config` with its `config.yaml` path
for validation, doctor and foreground startup. Otherwise these commands can
select an unrelated default setup.

Keep the daemon stopped during this step. Decline init's start offers if you
want to inspect the result first. Forced setup keeps private backups. Advanced
configurations that init refuses must have their performer images updated by
the operator. See [Docker setup](docker-mode-setup.md).

Check the configuration while the daemon is stopped:

```sh
coordinare config validate
coordinare doctor
```

For a registered service, reinstall its registration so the executable and
recorded version are current. Installation starts it immediately:

```sh
coordinare service install
coordinare service status
```

Use `coordinare service install --config /absolute/path/config.yaml` for a
custom setup directory. For a foreground process, run `coordinare` or the
explicit config command printed by init. See [user services](service.md).
