# Feature Specification: Checkpoint Protocol Extensions + Head-Delta Audit Trail

**Branch**: `072-implementer-checkpoint-extensions`
**Date**: 2026-05-24
**Spec depends on**: `070-implementer-commit-floor` (landed; this spec extends it)

## Problem

Spec 070 landed the implementer commit-or-checkpoint contract: `partial_progress`
sentinel + zero-commit guardrail, gated strictly on `role == implementing` /
`stage == implementing`. The gating was deliberately conservative — extending
the protocol to other roles was deferred Out-of-Scope, as was persisting the
HEAD deltas the guardrail relies on. We are now seeing two follow-on gaps in
production:

1. **Other roles can still terminate without commits.** A reviewer that
   uncovers a CI gap, a QA agent that finishes a multi-step verification, or
   a security agent partway through an audit have no checkpoint protocol. Their
   only terminal options are DONE (success) or BLOCKED (operator round-trip).
   Long QA / security turns hit context limits and bail with prose status
   reports, which currently route to `phase=blocked` — the exact failure mode
   070 fixed for implementer.
2. **HEAD deltas are response-scoped only.** `head_before` / `head_after`
   live on `ProtocolResponse` but are never persisted to the snapshot, so
   operators auditing "did this card actually advance the branch over its
   lifetime?" have no record across restarts or across multiple performer
   turns. The information is also unavailable to the conducting LLM when
   making routing decisions on a rehydrated state.

Spec 070 also left three "Manual Validation" items unverified by automated
tests. We replace those with deterministic e2e tests in this spec so the
contract is regression-protected.

## User Stories

### US1 — Non-implementer roles can checkpoint (P1)

**As** a reviewer / qa / security / documenting persona on a long-running turn,
**I want** to emit a `partial_progress` sentinel with `next_focus`,
**so that** the coordinare re-dispatches me with my own continuation hint
instead of routing the operator a spurious blocked notification.

**Independent test**: A reviewer turn that emits a trailing
`{"status": "partial_progress", "next_focus": "..."}` sentinel triggers a
re-dispatch back to `phase=dispatching` with `stage=reviewing`. The relay
feedback contains the `next_focus` verbatim. Reviewer is not handed back to
the operator.

### US2 — Per-role zero-progress guardrail (P1)

