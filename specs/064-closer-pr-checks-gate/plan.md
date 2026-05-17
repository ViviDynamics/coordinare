# Implementation Plan: Closer PR-Checks Gate (064)

**Branch**: `064-closer-pr-checks-gate`
**Date**: 2026-05-16
**Spec**: [spec.md](./spec.md)

## Summary

Close the last remaining bot-side blind spot before a PR reaches a human reviewer: verify that the PR's combined GitHub check rollup is green. The closer persona reads the rollup and refuses to approve a red PR; the coordinare's `_advance_stage` runs a second independent rollup query immediately before transitioning to `monitoring_pr` (defence-in-depth, mirroring the 043 local-lint gate). Pending checks put the card into a passive hold state where each performer-monitor tick re-polls the rollup over HTTP only — the closer is not re-dispatched, leaving its slot free for other cards.

## Technical Context

**Language/Version**: Python 3.11 (coordinare) + the existing performer image (Node + gh CLI already baked in)
**Primary Dependencies**: `httpx` (already a transitive dep via the GitHub service), `pydantic` v2 for the rollup schema, structlog for observability
**Storage**: No new persistent state. Timeout anchor derives from the PR's HEAD-commit push timestamp (always re-derived from GitHub on each evaluation per FR-009).
**Testing**: pytest (unit + integration). Mocked GraphQL responses for the rollup query. One integration test against a fixture PR with an intentionally failing required check.
**Target Platform**: macOS dev + Linux daemon (existing coordinare runtime)
**Project Type**: single (coordinare backend + performer CLI)
**Performance Goals**: ≤1 GraphQL request per closer dispatch, ≤1 per monitor tick during pending hold. p95 rollup query latency budget: 800ms (existing GitHub service latency norms).
**Constraints**: Must not block the `_advance_stage` path on its own failure modes (FR-007 — log and proceed on gate errors).
**Scale/Scope**: Single new helper (`get_pr_check_rollup`), one persona-directive insert, one branch in `_advance_stage`, one new config block. ~400 LoC of production code + ~600 of test scaffolding.

## Architecture Overview

```
┌──────────────────────── closer dispatch ────────────────────────┐
│  closer persona instructs backend to:                           │
│   1. Resolve open review threads (existing behaviour)           │
│   2. Query statusCheckRollup for the PR (NEW, FR-001)           │
│   3. Map conclusions per FR-002 table                           │
│   4. Emit JSON verdict {approved, status?, body, comments}      │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────── _advance_stage (NEW gate) ───────────────────┐
│  Existing 043 lint gate runs first (workspace lint)             │
│                                                                 │
│  ┌── NEW: PR-checks gate (FR-004) ──────────────────────────┐   │
│  │  Re-query statusCheckRollup independently                │   │
│  │                                                          │   │
│  │  Required check set = branch protection ∩ rollup,        │   │
│  │     or "all non-skipped" fallback (FR-006)               │   │
│  │                                                          │   │
│  │  conclusions → action:                                   │   │
│  │    all in {success, neutral, skipped} → forward to       │   │
│  │       monitoring_pr (status quo path)                    │   │
│  │    any in {failure, cancelled, timed_out,                │   │
│  │       action_required, stale, startup_failure} →         │   │
│  │       bounce to implementing with named-job feedback     │   │
│  │    any still queued/in_progress AND                      │   │
│  │      (now - head_pushed_at) < pending_timeout →          │   │
│  │       HOLD: keep stage=closing, set                      │   │
│  │       checks_state="pending_checks", schedule re-tick    │   │
│  │    any still queued/in_progress AND elapsed ≥ timeout →  │   │
│  │       bounce to implementing with "checks did not        │   │
│  │       complete in <X>s" message                          │   │
│  │                                                          │   │
│  │  On query error (network/auth/rate-limit): log warning,  │   │
│  │     proceed to monitoring_pr (FR-007 — gate is defence-  │   │
│  │     in-depth, not a hard dependency)                     │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘

┌─────────────── performer-monitor tick (pending hold) ───────────┐
│  if current_card.checks_state == "pending_checks":              │
│    re-run gate logic (HTTP only — no closer dispatch)           │
│    On resolution → fall through to forward/bounce branches      │
└─────────────────────────────────────────────────────────────────┘
```

## Key Files & Modules

### New files

