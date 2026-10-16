# Uninstalling

Let active work finish. If you registered a user service, remove it before
removing the command:

```sh
coordinare service uninstall
```

For a foreground process, stop it with Ctrl+C. Download the installer, inspect
it, then remove the tool and bundled performer:

```sh
curl -fsSL https://github.com/ViviDynamics/coordinare/releases/latest/download/install.sh -o install.sh
less install.sh
sh install.sh --uninstall
```

Uninstall offers to delete the managed Docker workspace volume for the default
config directory. This permanently removes its clones and temporary files. Use
`sh install.sh --uninstall --yes` to consent without a terminal; otherwise an
unattended uninstall preserves the volume and reports how to remove it. For an
init setup in a custom location, use
`sh install.sh --uninstall --config-dir /path/to/setup` (add `--yes` for automation).
The path must still exist so the installer can resolve the same volume name.

Stop performer containers first. If the volume is in use or Docker is unavailable,
uninstall reports the problem and continues. Retry volume removal after stopping
the containers. Custom `volume_name` overrides are operator-managed; inspect and
remove them explicitly with Docker. Service uninstall alone does not delete volumes.

This preserves configuration, secrets, state, transcripts and logs. It also
keeps uv, managed Python and shared download caches.

The optional purge has a narrow, legacy scope:

```sh
sh install.sh --uninstall --purge
```

It removes only `~/.coordinare`. It does not remove init's default
`~/.config/coordinare` or `~/.local/share/coordinare`, XDG overrides, custom
configurations or project state paths. Review [file locations](files-and-paths.md)
and remove those directories yourself only if you want to delete their secrets,
backups and run history. The installer does not remove the user service for you.
