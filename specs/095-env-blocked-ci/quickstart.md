# Quickstart: ENV_BLOCKED CI-Failure Classification

## What this delivers

CI failures that no code change can fix — artifact-storage quota exhausted, runner offline, billing/spending-limit, external-dependency outages in required jobs — are classified `ENV_BLOCKED` *before* the spec-090 inherited/introduced repair path. An ENV_BLOCKED card is **held** (no autonomous repair, no performer re-dispatch), the specific infra cause + needed action is **surfaced once** to the operator, and normal flow **auto-resumes** when the condition clears. Detection is signature-based, operator-extensible, fail-safe, and default-off.

## Enable it (per symphony)

Under the symphony's `persona_scope` in config (mirrors the 090 gates):

```yaml
persona_scope:
  env_blocked_gate:
    enabled: true
    # built-in patterns (artifact quota / runner offline / billing) always apply when enabled;
    # add project-specific ones here:
    patterns:
      - id: custom_registry_outage
        regex: "registry.*unavailable|pull access denied"
        cause: "Container registry unavailable"
        action: "Check registry status / credentials"
```

## Reproduce the bug it fixes (US1 / #159)

1. A card's PR has passing tests but a required job fails because the org Actions artifact-storage quota is exhausted (`Upload test artifacts` 403s).
2. **Before**: 090 labels it INHERITED (it also fails on main), coordinare fails-to-repair or re-dispatches the performer indefinitely (the #159 9-day bounce).
3. **After**: classification returns `env_blocked`; coordinare holds the card (no repair, no re-dispatch) and notifies the operator: *"CI artifact-storage quota exhausted — raise the Actions storage budget or clear old artifacts."*

## Verify each user story

- **US1 — hold, don't bounce**: infra-signature required failure → `env_blocked`, no repair mandate, zero performer re-dispatch; non-infra failure → falls through to existing 090 logic.
- **US2 — surface once**: exactly one operator notification naming cause + action (not "tests failed"); same condition across cycles → no re-notify; notification carries no secret values.
- **US3 — auto-resume**: after the operator clears the infra condition, the next evaluation has no infra signature → hold clears, normal classify/dispatch resumes within one cycle, no manual reset.

## Success-criteria checks

- **SC-001**: zero performer re-dispatches for an infra-only block.
- **SC-002/SC-003**: cause+action surfaced, once per condition.
- **SC-004/SC-008**: resumes within one cycle after the condition clears.
- **SC-005**: no non-infra failure is ever mislabeled `env_blocked`.
- **SC-006**: disabled → identical to pre-feature baseline.
- **SC-007**: the #159 quota scenario → `env_blocked` + storage-budget action.

## Run the tests

```bash
.venv/bin/pytest tests/unit/services/test_env_signature.py \
  tests/unit/services/test_failure_classification_env.py \
  tests/unit/graph/nodes/test_monitor_performer_env_blocked.py -v
.venv/bin/ruff check src/coordinare/services/env_signature.py src/coordinare/services/failure_classification.py
```

## Edge cases to confirm

- Mixed env-blocked + introduced failure on one card → introduced still classified/surfaced (not masked).
- Infra signature on a non-required check → does not hold the card.
- Flapping infra (clears then recurs) → resume on clear, re-block + re-notify-once on recurrence.
- Baseline indeterminate → env detection still works (head-only).