- `src/coordinare/services/pr_checks_service.py` — Single-purpose helper exposing `get_pr_check_rollup(owner, repo, pr_number) -> CheckRollup`. Uses the existing `github_service` client / GraphQL session. Pure I/O wrapper — no state, no branching logic, no policy.
- `src/coordinare/services/pr_checks_policy.py` — Pure-function decision module: `decide(rollup, head_pushed_at, now, pending_timeout) -> GateDecision`. Decision enum: `FORWARD | BOUNCE | HOLD`. Holds the FR-002 conclusion-mapping table and the FR-009 timeout math. **No I/O.** Trivially unit-testable.
- `tests/unit/services/test_pr_checks_policy.py` — Exhaustive table-driven tests for every conclusion combination and timeout edge case.
- `tests/unit/services/test_pr_checks_service.py` — Mocked GraphQL response tests for the I/O wrapper.
- `tests/integration/test_advance_stage_pr_checks_gate.py` — Tests the wired-in gate in `monitor_performer._advance_stage`. Uses the existing card-state fixtures and a stubbed `pr_checks_service`.

### Modified files

- `src/coordinare/services/persona_service.py` — Add a closer-specific PR-checks directive. Inserted after `_CI_REVIEWER_DIRECTIVE` in the closer entry (line 234). Directive text is in [Persona Directive (closer)](#persona-directive-closer) below.
- `src/coordinare/graph/nodes/monitor_performer.py` — Two changes:
  1. In `_advance_stage` (line 426), after the existing 043 lint gate and before the `monitoring_pr` transition, call `pr_checks_policy.decide(...)` and branch.
  2. In the performer-monitor tick path (around line 196), add a fast-path: if `state.current_card.checks_state == "pending_checks"`, re-run the gate logic without re-dispatching the closer.
- `src/coordinare/state.py` (or wherever `CoordinareState` / `current_card` is typed) — Add an optional `checks_state: Literal["pending_checks"] | None = None` field to `current_card`. No migration needed (sqlite TEXT column added on next boot; absence is the default).
- `src/coordinare/config.py` — Add the `closer_pr_checks` block to symphony config: `enabled: bool = True`, `pending_timeout_seconds: int = 900`. Per-symphony override supported via existing config merge.
- `src/coordinare/services/persona_service.py` — Closer entry also gets a one-line `closer-pr-checks-status` field added to the JSON contract documentation (`{"approved": bool, "status"?: "pending_checks", "body": str, "comments": list[str]}`).

### Test scaffolding

- Fixtures for three rollup payload shapes: all-green, mixed-pending, failure (each with branch-protection and "no-branch-protection fallback" variants).
- Mocked monitor-tick test verifying that pending hold doesn't dispatch the closer.

## Persona Directive (closer)

Append to the closer entry in `persona_service.py`:

```text
**PR check status (064)**: Before producing your verdict, query the
PR's combined check rollup (`gh pr checks <number> --json` or the
equivalent GraphQL `statusCheckRollup`). Apply this conclusion table:

- success, neutral, skipped → counts as pass
- failure, cancelled, timed_out, action_required, stale,
  startup_failure → refuse: return approved=false and name the
  failing job(s) in `comments` with a short tail of the failing step
- queued, in_progress (no conclusion yet) → return approved=false
  with an additional field `"status": "pending_checks"`. The coordinare
  will hold the card and re-poll without dispatching you again.

Required-check determination: prefer the rollup's `isRequired` flag
(populated when branch protection is readable). If no check is marked
required (token lacks branch-protection read, or no protection rule),
treat every non-skipped check as required.

The coordinare runs an independent rollup query immediately after your
verdict as a defence-in-depth gate. Do not skip your own query on the
assumption the gate will catch it — the gate logs and proceeds on its
own errors (network, auth) and your verdict is the primary signal.
```

## Data Shapes

```python
# src/coordinare/services/pr_checks_service.py

class CheckEntry(BaseModel):
    name: str                 # job name, e.g. "test (ruby-3.2)"
    status: Literal["queued", "in_progress", "completed"]
    conclusion: Literal[
        "success", "failure", "neutral", "cancelled", "skipped",
        "timed_out", "action_required", "stale", "startup_failure",
    ] | None = None           # None while status != completed
    is_required: bool = False # populated from branch protection when readable
    details_url: str | None = None

class CheckRollup(BaseModel):
    pr_number: int
    head_sha: str
    head_pushed_at: datetime   # GitHub's commit.pushedDate; falls back to authoredDate
    branch_protection_readable: bool
    checks: list[CheckEntry]

# src/coordinare/services/pr_checks_policy.py

class GateDecision(BaseModel):
    action: Literal["FORWARD", "BOUNCE", "HOLD"]
    reason: str                       # operator-facing one-liner
    failed_jobs: list[str] = []       # populated for BOUNCE on real failures
    elapsed_seconds: float | None = None  # for HOLD/timeout-BOUNCE observability
```

```yaml
# config/symphony.yaml — new block, defaults shown
closer_pr_checks:
  enabled: true
  pending_timeout_seconds: 900   # 15 minutes
```

## Coordinare Gate Flow

1. `_advance_stage` runs the existing 043 workspace-lint gate (lines 426–476). No change.
2. If lint passes, compute the gate's effective "required set":
   - Pull rollup via `pr_checks_service.get_pr_check_rollup`.
   - If `rollup.branch_protection_readable`: required set = checks with `is_required=True`.
   - Else: required set = checks with `conclusion != "skipped"` (FR-006 fallback).
3. Call `pr_checks_policy.decide(required_subset, rollup.head_pushed_at, now, config.pending_timeout_seconds)`. Branch on `decision.action`:
   - `FORWARD`: existing path — set `phase = "monitoring_pr"`, clear `checks_state`, advance card status to `IN_REVIEW`.
   - `BOUNCE`: route back to implementer. Compose `relay_feedback` body: ``"PR-check gate failed:\n```\n<failing job: short log tail>\n```"`` per failed job (cap at 3 jobs, 500 chars each — bounded payload). Clear `checks_state`.
   - `HOLD`: do NOT transition. Set `current_card.checks_state = "pending_checks"`, record `elapsed_seconds` in structured log, return state unchanged otherwise. The next monitor tick picks it up.
4. On any `pr_checks_service` exception (timeout, HTTP error, JSON parse): log `pr_checks_gate.query_failed` at WARNING, proceed to `monitoring_pr` (FR-007).

## Monitor Tick Fast-Path

The performer-monitor's existing tick logic (around `monitor_performer.py:196`) acquires `current_card`. Add at the top of the per-card body:

```python
if current_card.get("checks_state") == "pending_checks":
    decision = _evaluate_pr_checks_gate(state, current_card)
    if decision.action == "HOLD":
        return {}            # nothing to do; another tick later
    # FORWARD / BOUNCE: fall through to the same handling _advance_stage uses
    return _apply_pr_checks_decision(state, current_card, decision)
```

`_evaluate_pr_checks_gate` is a thin extraction of the gate block from `_advance_stage` so both call sites share the same code path. No closer re-dispatch occurs in this branch — the agent dispatch state is untouched.

## Branch-Protection Lookup

The GraphQL query bundles branch protection into the same request via `repository.branchProtectionRules`:

```graphql
query PRChecks($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      headRef { name }
      commits(last: 1) {
        nodes {
          commit {
            pushedDate
            authoredDate
            oid
            statusCheckRollup {
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    status
                    conclusion
                    detailsUrl
                  }
                  ... on StatusContext {
                    context
                    state
                    targetUrl
                  }
                }
              }
            }
          }
        }
      }
    }
    branchProtectionRules(first: 50) {
      nodes {
        pattern
        requiredStatusChecks { context }
      }
    }
  }
}
```

`is_required` is computed client-side: a check's name (CheckRun) or context (StatusContext) is required iff some `branchProtectionRule.pattern` matches the PR's base ref AND that rule's `requiredStatusChecks` includes the check's identifier. If the `branchProtectionRules` field is empty (token lacks `repo` admin scope, or no protection configured), `branch_protection_readable = False` and the fallback rule applies.

`StatusContext.state` maps onto the `conclusion` enum: `SUCCESS → success`, `FAILURE → failure`, `ERROR → failure`, `PENDING → null + status=in_progress`, `EXPECTED → null + status=queued`.

## Constitution Check

| Principle | Compliance |
|---|---|
| I. Code Quality First | Two new files, both single-responsibility (I/O wrapper vs. pure policy). Type annotations on every public interface. Decision policy is a pure function — readable and unit-testable. |
| II. Testing Discipline (NON-NEGOTIABLE) | Three test files: unit (policy, table-driven for every conclusion combo), unit (service, mocked GraphQL), integration (advance_stage gate). All deterministic — GraphQL is mocked; the integration test uses fixture responses. Coverage of the new modules must be ≥90% (policy module trivially achievable). |
| III. User Experience Consistency | The gate's bounce-relay-feedback follows the existing 043 lint-gate format (`"...gate failed:\n\`\`\`\n<output>\n\`\`\`"`). Observability events use the existing `pr_checks_gate.*` namespace pattern. |
| IV. Performance by Design | Cost budget: 1 GraphQL request per closer dispatch + 1 per monitor tick during pending hold. With default 30s tick and 900s timeout, worst case is 31 requests for a maximally-slow PR — well under any sensible rate-limit budget. Budget recorded in Technical Context. |
| V. Clarity Before Action | Five clarifications resolved in spec.md before planning. Two open questions remain in the spec but are tagged as low-impact tuning concerns, not implementation blockers. No `NEEDS CLARIFICATION` markers in this plan. |

**Gate result**: PASS — no violations, Complexity Tracking section omitted.

## Phased Delivery

**Phase 1 — Policy module + persona directive (no wiring)**
Ship `pr_checks_policy.py` with full unit coverage and the closer persona directive. The persona now reads the rollup and produces correct verdicts; nothing on the coordinare side acts on the verdict yet beyond logging. Validates the policy table and the closer's behaviour in isolation.

**Phase 2 — Service wrapper + coordinare gate (forward + bounce only)**
Land `pr_checks_service.py` and wire `_advance_stage` to FORWARD or BOUNCE. Pending checks are treated as BOUNCE in this phase (matches the current "fail-closed on uncertainty" instinct). Validates the GraphQL query, branch-protection lookup, and the bounce-feedback shape against real PRs.

**Phase 3 — HOLD state + monitor-tick fast-path**
Add `checks_state` to `current_card`, implement the HOLD branch and monitor-tick re-evaluation. The pending case stops bouncing and starts waiting. Validates the loop semantics and the `pending_timeout` anchor math under real-world check timings.

**Phase 4 — Config exposure + symphony override**
Make `enabled` and `pending_timeout_seconds` operator-tunable per symphony. Defer until at least one symphony has run cleanly with the Phase 3 defaults for a week.

## Phased Delivery (test-first)

Each phase's test files land in the same PR as the production code for that phase, and follow the constitution's TDD posture: write the table-driven policy tests first, watch them fail, implement, watch them pass.

## Resolved Decisions

1. **Module split (I/O vs. policy)**: The pure-function `pr_checks_policy.decide()` is intentionally separate from `pr_checks_service.get_pr_check_rollup()`. The policy module has zero external dependencies and tests in milliseconds; the service module is the only place GraphQL or network appears. This mirrors how `ci_detection.py` (043) is structured.
2. **No new persistent state for the timeout anchor**: `head_pushed_at` is always re-read from GitHub on every evaluation (FR-009). This avoids a `current_card` field that could drift if the daemon crashed between observations, and naturally resets on new pushes without any reset logic.
3. **`checks_state` IS stored on the card** (modest persistent state): It's the signal that tells the monitor-tick fast-path to skip closer dispatch and re-evaluate the gate. Without it, the tick has no way to distinguish "card is mid-closing, waiting on closer LLM" from "card is in pending-checks hold, waiting on GitHub." One enum-valued column.
4. **Fail-open on gate errors**: Matches FR-007 and the 043 lint-gate precedent. The alternative — fail-closed and bounce the card — would let GitHub flakes hold up otherwise-healthy PRs indefinitely. Operators would lose more time recovering than they save by catching the rare check that flips red between closer-read and gate-read.
5. **Combined Check Runs + commit Statuses via `statusCheckRollup`**: Selected per spec.md clarification Q4. Matches PR UI semantics and branch-protection enforcement; covers legacy CIs automatically.
6. **HTTP-only re-poll during pending hold**: Selected per spec.md clarification Q5. Avoids per-tick LLM cost; closer slot stays free for other cards in flight.

## Observability

New structured-log events (all under `coordinare.graph.nodes.monitor_performer` logger):

- `pr_checks_gate.query_started` — DEBUG; fields: `pr_number`, `head_sha`
- `pr_checks_gate.forward` — INFO; fields: `pr_number`, `required_check_count`, `passing_count`
- `pr_checks_gate.bounce` — WARNING; fields: `pr_number`, `failed_jobs[]`, `elapsed_seconds`
- `pr_checks_gate.hold` — INFO; fields: `pr_number`, `pending_count`, `elapsed_seconds`, `budget_remaining_seconds`
- `pr_checks_gate.timeout_bounce` — WARNING; fields: `pr_number`, `elapsed_seconds`, `pending_jobs[]`
- `pr_checks_gate.query_failed` — WARNING; fields: `pr_number`, `error`. Fail-open path.
- `pr_checks_gate.branch_protection_unreadable` — INFO; fields: `pr_number`. Fallback path taken.

The dashboard's existing card-event timeline can render `hold` events as a yellow waiting bar; no UI work is required for the gate to be operationally visible.

## Rollback Plan

The entire feature gates behind `closer_pr_checks.enabled`. Flipping it to `false` reverts to today's behaviour: closer ignores checks, coordinare transitions to `monitoring_pr` based on closer's approval alone. No state migration required — the optional `checks_state` field is simply unused.
