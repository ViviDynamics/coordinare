# Contract: Shell Runtime Invocation

**Feature**: 002-docker-cli-output

## Entry Script

- Required path: `scripts/run-coordinare.sh`

## Invocation Pattern

```bash
scripts/run-coordinare.sh --config <path> [--log-level <level>] [--structured-output]
```

## Required Behavior

- Starts the same runtime application entrypoint used in compose mode.
- Emits startup, major activity, heartbeat, and shutdown output.
- Emits detailed diagnostics when higher log level is selected.
- Exits non-zero on runtime errors.
- Masks known sensitive fields in output.

## Validation Signals

- Startup success/failure visible within expected startup window.
- Runtime state (`idle/active/blocked/recovery`) inferable from output events.

## Output Example

```text
startup: daemon startup complete (run_mode=shell)
activity: processing cycle completed (cycle=1 phase=running)
heartbeat: daemon heartbeat (cycle=1 phase=running)
shutdown: daemon stopped (graceful=true)
```
