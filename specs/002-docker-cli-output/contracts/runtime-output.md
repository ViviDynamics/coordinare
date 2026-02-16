# Contract: Runtime Output Semantics

**Feature**: 002-docker-cli-output

## Output Categories

- `startup`: config checks, dependency checks, ready/not-ready
- `heartbeat`: periodic liveness/progress summaries
- `activity`: meaningful work-progress events
- `state_change`: idle/active/blocked/recovery transitions
- `failure`: actionable failure context
- `shutdown`: graceful or failure termination summary

## Level Semantics

- `info` (default): startup, major events, heartbeat, shutdown
- `debug`: high-granularity diagnostics
- `error`: failure events with failing-step context

## Redaction Rules

- Known sensitive fields MUST be masked before output.
- Non-sensitive diagnostic values MAY remain visible.

## Mode Consistency

Output category semantics MUST be equivalent across:
- shell-script execution
- docker-compose execution
