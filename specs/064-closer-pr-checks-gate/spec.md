# Feature Specification: Closer PR-Checks Gate

**Feature Branch**: `064-closer-pr-checks-gate`
**Created**: 2026-05-16
**Status**: Draft
**Input**: User instruction: "The closer should absolutely make sure that CI checks have passed before handing the PR off to the human reviewer."

## Clarifications

### Session 2026-05-16

- Q: When required checks are still pending/in-progress at closer time, how should the closer signal that in its JSON verdict? → A: Reuse `approved=false` and add an explicit `status: "pending_checks"` field that the coordinare inspects to choose hold-vs-bounce; no other performer interprets the field.
- Q: How should the closer and coordinare-side gate treat non-success/non-failure check conclusions (`neutral`, `skipped`, `stale`, `startup_failure`)? → A: Treat `neutral` and `skipped` as pass; treat `stale` and `startup_failure` as refusal (alongside the FR-002 set).
- Q: What anchors the `pending_timeout` clock? → A: The PR's current HEAD-commit push timestamp. New pushes naturally reset the budget; daemon restarts don't reset the wait.
- Q: Which GitHub API surface defines the check set the gate consults? → A: The combined `statusCheckRollup` (Check Runs + commit Statuses), matching what humans see in the PR UI and what branch protection enforces.
- Q: While a card is held in `pending_checks`, does each monitor tick re-dispatch the closer or only re-poll the rollup? → A: HTTP-only re-poll. Closer only re-dispatches after checks resolve (gate forwards to `monitoring_pr` on success, or bounces to implementer on failure). Avoids per-tick LLM cost; card holds passively so the closer slot stays free for other cards.

## Background

Spec 043 (Performer CI Ownership) established that any performer that
commits and pushes code must verify the repo's CI-equivalent checks
locally before handing control back to the coordinare, and added a
coordinare-side gate (`_advance_stage` in `monitor_performer.py:426–476`)
that runs the detected lint command against the workspace before
transitioning to `monitoring_pr`.

That gate closes the obvious gap — a performer claiming green when its
diff is red — but it only checks **local** lint against the workspace
checkout. The actual **GitHub Actions check-runs** on the PR can still
be failing, pending, or never-started when the card transitions to
`IN_REVIEW`. Local CI passing ≠ remote CI green: secrets, services,
OS/matrix differences, integration jobs, and any non-default workflow
file produce verdicts the local gate never sees.

The closer is the last bot stage before a human takes the PR. Its
persona today (`persona_service.py:234–267`) instructs it to verify
prior review-thread resolution and to run the linter on changed files
(`_CI_REVIEWER_DIRECTIVE`). It does NOT instruct it to check the PR's
GitHub Actions check-run status, and the coordinare does not gate the
`monitoring_pr` transition on remote check status either.

The visible failure mode: a PR lands in `monitoring_pr` for human
review with red required checks that no bot stage ever consulted.
The human reviewer is the first party to notice — wasting a review
slot and undermining the value proposition of the lifecycle.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Closer Refuses To Approve When Required Checks Are Failing (Priority: P1)

A PR has finished the implementer/reviewer/security/qa/tech_writer
chain. All prior review threads have been resolved. The closer
dispatches, reads the PR's check-run status via `gh pr checks`
(or the equivalent GraphQL query), discovers that the `rspec`
workflow on `main` is failing, and returns `approved=false` with
a comment naming the failed job(s) and a tail of the failing log.
The card is routed back to the implementer with the check-failure
context in `relay_feedback`. The card does NOT transition to
`monitoring_pr`.

**Why this priority**: This is the core ask. Without it, the closer
remains the same persona it is today and the failure mode persists.

**Independent Test**: Run the closer against a fixture PR with an
intentionally failing required check (e.g. a workflow that runs
`exit 1`). The closer's JSON verdict must have `approved=false`,
its `comments` must reference the failing job by name, and the
card must transition back to `implementing`, not `monitoring_pr`.

**Acceptance Scenarios**:

1. **Given** a PR whose required GitHub Actions checks are all
   `success`, **When** the closer runs, **Then** the closer's
   pre-existing thread-resolution logic decides approval, and a
   `success` check status does not by itself force approval.
2. **Given** a PR with at least one required check in `failure`
   state, **When** the closer runs, **Then** it returns
   `approved=false` with the failed job names in `comments` and
   the coordinare routes the card back to the implementer.
3. **Given** a PR with required checks still `pending` or
   `in_progress`, **When** the closer runs, **Then** it returns
   `approved=false` with `status: "pending_checks"`, and the
   coordinare branches on that field to hold the card and re-poll
   rather than approve or bounce.

---

### User Story 2 — Coordinare-Side Gate Catches A Closer That Approves A Red PR (Priority: P1)

