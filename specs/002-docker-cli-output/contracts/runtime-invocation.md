# Contract: Runtime Invocation (Shell + CLI)

**Feature**: 002-docker-cli-output  
**Consumer**: Developers/operators running coordinare without Docker

## Shell Script Entry

Path: `scripts/run-coordinare.sh`

## Invocation

```bash
scripts/run-coordinare.sh [--config <path>] [--structured-output] [--log-level <level>] [extra args]
```

## Behavior Contract

- MUST start coordinare using the same application entrypoint used by container mode.
- MUST print startup progress and startup result to stdout/stderr.
- MUST print major runtime events and periodic heartbeats at default verbosity.
- MUST support higher-granularity output through log-level settings.
- MUST support optional structured output mode.
- MUST exit non-zero on runtime errors.

## Exit Codes

- `0`: clean shutdown
- `!=0`: startup/runtime failure

## Error Contract

On error, output MUST include:
- failing phase (`startup` or `runtime`)
- concise failure reason
- next-step guidance when possible
