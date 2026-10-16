# Plan

Use a small docker_setup module with a synchronous injectable command runner, bounded
subprocess timeouts, release tag selection, preflight, pull, inspect and digest resolution.
Integrate before init writes files. Suppress duplicate container probes during init while
retaining host GitHub and model checks. Preserve preset defaults and write explicit endpoint
settings in generated YAML. Share the bounded throwaway-container smoke function between
init and CI; always clean up by name, including after a launch timeout. Add a pure doctor version check. Share the backend entrypoint
with the base image so the base variant can install the selected harness at runtime.

Tests use a fake runner and isolated XDG directories. CI uses a separate opt-in smoke script
with actual Docker. Validate lint, mypy, the ruff ratchet and relevant pytest selections.