The closer hallucinates a green verdict, or a check transitions
to `failure` between the closer's read and the `_advance_stage`
call. Before transitioning the card to `monitoring_pr`, the
coordinare itself queries the PR's check-run status. If any
required check is `failure`, the transition is blocked and the
card is routed back to `implementing` with the failure context
in `relay_feedback`. If any required check is still pending, the
coordinare holds the card in its current stage and re-polls on the
existing performer-monitor tick.

**Why this priority**: Defence-in-depth. 043's local lint gate
already follows this pattern; without the remote analogue, a
closer mistake (or a model that simply ignores the directive) is
unrecoverable until a human notices.

**Independent Test**: Inject a closer verdict of `approved=true`
against a fixture PR whose required checks include a `failure`
state. The coordinare must NOT transition `phase` to
`monitoring_pr`; it must route back to `implementing` with
`relay_feedback` containing the failing job name(s).

**Acceptance Scenarios**:

1. **Given** a closer verdict of `approved=true` and remote
   required checks all `success`, **When** `_advance_stage`
   runs, **Then** the card transitions to `monitoring_pr` as
   today.
2. **Given** a closer verdict of `approved=true` and at least
   one required check in `failure`, **When** `_advance_stage`
   runs, **Then** the card is routed back to `implementing`
   with check-failure feedback and does NOT transition.
3. **Given** a closer verdict of `approved=true` and at least
   one required check still `pending`, **When** `_advance_stage`
   runs, **Then** the card stays in its current stage; the next
   performer-monitor tick re-evaluates.

---

### User Story 3 — Bounded Wait For Pending Checks (Priority: P2)

A PR's checks are still in-progress when the closer dispatches.
Rather than approve-and-pray or bounce immediately, the system
waits (bounded) for the checks to resolve. The wait timeout is
configurable per symphony with a sensible default (e.g. 15 minutes).
If checks don't resolve within the timeout, the card is routed
back to the implementer with a "checks did not complete in time"
note in `relay_feedback`.

**Why this priority**: Lower than P1 because the simpler behaviour
(bounce on pending, let the next implementer cycle re-trigger) is
acceptable for v1; the bounded wait is a UX refinement.

**Independent Test**: Inject a PR whose checks remain `pending`
for longer than the configured timeout. Verify the card transitions
back to `implementing` with the timeout message, NOT to
`monitoring_pr`.

## Functional Requirements

- **FR-001**: The closer persona MUST direct the backend to read
  the PR's combined check status via the GraphQL
  `statusCheckRollup` (or equivalent `gh pr checks` output)
  before producing its verdict. This surface fuses Check Runs
  (GitHub Actions, modern apps) and commit Statuses (legacy
  third-party CI) into one normalized list, matching what the
  PR UI shows and what branch protection enforces.
- **FR-002**: The closer MUST refuse to approve (i.e. return
  `approved=false`) when any required check is in `failure`,
  `cancelled`, `timed_out`, `action_required`, `stale`, or
  `startup_failure` state. Conclusions `success`, `neutral`, and
  `skipped` count as pass. `queued` / `in_progress` (no
  conclusion yet) are treated under FR-005's pending path, not
  here.
- **FR-003**: The closer's `comments` field on a check-failure
  refusal MUST name the failing job(s) and include enough context
  (failing step, log tail, or workflow file) for the next
  implementer pass to act without re-querying GitHub.
- **FR-004**: The coordinare MUST independently query the PR's
  required-check status in `_advance_stage` immediately before
  transitioning `phase` to `monitoring_pr`. On any required-check
  failure, the coordinare MUST route the card back to `implementing`
  with the failure context in `relay_feedback`, and MUST NOT
  perform the transition.
- **FR-005**: On pending required checks at gate time, the
  coordinare MUST hold the card in its current stage rather than
  transition to `monitoring_pr` or bounce. The closer signals
  pending via `approved=false` + `status: "pending_checks"`; the
  coordinare branches on that field rather than treating it as a
  normal bounce. While held, each performer-monitor tick MUST
  re-poll the rollup via HTTP only — the closer MUST NOT be
  re-dispatched. The closer re-dispatches only when checks
  resolve: on success the gate forwards to `monitoring_pr`; on
  failure (or `pending_timeout` exhaustion) the gate bounces to
  the implementer, after which the next normal lifecycle cycle
  will re-dispatch the closer.
- **FR-006**: "Required check" is determined by the PR's branch
  protection rules. If branch protection cannot be read (token
  permissions, public-repo case), the system MUST fall back to
  the operator-configured `treat_unknown_required_as` policy
  (default `"pass"` — treat no checks as required, gate forwards
  on auth degradation). Operators who want a stricter posture MAY
  set `"block"` to treat ALL non-skipped check-runs as required.
  Default is `"pass"` so the gate remains consistent with FR-007's
  fail-open infrastructure-error posture; a stale auth token should
  not strand every PR.
