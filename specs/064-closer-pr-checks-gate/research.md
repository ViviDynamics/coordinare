# Research: Closer PR Checks Gate (064)

All Technical Context items were resolved at spec/plan time; no
`NEEDS CLARIFICATION` markers remain. This file records the rationale for
the non-obvious choices.

## 1. `statusCheckRollup` over separate Checks API + Statuses API

**Decision**: Use the GraphQL `statusCheckRollup` field on the PR's HEAD
commit, combined with `branchProtectionRules` in the same query.

**Rationale**: Bundles the two data sources GitHub itself reconciles for the
PR merge button. Single round-trip, single rate-limit charge. Lets us mirror
exactly what the UI shows reviewers.

**Alternatives considered**:
- REST `GET /repos/{o}/{r}/commits/{sha}/check-runs` + `/status` — two calls,
  manual reconciliation, easy to drift from GitHub UI.
- Webhook-driven state — adds a public endpoint surface and persistence layer
  for a feature that polls fine.

## 2. HTTP-only re-poll during HOLD (no closer re-dispatch)

**Decision**: When the gate returns HOLD, coordinare re-queries the rollup
directly on the next monitor tick. The closer LLM is **not** re-invoked.

**Rationale**: A re-dispatch per tick would tie up the closer slot and block
other cards from advancing through the closer stage — the user explicitly
flagged this risk during clarification. The rollup is fully machine-readable;
no LLM judgement is needed to decide "still pending vs done."

**Alternatives considered**:
- Re-dispatch closer every tick (rejected — bottleneck).
- Background asyncio task per holding card (rejected — adds lifecycle
  complexity for marginal gain over tick-driven polling).

## 3. Fail-open on GraphQL errors

**Decision**: If the rollup query errors (rate-limit, network, auth), log at
WARN and treat as FORWARD by default (`fail_open_on_error: true`).

**Rationale**: Mirrors the 043 lint-gate precedent. Coordinare already
defaults to non-punitive failure modes on infrastructure-side errors so a
GitHub outage doesn't strand every PR. Operators who want strict behaviour
can flip the flag.

## 4. `pending_timeout` anchored to HEAD push, not stage-entry

**Decision**: `pending_timeout_seconds` is measured from the PR HEAD
commit's `pushedDate`, not from when the closer first ran.

**Rationale**: A closer push triggers a fresh CI cycle. Anchoring to push
time means the timeout naturally resets when the closer pushes a fix, and
operators can reason about it as "total CI budget per HEAD."

## 5. `neutral` and `skipped` count as pass; `stale` and `startup_failure` count as fail

**Decision**: Per FR-002 table in data-model.md.

**Rationale**: `neutral` is GitHub's "intentional no-op" — common for
docs-only PRs that legitimately skip test jobs. `skipped` is explicit
conditional skip from workflow YAML, also intentional. `stale` means the
result no longer applies to current HEAD (treat as missing → fail). `startup_failure`
means GitHub couldn't even launch the workflow (treat as fail, not pending —
it's terminal).

## 6. Policy split: pure `decide()` + I/O wrapper

**Decision**: `pr_checks_policy.decide(rollup, config) -> GateDecision` is a
pure function. `pr_checks_service.PrChecksService.get_pr_check_rollup(pr)`
is the only network surface.

**Rationale**: Lets us unit-test every conclusion-mapping permutation and
timeout edge case without mocks. The constitution's Testing Discipline
principle is satisfied trivially. Mirrors the project's broader
interface-first design memory ([[feedback_interface_first_design]]).
