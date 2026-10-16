# First launch

Issue #200, epic #192.

## Requirements

1. An opt-in `preset: solo` or `preset: containers`, `harness`, GitHub owner,
   repository, project number and human reviewers expand into a runnable configuration.
   Credentials come from the environment. Solo uses the existing performer protocol,
   never a bare harness as the protocol executable. Only implementer is enabled.
2. Explicit settings override preset defaults. Without a preset, existing config
   validation and runtime defaults remain unchanged. Expanded config is inspectable
   with `coordinare config` with secrets redacted.
3. Doctor and daemon startup call the same importable preflight, including existing
   start checks. Check credentials and permissions, board columns, repository access,
   harness response, model connectivity, writable directories and dashboard port.
   Every failure names a problem and a concrete fix, and prevents the poll loop.
4. After preflight and service setup, startup describes boards, watched column,
   enabled and skipped roles, concurrency and dashboard URL.
5. Idle console and dashboard explain empty boards, ownership filters and blocked
   work using symphony-scoped state. Repeated identical console messages are suppressed.
6. A starter file of about 15 lines and first-card instructions are the documented
   starting point. The dashboard offers the same small documentation issue.

## Success criteria and validation

Tests cover expansion, overrides, unchanged legacy loading, dispatch construction,
every preflight failure, daemon gating, startup output and idle explanations.
The minimal file is loaded by a unit test in normal CI. Probes have bounded timeouts
of five seconds locally, or the configured container readiness timeout for a
container CLI cold install; no probe generates model output or mutates GitHub. Browser and
full-suite execution are performed by the operator outside this sandbox.

## Integration with #195

`coordinare.presets.expand_preset` accepts and returns raw config mappings without
mutating its input. `coordinare.launch_preflight.run_preflight` returns a DoctorReport.
`coordinare.first_launch.sample_issue` returns the sample title and body without
creating any external resource. This issue does not implement the init command.