**As** an operator,
**I want** the zero-commit guardrail to extend to roles where "no progress"
is unambiguous (reviewer/qa/security/docs do not commit code, but they DO
post review comments / status updates — "zero progress" is "no commits AND
no PR comments AND no clarifications"),
**so that** roles other than implementing can also self-loop rather than
bouncing to the operator on a silent turn.

**Independent test**: A reviewer turn that returns `blocked` with
`head_before == head_after` AND no new PR comments posted by the bot user
AND no new clarifications recorded routes to `dispatching` with a "resume
your review" directive. A reviewer turn with the same head delta but where
the bot posted a PR comment in this turn routes to `blocked` normally
(the review surfaced something real).

### US3 — Head-delta audit trail (P2)

**As** an operator debugging "did this card actually advance?",
**I want** `PersistedSession` to record `head_at_dispatch` and `head_at_last_turn`,
**so that** I can ask `state-tool` or read the snapshot file and see whether the
branch HEAD has moved since this session started, regardless of restart count.

**Independent test**: A snapshot saved mid-session contains both
`head_at_dispatch` (set when the performer was first dispatched for this
session) and `head_at_last_turn` (overwritten on every terminal response).
v1 / v2 snapshots round-trip with both fields defaulting to `None`.

### US4 — Card #70 reproduction is regression-protected (P1)

**As** a maintainer,
**I want** an e2e test that reproduces the card #70 / PR #135 failure
(implementer terminates with "next steps" prose, no commits, no sentinel),
**so that** the 070 guardrail cannot silently regress.

**Independent test**: Drive the LangGraph workflow end-to-end against a
fake performer that emits `{"status": "blocked", "head_before": "X",
"head_after": "X"}`. Final state is `phase=dispatching` with `relay_feedback`
populated; `phase` is never `blocked`.

### US5 — Reviewer blocked still routes to operator (P1, regression)

**As** an operator,
**I want** legitimate reviewer/security/qa blocked verdicts to keep routing
to `phase=blocked`,
**so that** the per-role guardrail (US2) cannot accidentally swallow real
review-surfaced issues.

**Independent test**: A reviewer turn that returns `blocked` AND posts a
new PR comment in the same turn routes to `phase=blocked` (US2 gate does
not trip). Implementer commits + blocked still routes to `phase=blocked`
(070 guardrail does not trip — `head_before != head_after`).

### US6 — partial_progress PR comment is human-readable (P2)

**As** an operator scanning a PR conversation,
**I want** the `[partial_progress]` PR comment posted by the performer to
be human-readable and clearly distinguishable from the agent's prose
output,
**so that** I can tell at a glance "the agent checkpointed here" without
parsing JSON.

**Independent test**: Performer emits a sentinel; the GitHub comment
posted starts with `[partial_progress]`, contains the `comment` field
verbatim, and does NOT contain the literal `next_focus` JSON field
(operator-facing prose only — the focus relays through state, not the
comment).

## Functional Requirements

### Protocol extensions

- **FR-072-1** `partial_progress` sentinel parsing extends to roles
  `reviewing`, `security`, `qa`, `documenting`. Implementation remains a trailing
  JSON sentinel; gating expands from `role == "implementing"` to
  `role in {"implementing", "reviewing", "security", "qa", "documenting"}`.
- **FR-072-2** On `partial_progress` from any extended role, the performer
  MUST push any uncommitted changes (no-op for review-only roles that have
  no diff) and post a PR comment `[partial_progress] {comment}` when a PR
  exists.
- **FR-072-3** Coordinare `monitor_performer` `partial_progress` branch
  routes back to `phase=dispatching` with `performer_stage` preserved as
  the role that emitted the sentinel — not coerced to `implementing`.
- **FR-072-4** Non-extended roles (e.g. `assessing`, `closing`,
  `architect`) MUST NOT honor the sentinel — emitting one is treated as
  prose and ignored. The gate is an allow-list, not a deny-list.

### Per-role zero-progress guardrail

- **FR-072-5** For `stage in {"reviewing", "security", "qa", "documenting"}`, the
  zero-progress guardrail trips when ALL of:
  (a) `head_before == head_after`,
  (b) no new PR comments authored by the bot user in this turn,
  (c) no new clarifications appended in this turn.
  When tripped, route to `phase=dispatching` with a role-appropriate relay
  feedback ("resume your review", "resume your QA pass", etc.).
- **FR-072-6** "New PR comments in this turn" is detected by comparing the
  performer's post-turn comment list against the pre-turn count for the
  same author. The performer surfaces this delta on `ProtocolResponse`.
- **FR-072-7** The implementer guardrail (FR-070-7) remains unchanged in
  behavior — its single-condition trip (head delta only) is correct for
  the implementer because commits ARE the progress signal.

### Head-delta audit trail

- **FR-072-8** `PersistedSession` gains two optional datetime-agnostic
  string fields: `head_at_dispatch: str | None = None` and
  `head_at_last_turn: str | None = None`. Both default to `None`; v1 / v2
  snapshots round-trip without loss.
- **FR-072-9** `head_at_dispatch` is written once, on the first terminal
  `ProtocolResponse` from `monitor_performer` that carries a non-empty
  `head_before` for this card's current session. It is sticky thereafter
  (subsequent turns do not overwrite it), so it captures the branch HEAD
  at the moment the performer began work on this card.
- **FR-072-10** `head_at_last_turn` is overwritten on every terminal
  `ProtocolResponse` that carries a non-null `head_after`.
- **FR-072-11** Head fields are mirrored from session into top-level
  state for single-card legacy paths, matching the established
  `last_blocked_notified_at` mirror pattern.

### Test coverage (replaces 070 Manual Validation)

- **FR-072-12** New e2e test `tests/integration/test_072_implementer_no_commit_reproduction.py`
  drives the workflow against a fake performer simulating card #70 (no
  commits, blocked verdict). Asserts `phase=dispatching` (not `blocked`)
  and `relay_feedback` populated.
- **FR-072-13** New e2e test `tests/integration/test_072_role_checkpoint_protocol.py`
  exercises reviewer + qa partial_progress + per-role guardrail in a
  single multi-turn workflow.
- **FR-072-14** New e2e test asserting reviewer `blocked` with a posted
  PR comment routes to `phase=blocked` (regression guard on FR-072-5).

## Out of Scope

- Architect / assessing / closing roles — these are short single-turn
  roles where checkpointing doesn't apply.
- Persisting historical head deltas (snapshot stores only the current
  pair, not a list).
- UI surfacing of head deltas in CLI tools — separate spec if needed.
- Migrating the implementer guardrail to the broader 3-signal model;
  FR-072-7 explicitly keeps it as-is.

## Success Criteria

- A reviewer turn that hits a context limit checkpoints + re-dispatches
  without operator involvement.
- A reviewer turn that legitimately finds a blocker still reaches
  `phase=blocked`.
- Snapshots saved on any branch contain a head-delta audit trail visible
  via `state-tool`.
- Card #70 reproduction is now a deterministic test.
- Full coordinare + performer test suites green, no regressions in 070
  or 069 tests.
