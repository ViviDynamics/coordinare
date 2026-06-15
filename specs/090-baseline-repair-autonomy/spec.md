# Feature Specification: Baseline Repair Autonomy

**Feature Branch**: `090-baseline-repair-autonomy`  
**Created**: 2026-06-13  
**Status**: Draft  
**Input**: User description: "The reality is that if coordinare and its performers are not smart enough to correct a failing test that might appear to be out of scope of the issue, then it isn't going to be a useful tool." Coordinare must autonomously FIX out-of-scope / inherited (baseline) test failures rather than self-blocking and giving up — but only through a reviewable change that cannot weaken or remove a test.

## Clarifications

### Session 2026-06-13

- Q: Where does an autonomous baseline repair land? → A: On the card's existing active branch, flowing through its open PR and the normal human-approved review — no separate branch or PR.
- Q: How is a "fix" prevented from simply weakening the test? → A: A dual test-integrity guard runs on every candidate repair: a static heuristic (deleted/loosened assertions, loosened comparisons/operators, conditional or exception-suppressed assertions, added skip/xfail/disable, mocked-away requirements, removed setup/teardown) AND an independent **automated** adversarial reviewer pass. Either one flagging a weakening forces escalation to a human. Both guards are conservative: when they cannot confidently clear a change, they reject rather than pass.
- Q: Is the "adversarial reviewer" in the guard the same as the human approval? → A: No. The adversarial reviewer is an automated, independent judging pass (separate context from the agent that wrote the repair) — part of the autonomous guard. It is distinct from, and in addition to, the final human approval that every landed repair still requires.
- Q: How does coordinare tell an inherited failure apart from one the card introduced? → A: By comparing the head's failing checks against the merge-base baseline using a stable failure signature `(name, conclusion, normalized-reason)`. A check failing on the baseline for the same reason is INHERITED; a check that newly fails, or fails for a *different* reason than the baseline, is INTRODUCED.
- Q: What about checks that time out / are cancelled rather than fail outright? → A: Only stable failures (a definitive "failed" conclusion) participate in INHERITED classification. Transient conclusions (timed-out, cancelled, neutral, skipped) on either the baseline or the head are treated as flake/non-stable, are never labeled INHERITED, and are excluded from autonomous repair.
- Q: What happens when the baseline cannot be fetched (API error/timeout/ambiguous merge-base)? → A: Fail safe to today's behavior — treat all failures as the card's own (no inherited classification, no autonomous repair), and let the existing gate proceed unchanged. A degraded baseline must never grant autonomy it cannot justify. Repeated/systemic baseline-fetch failure must raise an operator-visible signal rather than degrade silently.
- Q: Does an accepted autonomous repair merge on the strength of the guard? → A: Never. The repair commit lands on the active branch, invalidates any prior approval, and requires a *fresh* human approval before merge. Coordinare does not auto-merge on the guard's verdict.
- Q: How is the feature rolled out? → A: Phased and config-gated. Layer 1 (prevention) ships first and is independently valuable; Layer 2 (classification) is observe-only until trusted; Layer 3 (repair) is opt-in per persona scope and bounded by an attempt budget.

## User Scenarios & Testing *(mandatory)*

This feature is delivered in three independently shippable layers, ordered so each one is valuable on its own and de-risks the next. Layer 1 prevents the most dangerous failure mode (merging on top of a broken base). Layer 2 makes the system able to *explain* a failure's origin without acting on it. Layer 3 uses that explanation to act — autonomously repairing inherited breakage behind a guard that refuses to let a test be weakened.

### User Story 1 - Refuse to merge onto a red base (Priority: P1)

A maintainer has a card whose PR is approved and whose own checks are green. The PR's base branch (e.g. `main`) currently has a *required* check failing — something landed broken, unrelated to this card. Today, coordinare would merge anyway, compounding the breakage. With this story, coordinare refuses to advance the merge while the base branch's required checks are red, surfaces *which* base check is red and why, and holds (re-evaluating) rather than merging — unless the base state cannot be determined, in which case it falls safe to current behavior.

**Why this priority**: This is the highest-severity, lowest-complexity slice. Merging onto a known-red base actively makes the repository worse and is the one failure mode with no upside. It needs no classification, no repair, and no new agent dispatch — only a read of the base branch's check rollup at the existing merge decision point. It is shippable and valuable with zero autonomy.

