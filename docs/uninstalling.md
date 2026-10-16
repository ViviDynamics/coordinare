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
