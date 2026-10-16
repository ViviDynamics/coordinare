# Plan

Use standard library plist serialization and subprocess execution. Separate file
generation, manager operations and argparse integration. Persist only nonsecret
registration metadata in the existing user data directory, allowing subsequent
commands to find custom init config directories. Use systemd EnvironmentFile and
a shell wrapper on macOS. Generate quoted argv rather than interpolate shell code.

Keep the checkout foreground launcher and process cleanup. Adapt checkout install
and uninstall to the same Python service manager with explicit checkout paths.
No daemon, dashboard, performer protocol or dependency changes are required.
