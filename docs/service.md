# Start at login

After installing Coordinare, run `coordinare init` and accept "Start at login".
For an existing setup:

```sh
coordinare service install
coordinare service status
coordinare service logs
```

Installation starts the daemon immediately and registers it to start at login
and restart on failure. It uses this installation's `coordinare` executable,
`~/.config/coordinare/config.yaml` and the adjacent `coordinare.env`. The issue
195 `XDG_CONFIG_HOME` and `XDG_DATA_HOME` settings are respected. For a custom
init directory, use `coordinare service install --config '/path with spaces/config.yaml'`.
Later commands use the registered path, regardless of the current directory.
There is one service per user; installing again replaces its registration and
restarts it. Rerun install after moving or upgrading the installed tool so its
registered executable and version are updated.

macOS writes `~/Library/LaunchAgents/com.vividynamics.coordinare.plist`. Run from
your graphical login session. A small wrapper references the existing env file;
no credential is copied into the plist or wrapper. Logs go to
`~/.local/share/coordinare/service/coordinare.log`.

Linux writes `~/.config/systemd/user/coordinare.service` and uses
`systemctl --user`. Its `EnvironmentFile` references the existing env file.
Logs are available through `journalctl --user`. A systemd user session is
required; an SSH or container session without one cannot install the service.
This is a login service. Running after logout or before login requires your
administrator to enable user lingering separately.

```sh
coordinare service stop
coordinare service start
coordinare service restart
coordinare service uninstall
```

Stop prevents automatic respawn until start or the next login. Uninstall stops
the daemon and removes the registration and wrapper, preserving config, env,
state, transcripts and logs. Status reports running state, process start time,
registered installation version, config path and dashboard URL. A wildcard
dashboard bind address is shown as a reachable loopback URL. Logs follow output;
press Ctrl+C to return.

The foreground command remains `coordinare --config /path/to/config.yaml`.
Checkout developers can keep using `bin/install`, `bin/uninstall`, `bin/start`
and `bin/stop`, or the corresponding Makefile targets. Install and uninstall
share the Python service manager, with checkout config, env and interpreter
paths. Start retains its foreground and container modes; stop still cleans up
manual checkout processes after stopping the registered service.

The Linux CI probe uses a real systemd user manager and a small fixture daemon
that checks config and env delivery without GitHub credentials or model calls.
It exercises install, status, stop, start, restart and uninstall, plus unit
enablement. If the runner has no systemd user session, it explicitly reports
that reason in the job summary and does not claim a lifecycle pass.
