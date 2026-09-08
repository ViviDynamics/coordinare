# 174: Bounded environment bootstrap

Issue: #287. Optional workflow selected by `performers.env_bootstrap.workflow`.
User approved completing the thin workflow from the spike, including verifier integrity.

## Stories and requirements

1. FR-001: Opting into `env_bootstrap` forwards workflow and workflow_env through daemon bootstrap dispatch to Score. Backend fallback must not inherit other role workflows. Unconfigured behavior stays unchanged.
2. FR-002: One install harness turn, then inference, readiness, verify and report steps. A failing verification/readiness may receive one repair by default, configurable from zero to two. Repair receives the actual failure tail and rechecks the entire ordered gate sequence. No extra model planning/judging calls.
3. FR-003: A whole-run timeout defaults to 1800 seconds, configurable 1..3600, also limited by daemon bootstrap budget when positive. Turn timeout/cancellation stops the inner harness, never starts a repair after cancellation, and reports failure on timeout. Existing outer retry breaker is unchanged.
4. FR-004: Before installation, snapshot promised coordinare-owned verify.sh and activate.sh in trusted process memory. Missing, non-regular, symlinked or changed artifacts fail without repair or success. Verify must exist and pass for opted-in workflows, including when not originally provided. Unconfigured legacy degraded behavior is unchanged. This protects accidental installer changes, not against arbitrary hostile code with access to the performer process.
5. FR-005: Service inference keeps its best-effort timeout semantics. Readiness precedes verify, and is skipped when coordinare_manages_services is false. Final report preserves inference fields.
6. FR-006: Only the configured workflow report may map success to env_bootstrap_complete. Failures map to error; malformed reports fail closed. Never enter push/PR post-processing or execute legacy gates twice. Clean consumer verification stays in daemon and remains authoritative.
7. FR-007: Emit named step progress and durations, actual harness turn counts and bounded failure text. Preserve live inner harness events. No secret values are logged by new code.

## Success criteria

- SC-001: Fixtures demonstrate success, one-turn repair and bounded exhaustion; at most three harness turns and zero workflow model calls. Fixture overhead under five seconds excluding deliberate timeouts.
- SC-002: Real production Toolkit/adapter fixture covers dispatch-to-Score, terminal result and cache integrity, both service modes, inference timeout and cancellation. Rule mutations fail their named tests and restore exact source hashes.
- SC-003: Full coordinare and performer suites pass separately, coverage >=90%, lint, browser/image checks and current-head CI pass before squash merge. Production config is not enabled as part of this code delivery.

## Edge cases

Missing cache path fails before install. Invalid budgets fail before install. Readiness failure
can repair services; inference timeout alone does not fail. File mutation fails even if verify
would return zero. Declared artifacts are never accepted solely from repository git status.
A persisted cache cannot bypass the final consumer check. No new persisted state schema.
