# Quickstart: Performer Lifecycle

**Branch**: `019-performer-lifecycle` | **Date**: 2026-03-18

## Prerequisites

- Coordinare daemon running
- `config.yaml` present and valid
- At least one performer service running (implementer is the minimum)

---

## Scenario 1 — Verify backward-compatible single-implementer lifecycle

**Goal**: Confirm that existing config (no `performers:` key) produces identical behavior to pre-019.

**Steps**:

1. Run with existing `config.yaml` (no `performers:` section).
2. Dispatch a card.
3. Observe the dashboard — card transitions: TODO → In Progress → In Review.
4. Verify `performer_stage` logs show `"implementing"` only.
5. Verify `lifecycle_sequence` is `["implementing"]`.

**Expected**: Identical behavior to pre-019 deployment.

---

## Scenario 2 — Run a two-role lifecycle (implementer + reviewer)

**Goal**: Verify that after the implementer completes, the reviewer is automatically dispatched.

**Steps**:

1. In `config.yaml`, add:
   ```yaml
   performers:
     implementer:
       backend: opencode
       transport: subprocess
     reviewer:
       backend: opencode
       transport: subprocess
   ```

2. Dispatch a card. Observe:
   - `performer_stage` starts as `"implementing"`
   - After implementer returns `pr_opened`, `performer_stage` changes to `"reviewing"`
   - Reviewer is dispatched automatically (no human intervention)
   - After reviewer returns `approved`, card transitions to "In Review"

3. Verify the board shows "In Progress" throughout both roles (never exposes internal stage to human).

**Expected**: Two sequential performer dispatches; human sees only "In Progress" → "In Review".

---

## Scenario 3 — Skip a role by omitting it from config

**Goal**: Confirm that absent roles are silently skipped.

**Steps**:

1. Configure only `implementer` and `qa` in `performers:` (no reviewer, no security).
2. Dispatch a card.
3. Observe that `lifecycle_sequence` is `["implementing", "qa"]`.
4. Verify reviewer is never dispatched; QA follows implementer directly.

**Expected**: `lifecycle_sequence = ["implementing", "qa"]`; no errors, no warnings about missing roles.

---

## Scenario 4 — Human feedback routes back to implementer

**Goal**: Verify that an implementation-concern PR comment triggers re-dispatch of the implementer.

**Steps**:

1. Configure implementer + reviewer + tech_writer.
2. Dispatch a card through the full lifecycle to "In Review".
3. Post a PR comment: "The authentication logic is broken — it doesn't handle expired tokens."
4. Wait one coordinare poll cycle.
5. Observe: `classify_human_feedback` fires, classifies as `implementation`, `performer_stage` resets to `"implementing"`, implementer is re-dispatched.

**Expected**: PR comment routes to implementer; lifecycle re-runs from implementer stage; reviewer and tech writer re-run after the fix.

---

## Scenario 5 — Human approval advances to Done

**Goal**: Verify that a GitHub PR approval triggers the merge path.

**Steps**:

1. Card is in "In Review" state (all automated roles completed).
2. Human approves the PR on GitHub.
3. Wait one coordinare poll cycle.
4. Observe: `classify_human_feedback` detects approval, card transitions to "Done", PR is merged.

**Expected**: No additional performer dispatches after human approval; card moves directly to Done.

---

## Config Reference

```yaml
# config.yaml — performers section
performers:
  implementer:
    backend: opencode          # AI backend identifier
    transport: subprocess      # subprocess | ssh | kubernetes
    # executable: /path/to/opencode   # optional override
    # timeout_seconds: 1800           # optional override (default: 30 min)

  reviewer:
    backend: opencode
    transport: subprocess

  security:
    backend: claude-code
    transport: kubernetes
    image: coordinare-performer:full

  qa:
    backend: opencode
    transport: subprocess

  tech_writer:
    backend: opencode
    transport: subprocess
```

Roles absent from `performers:` are silently skipped in the lifecycle.

**Note**: As of V1, only the `subprocess` transport is fully implemented. The `ssh` and `kubernetes` transports raise `NotImplementedError` — roles configured with those transports will be skipped with a warning at startup. The `backend` field is stored for documentation purposes; transport selection currently uses `transport` and `executable` only.