**Independent Test**: Stand up a PR that is approved and mergeable (head green) whose base branch has a required check in `failure`. Confirm coordinare does NOT merge, records a structured "base-not-green" hold with the offending check name/URL, and re-evaluates rather than terminating. Then flip the base check to `success` and confirm the merge proceeds. Separately, force the base-rollup fetch to error and confirm coordinare falls back to current merge behavior (does not block on an indeterminate base).

**Acceptance Scenarios**:

1. **Given** an approved, head-green PR whose base branch has a required check failing, **When** coordinare reaches the merge decision, **Then** it does not merge, emits a "base-not-green" decision naming the failing base check, and holds for re-evaluation.
2. **Given** the same PR after the base check turns green, **When** coordinare re-evaluates, **Then** the merge proceeds normally.
3. **Given** the base-branch check rollup cannot be fetched (API error/timeout/ambiguous merge-base), **When** coordinare reaches the merge decision, **Then** it logs the degraded read and proceeds with today's head-only merge behavior (fail-safe, never silently blocks forever).
4. **Given** the base branch has only *non-required* checks failing, **When** coordinare reaches the merge decision, **Then** the merge is not blocked on those (the gate keys off the base's required set, mirroring head-side policy).

---

### User Story 2 - Classify a failure's origin (inherited / introduced / flake) (Priority: P1)

When a card's head CI shows failing checks, coordinare compares them against the merge-base baseline and labels each failing check as INHERITED (already failing on the baseline for the same reason), INTRODUCED (new failure, or a baseline check now failing for a *different* reason), FLAKE (transient/non-reproducing per existing flake handling, including timed-out/cancelled), or UNKNOWN (baseline indeterminate). The classification is recorded on the gate decision and surfaced in observability. In this story the classification is **observe-only**: it changes no routing or verdict — it produces the labeled evidence that Layer 3 and human reviewers will rely on.

**Why this priority**: Correct classification is the safety foundation for any autonomous repair. The dominant risk in the whole feature is *regression-masking* — mistaking a card-introduced regression for inherited breakage and "repairing" it by accommodating the regression. The signature deliberately keys on the failure's *reason* (a normalized reason derived from stable check metadata), not just its name, so that a check which fails for a new reason after the diff reclassifies as INTRODUCED. Shipping classification observe-only lets us validate accuracy against real cards before any action is taken on it.

**Independent Test**: Construct a head with a stable failing check that also fails identically on the merge-base → assert it is labeled INHERITED. Construct a head where a previously-green base check now fails → INTRODUCED. Construct a head where a baseline-failing check now fails with a *different* reason → INTRODUCED (anti-masking). Construct a head failure whose baseline counterpart only timed-out/was-cancelled (transient) → NOT INHERITED. Force the baseline fetch to error → assert everything is labeled UNKNOWN (no inherited labels) and no downstream behavior changes. Load a pre-feature persisted snapshot and confirm it loads with the new classification fields defaulted and behavior unchanged. Verify the labels appear in the persisted gate decision and emitted events, and that no routing/verdict differs from today.

**Acceptance Scenarios**:

1. **Given** a head check with a definitive "failed" conclusion whose `(name, conclusion, normalized-reason)` matches a stable failure on the merge-base, **When** classification runs, **Then** it is labeled INHERITED.
2. **Given** a head check that is failing but was passing (or absent) on the merge-base, **When** classification runs, **Then** it is labeled INTRODUCED.
3. **Given** a check that fails on both head and merge-base but with a *different* normalized reason (different cause), **When** classification runs, **Then** it is labeled INTRODUCED, not INHERITED (anti-masking).
4. **Given** a head failure whose baseline counterpart has a transient conclusion (timed-out/cancelled/neutral/skipped) or is itself flaky, **When** classification runs, **Then** the head check is NOT labeled INHERITED (it is INTRODUCED or FLAKE) and is excluded from repair eligibility.
5. **Given** the baseline rollup cannot be fetched, **When** classification runs, **Then** no check is labeled INHERITED, the degradation is recorded, and downstream routing/verdict is identical to today.
6. **Given** any classification result in this layer, **When** the gate decision is produced, **Then** routing and verdict are unchanged from current behavior (observe-only), and the labels are present in persisted state and observability.

---

### User Story 3 - Guarded autonomous repair of inherited breakage (Priority: P2)

When classification (US2) identifies failures that are INHERITED — breakage the card did not cause — coordinare may dispatch a bounded, autonomous repair on the card's *existing active branch*. The repair instruction mandates fixing the underlying code/config so the inherited check passes, and explicitly forbids passing the check by weakening tests. Every candidate repair is gated by a **dual test-integrity guard**: a static heuristic and an independent automated adversarial reviewer pass. If either guard finds the repair would weaken or remove a test — or cannot confidently clear it — the change is rejected and the situation is escalated to a human via a visible, actionable signal. The repair commit lands on the active branch, invalidates any prior approval, and flows through the card's normal human review and merge — it is never auto-merged on the strength of the guard. Repairs are bounded by a per-head attempt budget; exhaustion escalates to a human rather than looping.

**Why this priority**: This is the payoff of the governing intent — making coordinare able to fix out-of-scope failures instead of giving up — but it is correctly last because it depends entirely on US2's classification being trustworthy and on the guard being sound. It is the highest-complexity, highest-risk slice (it dispatches code changes), so it ships opt-in per persona scope, bounded, and gated, after Layers 1–2 are validated in production.

**Independent Test**: With repair enabled for a persona scope, present a card whose head has an INHERITED stable failing check fixable in code. Confirm coordinare dispatches one repair attempt on the existing branch with a scope instruction that mandates a code fix and forbids test-weakening. Feed the guard candidate diffs that (a) remove an assertion, (b) add `xfail`/`skip`, (c) loosen a comparison operator, (d) wrap an assertion in a swallowing `try/except`, (e) mock away the asserted requirement — confirm each is rejected and escalated (no push, no merge), with at least the static heuristic flagging (a)–(c) and (e). Feed the guard a clean code-only fix and confirm it passes both guards, the commit lands on the branch, prior approval is invalidated, and it awaits a fresh human approval (not auto-merged). Exhaust the attempt budget and confirm a visible escalation rather than further dispatch.

**Acceptance Scenarios**:

1. **Given** an INHERITED stable failing check and repair enabled for the scope, **When** coordinare acts, **Then** it dispatches at most one repair attempt against the card's existing branch with an instruction mandating a code/config fix and prohibiting test-weakening.
2. **Given** a candidate repair that deletes/loosens an assertion, loosens a comparison, conditionally or exception-suppresses an assertion, adds `skip`/`xfail`, removes setup/teardown, or mocks away the asserted requirement, **When** the test-integrity guard runs, **Then** the static heuristic flags it, the repair is rejected, and the card is escalated to a human (no push, no merge).
3. **Given** a candidate repair the static heuristic passes but the automated adversarial reviewer judges to be a test-weakening — or that the static heuristic cannot confidently clear — **When** the guard runs, **Then** the repair is still rejected and escalated (either guard can veto; uncertainty rejects).
4. **Given** a clean code-only repair that makes the inherited check pass without touching test strength, **When** the guard runs, **Then** the repair is accepted and the commit lands on the active branch; the commit invalidates any prior approval and the PR awaits a fresh human approval before merge — coordinare does NOT auto-merge it.
5. **Given** repeated repair attempts on the same head, **When** the per-head attempt budget is exhausted, **Then** coordinare raises a visible, actionable escalation to a human and stops dispatching further repairs for that head.
6. **Given** an INTRODUCED, FLAKE, or UNKNOWN failure (not INHERITED), **When** coordinare evaluates repair, **Then** it does NOT dispatch an autonomous baseline repair (introduced regressions remain the card's own responsibility via existing flows).
7. **Given** repair is disabled for the persona scope (default), **When** an INHERITED failure occurs, **Then** behavior is exactly Layers 1–2 (classify + observe/prevent) with no repair dispatch.

---

### Edge Cases

- **Indeterminate baseline**: base/merge-base rollup fetch errors, times out, or the merge-base is ambiguous/unresolvable → fail safe. No INHERITED labels, no repair, existing gate behavior preserved. The degradation is logged but never blocks indefinitely (US1 #3, US2 #5). Repeated/systemic baseline-fetch failure raises an operator-visible signal so degradation cannot be weaponized (e.g. rate-limit exhaustion) unnoticed (FR-006, FR-027).
- **Regression that resembles inherited breakage**: a check fails on both head and baseline but for a *different reason* → classified INTRODUCED via the reason-sensitive signature, so it is never auto-repaired as inherited (US2 #3). This is the core anti-masking guarantee.
- **Transient conclusion mismatch**: a check that times-out/cancels on one side and fails on the other does not match as a stable inherited failure; transient conclusions never produce INHERITED (US2 #4), preventing both false-INHERITED repairs on flaky baselines and false-INTRODUCED churn.
- **Flaky baseline**: a check that is non-deterministic on the merge-base must not anchor an INHERITED label; a corresponding head failure is conservatively INTRODUCED or UNKNOWN (no repair), so baseline flakiness can never suppress investigation of a real regression.
- **Repair that only passes by neutering the test**: caught by the dual guard; rejected and escalated, never pushed (US3 #2, #3).
- **Guard cannot decide**: if the static heuristic encounters a pattern it cannot confidently classify (e.g. a custom assertion framework), it does not pass by default — it rejects/escalates (conservative bias).
- **Guard false-positive on a legitimate refactor**: if the guard rejects a genuine, safe change (e.g. a test was *correctly* removed because the feature was removed), the result is escalation to a human, not silent acceptance — erring toward human review is the intended bias.
- **Accepted repair must be re-reviewed**: the repair commit invalidates the PR's prior approval and requires fresh human approval; it is never merged on the guard's verdict alone (US3 #4).
- **Silent escalation forbidden**: every "escalate to human" path produces a visible, actionable signal (surfaced on the card/PR and a human-review phase) — never log-only — so a stalled card is never invisible (FR-024).
- **Base advances mid-flight**: the base branch turns green (or red) between the hold and re-evaluation → the gate re-reads on each evaluation; it is not latched (US1 #2).
- **Attempt-budget exhaustion**: bounded per head; exhaustion escalates rather than loops (US3 #5), mirroring the existing local-fix / bounce budgeting. A new head (new commit) starts a fresh budget.
- **Non-required base failures**: base failures outside the required set do not block the merge (US1 #4); only required-check breakage gates.
- **Backward compatibility**: a pre-feature persisted snapshot loads with the new fields (classification labels, repair counter) defaulted to safe empty values and behaves identically to before (FR-026, SC-009).

## Requirements *(mandatory)*

### Functional Requirements

**Layer 1 — Merge-precondition prevention gate**

- **FR-001**: Coordinare MUST evaluate the merge-base / base-branch required-check state at the merge decision point, before advancing an approved, head-green PR to merge.
- **FR-002**: Coordinare MUST NOT advance a PR to merge while a *required* check on its base branch is in a failing conclusion; it MUST instead hold and re-evaluate on subsequent cycles.
- **FR-003**: When blocking on a red base, coordinare MUST record a structured decision identifying the offending base check(s) by name and link, distinct from any head-side failure record.
- **FR-004**: Coordinare MUST NOT block the merge on *non-required* base-branch check failures.
- **FR-005**: If the base-branch / merge-base check state cannot be determined (API error, timeout, ambiguous base), coordinare MUST fall back to current head-only merge behavior (fail-safe) and record the degraded read; it MUST NOT block indefinitely on an indeterminate base.
- **FR-006**: The Layer 1 gate MUST re-read base state on each evaluation (not latch a prior result), so a base that turns green proceeds and one that turns red holds.

**Layer 2 — Failure-origin classification**

- **FR-007**: For each failing head check, coordinare MUST compute a stable failure signature from `(check name, conclusion, normalized reason)` and compare it against the merge-base baseline's failing checks. The normalized reason MUST be derived deterministically from stable check metadata (e.g. the check's summary/title) such that the same underlying failure yields the same reason and a genuinely different failure yields a different reason; benign wording drift (timestamps, line numbers, run IDs) MUST NOT change it. The exact normalization algorithm is an implementation concern for the plan, but it MUST satisfy these stability and reason-sensitivity properties.
- **FR-008**: Coordinare MUST classify a failing head check as INHERITED only when it is a *stable failure* (a definitive "failed" conclusion) whose signature matches a stable failure with the same signature in the merge-base baseline.
- **FR-009**: Coordinare MUST classify a failing head check as INTRODUCED when it has no matching baseline signature — including when it fails for a *different reason* than on the baseline. This anti-masking rule is mandatory.
- **FR-010**: Transient conclusions (timed-out, cancelled, neutral, skipped) on either the head or the baseline MUST NOT produce an INHERITED label; such checks are treated as FLAKE/non-stable and excluded from repair eligibility.
- **FR-011**: Coordinare MUST integrate existing flake handling so transient/non-reproducing failures are classified FLAKE and excluded from INHERITED and from repair. If a baseline check is itself flaky/non-deterministic, a corresponding head failure MUST NOT be classified INHERITED; it is conservatively INTRODUCED or UNKNOWN.
- **FR-012**: When the baseline cannot be determined, coordinare MUST label affected checks UNKNOWN (treated as the card's own), apply no INHERITED labels, and leave downstream routing/verdict unchanged.
- **FR-013**: Classification results (per-check labels and the signatures used) MUST be persisted on the gate decision / session state and emitted to observability.
- **FR-014**: In the Layer 2 (observe-only) rollout stage, classification MUST NOT alter routing or verdict relative to current behavior; it only adds evidence.

**Layer 3 — Guarded autonomous repair**

- **FR-015**: Autonomous baseline repair MUST be opt-in per persona scope and disabled by default; when disabled, behavior is exactly Layers 1–2.
- **FR-016**: Coordinare MUST only dispatch an autonomous baseline repair for failures classified INHERITED (never INTRODUCED, FLAKE, or UNKNOWN).
- **FR-017**: A baseline repair MUST be dispatched against the card's existing active branch (and therefore its open PR), not a separate branch or PR.
- **FR-018**: The repair instruction MUST mandate fixing the underlying code/config to make the inherited check pass and MUST explicitly forbid making the check pass by weakening, skipping, deleting, mocking away, or otherwise loosening tests.
- **FR-019**: Every candidate repair MUST pass a dual test-integrity guard before it is accepted: (a) a static heuristic that detects, at minimum, deleted assertions, loosened assertions and comparison operators, assertions made conditional or suppressed by exception handling, added `skip`/`xfail`/`disable` markers, removed setup/teardown, and tests mocked away from their asserted requirement; and (b) an independent **automated** adversarial reviewer pass (separate context from the agent that produced the repair) that judges whether the change weakens test coverage. Both guards MUST be conservative: a change the guard cannot confidently clear is treated as a weakening.
- **FR-020**: If EITHER guard flags a test-weakening (or cannot confidently clear the change), coordinare MUST reject the repair and escalate to a human; it MUST NOT push or merge a guard-rejected repair.
- **FR-021**: An accepted repair commit MUST invalidate any prior PR approval and require a fresh human approval before merge; coordinare MUST NOT auto-merge a repair on the strength of the guard alone. The dual guard governs whether a repair may *land as a candidate*, never whether it may *merge*.
- **FR-022**: Repairs MUST be bounded by a per-head attempt budget (keyed on the head commit, parallel to existing bounce / local-fix counters); a new head commit starts a fresh budget. On exhaustion coordinare MUST escalate to a human and stop dispatching further baseline repairs for that head.
- **FR-023**: All repair decisions (dispatch, guard verdicts, acceptance/rejection, escalation, budget state) MUST be recorded in persisted state and observability for auditability.
- **FR-024**: Every escalation path (guard rejection, budget exhaustion, indeterminate-but-required human judgment) MUST produce a visible, actionable signal — surfaced on the card/PR and reflected in the card's phase (a human-review/blocked state) — never a log-only event, so a stalled card is never invisible.

**Cross-cutting**

- **FR-025**: All new behavior MUST be config-gated such that, with every flag at its default (disabled), coordinare's routing, verdicts, and merge decisions are identical to the pre-feature baseline.
- **FR-026**: Persisted state additions (e.g. a per-head repair counter, classification labels) MUST migrate forward from existing snapshots with safe empty defaults, and existing snapshots MUST continue to load. An empty repair counter means "no attempts recorded yet" — the configured budget still applies; it never means "unlimited."
- **FR-027**: Repeated or systemic baseline-fetch failure MUST raise an operator-visible signal (distinct from a per-evaluation debug log) so that a degraded baseline — which suppresses inherited classification and repair — cannot persist unnoticed (e.g. from API rate-limit exhaustion).

### Key Entities *(include if feature involves data)*

- **Base-branch check state**: the required-check rollup of the PR's base branch / merge-base at evaluation time. Attributes: per-check name, conclusion, required-or-not, link. Read-only; never persisted as authoritative (re-read each cycle).
- **Failure signature**: a stable identity for a failing check derived from `(name, conclusion, normalized reason)`. The unit of comparison between head and baseline. The *normalized reason* is the reason-sensitive component: it is derived deterministically from stable check metadata so the same root cause produces the same signature across runs and environments, while a genuinely different cause produces a different signature (the anti-masking property). Only definitive "failed" conclusions form stable signatures; transient conclusions do not.
- **Failure classification**: per failing head check, one of INHERITED / INTRODUCED / FLAKE / UNKNOWN, plus the signature(s) that justified it. Attached to the gate decision / session state; the input to repair eligibility.
- **Gate decision (extended)**: the existing CI-gate decision record, extended with classification labels and (for Layer 1) a base-not-green hold reason. The added fields are observability/evidence and MUST NOT change any verdict or merge outcome relative to baseline (see SC-006).
- **Repair attempt budget**: a per-head counter bounding autonomous baseline repairs, parallel to the existing bounce / local-fix counters. Persisted; a new head starts fresh; exhaustion drives escalation.
- **Test-integrity guard verdict**: the combined result of the static heuristic and the automated adversarial reviewer for a candidate repair — accept, or reject-with-reason. A veto from either component, or an inability to confidently clear the change, rejects.
- **Repair gate config**: per-persona-scope settings: enabled (default off) and per-head attempt budget. Parallel to the existing CI-gate / local-test-gate config blocks.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of cases where an approved, head-green PR has a *required* base-branch check failing, coordinare does not merge until the base check passes (no merges onto a known-red base).
- **SC-002**: When the base/merge-base state cannot be determined, coordinare's merge behavior is identical to the pre-feature baseline in 100% of cases (verifiable fail-safe; no indefinite blocking).
- **SC-003**: Failure classification correctly labels INHERITED vs INTRODUCED — including the different-reason / anti-masking case and the transient-conclusion case — in ≥99% of evaluated cards over a validation window, with zero INTRODUCED-as-INHERITED misclassifications on the anti-masking test corpus.
- **SC-004**: No autonomous baseline repair is ever accepted that weakens or removes a test: 100% of candidate repairs that delete/loosen assertions, loosen comparisons, conditionally or exception-suppress assertions, add skip/xfail, remove setup/teardown, or mock away a requirement are rejected and escalated by the guard (measured against an adversarial test-weakening corpus).
- **SC-005**: No autonomous baseline repair is auto-merged; 100% of accepted repairs reach `main` only via a fresh human approval recorded after the repair commit.
- **SC-006**: With all feature flags at their defaults (disabled), coordinare's routing, verdicts, and merge decisions are identical to the pre-feature baseline; any classification/observability fields added when flags are enabled change no verdict or merge outcome (regression guard for phased rollout).
- **SC-007**: Autonomous baseline repair is bounded: the number of repair dispatches per head never exceeds the configured attempt budget, and budget exhaustion results in a visible human escalation in 100% of cases (no infinite repair loops, no silent stalls).
- **SC-008**: For the originating problem — a card blocked solely by an inherited, code-fixable required-check failure — coordinare resolves it without human intervention up to the final approval (when repair is enabled) in the target majority of cases, instead of self-blocking and giving up.
- **SC-009**: 100% of pre-feature persisted snapshots load successfully under the new code, with new fields applied at safe empty defaults and pre-feature behavior preserved.
- **SC-010**: With Layer 1 enabled, the base-not-green prevention gate adds ≤500 ms p95 to the merge decision (head and base required-check rollups fetched concurrently); with the gate disabled it adds no measurable latency over the pre-feature baseline.
- **SC-011**: With Layer 2 enabled, failure classification (the head-vs-baseline signature comparison) completes in ≤50 ms p95 on the hot path (pure CPU); with the gate disabled it adds no measurable latency.
- **SC-012**: The autonomous repair flow (Layer 3) imposes no hot-path cost beyond the static test-integrity guard's `analyze_diff`, which completes in ≤100 ms p95 for diffs ≤2,000 lines; the adversarial reviewer and repair dispatch run out-of-band (30–90 s) and never block the merge or verdict decision.