- **FR-007**: The coordinare-side gate MUST NOT block on its own
  execution errors (network, auth) — log and proceed to
  `monitoring_pr`, mirroring 043's lint-gate behaviour. The
  closer-side directive is the primary signal; the gate is
  defence-in-depth, not a hard dependency.
- **FR-008**: Symphony config MUST expose a per-symphony
  `closer_pr_checks` block with at minimum a `pending_timeout`
  (default 900s) and an `enabled` flag (default true). Disabling
  is the escape hatch for repos with intentionally-slow or
  externally-triggered checks.
- **FR-009**: The `pending_timeout` clock MUST be anchored to
  the timestamp of the PR's current HEAD commit (the push time
  reported by GitHub). The coordinare MUST re-derive this anchor
  on each evaluation so that a new push to the PR resets the
  wait budget, and so a daemon restart does not reset it.

## Out of Scope

- Replacing the closer persona entirely with a deterministic
  check-status reader (the closer still owns thread-resolution
  verification).
- Re-running failing checks from the coordinare (use the existing
  `watch-ci` skill / human action). The gate REPORTS failures,
  it does not retry them.
- Health-check-style status outside of GitHub (e.g. dashboards,
  external monitors that do not post back to the PR). Anything
  that surfaces through `statusCheckRollup` — Check Runs or
  commit Statuses, from GitHub Actions or any third-party CI —
  participates automatically.
- Fixing red PRs already sitting in `monitoring_pr` when this
  ships. This spec prevents the failure mode going forward.

## Success Criteria

- [ ] A fixture PR with a failing required check cannot reach
      `monitoring_pr` via the bot lifecycle — either the closer
      refuses, or the coordinare gate refuses, or both.
- [ ] The closer's verdict on a check-failure case names the
      failing job(s) and produces actionable `relay_feedback`.
- [ ] Pending-check handling holds the card without bouncing for
      transient in-progress states.
- [ ] At least one integration test verifies the coordinare-side
      gate triggers on a synthesized failed check-run.
- [ ] The closer persona's directive and the coordinare gate are
      both covered by unit tests against mocked `gh` / GraphQL
      responses.

### Performance Budgets

- [ ] **Gate latency**: The coordinare-side gate (`_advance_stage`
      rollup query + policy evaluation) MUST add ≤ 500ms p95 to
      the existing transition time, measured against a real PR
      with ≤ 20 check entries.
- [ ] **Per-tick cost during HOLD**: While a card is held in
      `pending_checks`, each monitor tick MUST issue **at most 1**
      GraphQL rollup query (no closer LLM call). The tick fast-path
      (skip if `last_polled_at` within `poll_interval_seconds`)
      MUST reduce this to 0 on subsequent fast ticks.
- [ ] **No cost regression on the happy path**: When all required
      checks are already green at gate time, total added cost is
      1 GraphQL query — no retries, no closer re-dispatch.

## Files Likely to Change

| File | Change |
|---|---|
| `src/coordinare/services/persona_service.py` | Add a closer-specific PR-checks directive alongside `_CI_REVIEWER_DIRECTIVE` |
| `src/coordinare/services/pr_checks_service.py` (new) | I/O wrapper: `PrChecksService.get_pr_check_rollup(pr_number) -> CheckRollup`. Single GraphQL round-trip bundling `statusCheckRollup` + `branchProtectionRules`. |
| `src/coordinare/services/pr_checks_policy.py` (new) | Pure-function policy: `decide(rollup, *, pending_timeout_seconds, treat_unknown_required_as, now) -> GateDecision`. Trivially unit-testable. |
| `src/coordinare/graph/nodes/monitor_performer.py` | Extend the `_advance_stage` pre-transition gate (~line 426) with a remote-checks query mirroring the 043 lint gate |
| `src/coordinare/config/symphony.py` (or wherever symphony config lives) | Add the `closer_pr_checks` block |
| `tests/unit/services/test_persona_service.py` | Verify closer persona includes the new directive |
| `tests/unit/graph/nodes/test_monitor_performer.py` | New test cases for failed / pending / passing required checks |

## Open Questions

*(All open questions resolved during analysis — see Clarifications
session 2026-05-16 and Performance Budgets above.)*

1. ~~**Required-check fallback**~~: **Resolved.** FR-006 now defers
   to operator-configured `treat_unknown_required_as` (default
   `"pass"`), matching FR-007's fail-open posture on infra errors.
2. ~~**Cost / bounded-wait loop multiplication**~~: **Resolved.**
   Per-tick cost is bounded by the Performance Budgets above:
   ≤1 GraphQL call per tick during HOLD, 0 on fast-path ticks
   inside `poll_interval_seconds`, no closer LLM re-dispatch.
