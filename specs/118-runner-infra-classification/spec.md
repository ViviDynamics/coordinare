# Spec 118: Classify self-hosted-runner setup failures as ENV_BLOCKED

## Problem (observed live 2026-06-24 on website #181/#173)

The website self-hosted runners intermittently fail the `setup-*` actions (e.g.
`ruby/setup-ruby@v1`) at the toolchain-cache step with:

```
##[error]Error: EACCES: permission denied, mkdir '/opt/hostedtoolcache'
```

This is a **pure infrastructure failure** — the job dies at runner setup before any
project code (rubocop, tests) runs; no code change can fix it. But coordinare's 090 CI-gate
classifier (`failure_classification.classify_failure_origin`) only short-circuits to
`env_blocked` when the normalized failure reason matches an `env_signature` built-in pattern.
The shipped built-ins cover artifact-storage-quota / runner-offline / billing — **not** the
runner setup-action / toolchain-cache permission failure. So this failure falls through to the
inherited/introduced/flake path, gets classified **`introduced`** (the diff touched files, so a
HEAD-only failing check looks code-caused), and the implementer is **bounced** — through cycles
it can never win, because the failure is infra. Live, this left website PR #181 (card #177)
bouncing and ultimately BLOCKED on what was actually a runner permission flake.

## Goal

Recognize self-hosted-runner setup/toolchain-cache failures as `env_blocked` infra, so the
spec-095 `env_blocked_gate` **HOLDs the card + notifies the operator** (no bounce, no
re-dispatch) instead of bouncing the implementer on an unfixable failure — exactly as it
already does for artifact-storage-quota. Benefits every symphony, not just website.

## Requirements

1. Add built-in `env_signature` pattern(s) (`src/coordinare/services/env_signature.py`,
   `_BUILTIN_PATTERNS`) matching the runner setup/toolchain-cache infra signature, at minimum:
   - `EACCES`/permission-denied on `/opt/hostedtoolcache` (or `hostedtoolcache` generally),
   - `setup-ruby`/`setup-node`/`setup-python` failing on a tool-cache mkdir/permission error,
   - the `AGENT_TOOLSDIRECTORY` / `RUNNER_TOOL_CACHE` permission variants.
   Pattern id e.g. `runner_toolcache_perm`; cause + operator-facing action string (point at
   the runner's `/opt/hostedtoolcache` perms / ARC `securityContext`).
2. The pattern is matched case-insensitively against the normalized reason
   (`failure_signature.normalize_reason(title, summary)`), the same surface the existing
   built-ins match — no change to the matching mechanism.
3. **Reason-coverage contingency:** if the check's `output.title`/`output.summary` do not carry
   the `hostedtoolcache`/`EACCES` text for these failures (verify during implementation against
   a real failed check via coordinare's App-token github_service — the operator PAT cannot read
   check-run output), extend the failing-check reason to include the failing step's annotation
   / job-log error line so the pattern has something to match. Prefer the pattern-only fix if
   the summary already carries it (as artifact-quota's built-in implies it does).
4. When matched → `classify_failure_origin` returns `env_blocked` (Row-0 short-circuit) → the
   `env_blocked_gate` HOLDs + notifies (existing 095 behavior) — no new gate logic.
5. Tests: a failed check whose reason carries the hostedtoolcache-EACCES signature classifies
   `env_blocked` (not introduced/bounce); a normal code failure still classifies introduced
   (no false positives); the operator-add path still composes.
6. Invariants: no secret values in logs/records; built-in (not operator-config) so all
   symphonies benefit; no change to the env_blocked_gate or notify layers.

## Out of scope

- The runner's `/opt/hostedtoolcache` permission fix itself — that's the actual root-cause fix,
  made in the `infrastructure/docker-base-images/actions-runner-linux` image
  (`chmod 1777 /opt/hostedtoolcache`) + ARC deploy; tracked separately, not a coordinare change.
- Auto-recovering / re-running the failed job (coordinare HOLDs + notifies; the operator or the
  runner fix clears it).

## Acceptance

- A `ruby/setup-ruby` `EACCES /opt/hostedtoolcache` failure classifies `env_blocked` → the card
  is HELD + the operator notified, NOT bounced to the implementer.
- Existing artifact-quota / runner-offline / billing built-ins and normal code-failure
  classification are unchanged. Coordinare unit suite green; adversarial review before merge.
