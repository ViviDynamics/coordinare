# Quickstart: Implementer Local Test Gate

## What it does

Makes the implementer run the detected test suite locally (env-cache active) before pushing. Green → push as today. Red for a code reason → no push, the agent gets the failing output and retries (bounded), then escalates to blocked. Red because the env-cache is broken → routed to a same-stage env-hold, not treated as a code defect.

It is a cheap pre-filter in front of the spec-075 remote CI gate — local pass ≠ remote pass; spec-075 stays authoritative.

## Enable it

In symphony config, next to the spec-075 implementer CI gate:

```yaml
local_test_gate:
  enabled: true
  timeout_seconds: 600     # generous; slow suites are not hangs
  max_fix_attempts: 2      # coordinare-side re-dispatch budget before escalate-to-blocked
```

Omit or `enabled: false` → behaviour is byte-identical to pre-089 (lint, then push).

## End-to-end flow (enabled)

1. Implementer finishes; role contract has already had it run/fix tests in-session.
2. Done-path: lint runs (spec-043). If lint fails → `changes_requested` (unchanged).
3. Lint passes → `_run_test_check` runs the detected `test_command`:
   - **Green / no test_command / standalone** → push branch, open PR (unchanged path).
   - **Red, code reason** → no push; `changes_requested` with the failing tail + `local_test_failed=True`.
   - **Red, env-cache signal present** → `status="env_blocked"` with the env reason; no push.
4. Coordinare:
   - `changes_requested` + `local_test_failed` → increment `local_fix_counter[head]`; re-dispatch while ≤ `max_fix_attempts`, else → **blocked** (reason = test output).
   - `env_blocked` → hold same stage, `mark_runtime_health_failed`, wait for cache repair. No attempt consumed.

## Verify

```bash
# Helper + done-path branch selection
.venv/bin/pytest agent/performer/tests/unit/ -k "test_check or local_test or env_blocked" -q

# Coordinare route, counter, escalation, config
.venv/bin/pytest tests/unit/ -k "local_test_gate or env_blocked or local_fix_counter" -q

# Regression: disabled-by-default unchanged
.venv/bin/pytest agent/performer/tests/unit/ tests/unit/ -q

.venv/bin/ruff check agent/performer/src/performer/main.py src/coordinare/
```

## Acceptance check (maps to spec SC)

| Check | SC |
|-------|----|
| Enabled + breaking test → no PR opened, failing output surfaced locally | SC-001, SC-002 |
| Env-cache failure + red tests → env-blocked, 0 attempts consumed, no changes_requested | SC-003 |
| `local_fix_counter` moves independently of `bounce_counter` | SC-004 |
| Disabled → existing tests green, no behavioural diff | SC-005 |
