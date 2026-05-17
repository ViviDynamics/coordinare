# Quickstart: Closer PR Checks Gate (064)

## What it does

After 064 ships, the closer **will not** hand a PR off to human reviewers
until GitHub's required status checks pass. If checks are still running, the
card holds in the closer stage. If they fail, the card bounces back to the
implementer with the failing job names.

## Operator: enabling/tuning

In `config.yaml`, under your symphony:

```yaml
symphonies:
  my-symphony:
    closer_pr_checks:
      enabled: true                  # default
      pending_timeout_seconds: 900   # 15 min; raise for slow CI
      poll_interval_seconds: 30
      fail_open_on_error: true       # GraphQL error → FORWARD (default)
      treat_unknown_required_as: pass
```

To disable entirely (rollback): set `enabled: false`. Closer reverts to its
pre-064 behaviour.

## Operator: what you'll see

**Healthy path** — checks green:
```
INFO closer.pr_checks.decision action=FORWARD pr=123 head=abc1234 elapsed=42.1s
```

**Hold** — checks still running:
```
INFO closer.pr_checks.decision action=HOLD pr=123 head=abc1234 pending=[ci/test,build] elapsed=120.4s
```
Card stays in `closer` stage. Monitor re-polls every `poll_interval_seconds`.
No closer re-dispatch (the closer slot is free for other cards — FR-005).

**Bounce** — checks failed:
```
WARN closer.pr_checks.decision action=BOUNCE pr=123 head=abc1234 failed=[ci/test] reason=required_check_failed
```
Card returns to `implementer` with the failed job list in its context.

**Timeout bounce**:
```
WARN closer.pr_checks.decision action=BOUNCE pr=123 head=abc1234 reason=pending_timeout elapsed=901s
```

## Developer: testing locally

The policy is a pure function — exercise it without GitHub:

```python
from coordinare.services.pr_checks_policy import decide
from coordinare.services.pr_checks_service import CheckRollup, CheckEntry
from datetime import datetime, timezone

rollup = CheckRollup(
    pr_number=1,
    head_sha="abc",
    head_pushed_at=datetime.now(timezone.utc),
    branch_protection_readable=True,
    checks=[
        CheckEntry(name="ci/test", status="completed", conclusion="success", is_required=True),
    ],
)
print(decide(rollup, pending_timeout_seconds=900))
# GateDecision(action='FORWARD', reason='all required checks passing', ...)
```

The I/O wrapper (`PrChecksService`) is the only place that touches the
network — mock it in integration tests.

## Coordinare: defence-in-depth

Even if a closer's JSON says `approved=true`, coordinare re-queries the same
GraphQL in `_advance_stage` before transitioning to `monitoring_pr`. If the
closer was lying or stale, coordinare blocks the transition and logs the
mismatch. This mirrors the 043 lint-gate pattern.

## Rollback

1. Flip `enabled: false` in config.
2. Restart coordinare.
3. Closer behaves exactly as it did pre-064.

No state migration needed — the per-card `checks_state` field is optional and
ignored when the gate is disabled.
