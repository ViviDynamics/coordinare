# Contract: QA Freshness Check

**Feature**: 054-async-multi-card-orchestration  
**Type**: Performer protocol extension (additive)  
**Files affected**: `agent/performer/src/performer/main.py`, `src/coordinare/protocol.py` (optional type annotation), `src/coordinare/graph/nodes/monitor_performer.py`

## Overview

QA performer responses for in-flight open-PR work MUST include a `qa_freshness_check` block in the `report` payload. This block allows coordinare to verify that the branch being QA-checked includes the latest `main` commits before accepting a `qa_passed` outcome.

## Dispatch: Coordinare → Performer

When dispatching to the QA persona, coordinare MUST include `latest_main_sha` in the dispatch payload:

```json
{
  "role": "qa",
  "card_id": "<card_id>",
  "latest_main_sha": "<state.last_known_main_sha>"
}
```

If `last_known_main_sha` is `null` (not yet populated), coordinare omits the field; the performer's `Score.latest_main_sha` will be empty.  QA MUST include a `qa_freshness_check` block with `up_to_date: null` and `detail: "freshness_check_indeterminate"`, but MUST NOT add a failure entry — QA can still pass when coordinare has not yet populated the SHA (e.g. first cycle after startup).  This is a non-blocking indeterminate.  A `freshness_indeterminate` failure is only added when a git or environment error occurs during the check itself.

## Response: Performer → Coordinare

QA performer adds `qa_freshness_check` to the `report` dict in its status response.

### `status: qa_passed` (freshness passes)

```json
{
  "status": "qa_passed",
  "report": {
    "qa_freshness_check": {
      "latest_main_sha": "abc123",
      "branch_head_sha": "def456",
      "up_to_date": true,
      "detail": "branch includes latest main (git merge-base confirmed)"
    }
  }
}
```

### `status: qa_failed` (freshness fails)

```json
{
  "status": "qa_failed",
  "failures": [
    {
      "file": null,
      "line": null,
      "message": "Branch is behind latest main — rebase required before QA can pass",
      "type": "freshness"
    }
  ],
  "report": {
    "qa_freshness_check": {
      "latest_main_sha": "abc123",
      "branch_head_sha": "old789",
      "up_to_date": false,
      "detail": "abc123 is not an ancestor of old789"
    }
  }
}
```

### `status: qa_failed` (freshness indeterminate)

```json
{
  "status": "qa_failed",
  "failures": [
    {
      "file": null,
      "line": null,
      "message": "Branch freshness could not be verified — environment/git failure",
      "type": "freshness_indeterminate"
    }
  ],
  "report": {
    "qa_freshness_check": {
      "latest_main_sha": null,
      "branch_head_sha": null,
      "up_to_date": null,
      "detail": "freshness_check_indeterminate"
    }
  }
}
```

## Implementation: QA Performer Check

The QA performer checks freshness via:

```bash
git merge-base --is-ancestor <latest_main_sha> HEAD
```

Exit code 0 → branch includes latest main (`up_to_date: true`).  
Exit code 1 → branch does NOT include latest main (`up_to_date: false`).  
Non-zero from command error / missing sha → indeterminate.

## Coordinare Handling (monitor_performer.py)

No new code path required. The existing `qa_failed` handling at monitor_performer.py:1065 already:
1. Relays failures back to the implementer via `relay_feedback`.
2. Increments `feedback_cycle_count`.
3. Blocks the card when `max_feedback_cycles` is exhausted.

A freshness failure goes through the same loop. After the rebase (triggered by coordinare's existing rebase-round detection), the implementer stage handles the rebase outcome and QA is re-dispatched on the next eligible cycle.

## Backward Compatibility

- `qa_freshness_check` is an additive field in `report`; existing QA consumers that do not read this field are unaffected.
- If `report` is `None`, coordinare treats the absence as `qa_freshness_check` not present (no freshness enforcement — backward-compatible grace mode for performers not yet updated).
- `up_to_date: null` is the sentinel for "could not determine". Two distinct cases apply:
  - **Missing SHA** (`latest_main_sha` not provided by coordinare): non-blocking; the freshness block is included with `up_to_date: null` and `detail: "freshness_check_indeterminate"`, but no failure entry is added and QA can still pass.
  - **Git/environment error** during the check itself: blocking; a `qa_failed` is returned with `type: "freshness_indeterminate"` and explicit detail.
