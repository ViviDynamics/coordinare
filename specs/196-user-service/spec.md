# 196: Installed user service

An installed Coordinare can start at login without a checkout. macOS uses a
LaunchAgent and Linux uses a systemd user unit. Installation starts the daemon
and enables restart on failure. The default foreground command stays available.

## Requirements

1. Install uses the installed executable and issue 195 config and env paths.
2. Start, stop, restart, status, logs and uninstall use an injectable runner.
3. Status reports running state, start time, installed version, registered config
   path and dashboard URL. Missing installation is reported without starting it.
4. Secret values are never embedded in generated service files. Paths containing
   spaces, quotes, dollar signs and percent signs retain their literal meaning.
5. Uninstall stops the service and removes only service registration files.
6. Successful interactive init offers start at login and calls service install.
   Noninteractive init never installs a service without an explicit flag.
7. Checkout scripts and Makefile targets remain available and share service code.
8. CI exercises the Linux install, status and uninstall lifecycle with a user
   manager, or explicitly reports why a systemd user session is unavailable.

## Validation

Service manager calls time out after 60 seconds. Status performs at most two
manager or process queries and makes no network request. Streaming logs have no
timeout. Deterministic runner tests measure the query count.

Write deterministic unit tests first, including fake managers, failure reporting,
quoting, secret isolation, init integration and preservation of config and state.
Run focused pytest, Ruff, strict mypy and the Ruff baseline ratchet. Real login
and reboot verification requires the target operating system outside the sandbox.
