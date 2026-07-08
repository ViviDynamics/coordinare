# Feature Specification: BLOCKED-Card Auto-Recovery + QA Visual-Capture Env Resilience

**Feature Branch**: `129-blocked-card-recovery`
**Created**: 2026-07-08
**Status**: Draft
**Input**: Auto-recover BLOCKED cards when their block condition clears, and stop QA from hard-blocking when visual-capture tooling is unavailable.

## Context / Problem

Once a card lands in the **BLOCKED** column, coordinare skips it every cycle (`session_skipped reason=blocked_column`) and **never re-evaluates whether the thing that blocked it still holds**. A card "checks into BLOCKED and never checks out" — it sits there indefinitely after the blocker is resolved, forcing an operator to manually move it back to TODO/IN_REVIEW. (Observed live: #111 and #177 stuck for hours/days.)

spec-128 added stale-review handling but **only during PR monitoring (IN_REVIEW)** — it cannot reach a card already parked in BLOCKED. Separately, #111's *actual* current blocker is the QA stage hard-blocking because the **visual-capture (screenshot) tooling is unavailable in the QA runtime** — a genuine environment limitation that traps the card instead of degrading gracefully or auto-recovering when tooling returns.

Two complementary fixes: **(US1)** re-evaluate BLOCKED cards each cycle and auto-recover them when their block clears; **(US2)** make QA treat "capture tooling unavailable" as a recoverable env-block, not a hard fail — which US1 then auto-recovers when tooling returns.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - BLOCKED cards auto-recover when their blocker clears (Priority: P1)

Each cycle, before skipping a BLOCKED card, coordinare re-evaluates whether the original block condition still holds. When it has **demonstrably cleared**, the card is auto-unblocked and routed to the correct next stage — not blindly to TODO, but to where it should resume (a PR-review block → IN_REVIEW; an env block → its prior stage; an answered clarification → resume the lifecycle).

**Why this priority**: This is the architectural root — cards rotting in BLOCKED is the recurring failure across #111/#177 and forces constant manual intervention. It also extends spec-128's stale-review handling to already-blocked cards.

**Independent Test**: Park a card in BLOCKED for each recoverable reason; clear that reason; run one cycle and confirm the card auto-unblocks to the correct column with one notification — and that a card whose block still holds stays put.

**Acceptance Scenarios**:

1. **Given** a card BLOCKED behind a stale human `CHANGES_REQUESTED` whose feedback is now addressed/dismissed (per the spec-128 evaluator + `reviewDecision`), **When** coordinare re-evaluates, **Then** the card moves to IN_REVIEW and the reviewer is re-requested, with one notification.
2. **Given** a card BLOCKED as ENV_BLOCKED whose environment has since recovered, **When** coordinare re-evaluates, **Then** the card returns to its prior stage and resumes, with one notification.
3. **Given** a card BLOCKED behind a required CI check that was red and is now green, **When** coordinare re-evaluates, **Then** the card resumes toward merge/review.
4. **Given** a card BLOCKED on an open clarification that a human has since answered on the issue, **When** coordinare re-evaluates, **Then** the card resumes the lifecycle with the answer applied.
5. **Given** a card whose block condition **still holds** (unanswered clarification / genuinely-unresolved human verdict / still-red CI / still-broken env), **When** coordinare re-evaluates, **Then** the card stays BLOCKED, is not re-notified, and is not thrashed.

---

### User Story 2 - QA does not hard-block when visual-capture tooling is unavailable (Priority: P2)

When QA cannot perform visual capture because the screenshot tooling is unavailable (an environment limitation, **not** an application failure), QA classifies the situation as ENV_BLOCKED with a clear, deduplicated operator surface naming the missing tooling — instead of trapping the card as a hard failure. When the tooling returns, US1's env-recovery re-check resumes the card. Where the **non-visual** acceptance criteria are independently satisfied with real evidence, QA MAY record a **partial/limited pass that is explicit about the missing visual evidence** — never a false pass.

**Why this priority**: This is #111's concrete current blocker; without it the card re-blocks on QA every recovery attempt. It depends on US1 to actually resume once tooling returns.

**Independent Test**: Run QA with capture tooling unavailable but the app healthy → card is ENV_BLOCKED (not failed), operator surfaced once; restore tooling → US1 resumes it. Run QA with the app genuinely broken → still a real QA fail (not mis-labeled env-blocked).

**Acceptance Scenarios**:

1. **Given** the QA runtime cannot capture screenshots (tooling missing) but the app is healthy, **When** QA runs, **Then** the outcome is ENV_BLOCKED (recoverable), one operator notification names the missing tooling, and the card is not recorded as a hard QA failure.
2. **Given** capture tooling is unavailable but the non-visual acceptance criteria are met with real evidence, **When** QA records a verdict, **Then** it is a limited pass that explicitly states visual evidence was not captured — and it never claims visual evidence it does not have.
3. **Given** the application genuinely fails a criterion, **When** QA runs, **Then** it is a real QA failure — never mislabeled as env-blocked.

---

### Edge Cases

- **Recovery-loop thrash**: a card whose block flaps (env recovers then breaks again) must not bounce in/out of BLOCKED every cycle — a per-card cooldown / dedup marker bounds recovery attempts.
- **Multiple simultaneous block reasons** (e.g. stale verdict AND env-blocked, as on #111): the card only auto-recovers when **all** active block conditions have cleared; partial clearance keeps it blocked.
- **Genuinely-open block**: an unanswered clarification or a fresh unresolved human `CHANGES_REQUESTED` must NEVER be auto-unblocked (respect the human-approval gate).
- **Re-eval error / API failure**: leaves the card BLOCKED and surfaces the error; never crashes the poll cycle.
- **Card manually moved by an operator** while blocked: operator action wins; auto-recovery must not fight a human move.
- **Ambiguous QA signal**: if QA can't reliably distinguish "app failed" from "tooling unavailable," it must fail safe toward a real QA failure (don't hide a real bug as env-blocked).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Once per cycle, before skipping a card in the BLOCKED column, coordinare MUST re-evaluate whether its recorded block condition still holds.
- **FR-002**: When a block condition has demonstrably cleared, coordinare MUST auto-unblock the card and route it to the correct resumption stage (PR-review → IN_REVIEW; env → prior stage; answered clarification → resume lifecycle; green CI → resume toward merge/review) — not unconditionally to TODO.
- **FR-003**: Coordinare MUST support these recoverable block reasons and cleared-signals: (a) stale human `CHANGES_REQUESTED` addressed/dismissed (reuse spec-128 evaluator + `reviewDecision`); (b) ENV_BLOCKED environment recovered; (c) required CI check red→green; (d) open clarification answered by a human on the issue.
- **FR-004**: Coordinare MUST NEVER auto-unblock past a genuinely unresolved human verdict or an unanswered clarification (human-approval gate preserved).
- **FR-005**: When a card has multiple active block reasons, coordinare MUST require **all** to clear before auto-recovering.
- **FR-006**: Coordinare MUST bound recovery with an anti-thrash mechanism (per-card cooldown / dedup marker) so a card cannot flap in/out of BLOCKED across cycles.
- **FR-007**: Coordinare MUST emit exactly one deduplicated operator notification when a card is auto-recovered.
- **FR-008**: BLOCKED re-evaluation MUST be fail-safe — a re-eval error leaves the card blocked and surfaces the error; it never crashes the poll cycle.
- **FR-009**: Coordinare MUST NOT override an operator's manual board move of a blocked card.
- **FR-010**: When QA cannot perform visual capture because the capture tooling is unavailable (not an app failure), QA MUST classify the outcome as ENV_BLOCKED (recoverable) rather than a hard failure, with one deduplicated operator notification naming the missing tooling.
- **FR-011**: QA MAY record a limited/partial pass when the non-visual acceptance criteria are met with real evidence AND visual capture is legitimately unavailable — and MUST state explicitly that visual evidence was not captured; it MUST NOT claim visual evidence it does not have (spec-120 evidence integrity).
- **FR-012**: QA MUST reliably distinguish a genuine application failure (real QA fail) from missing capture tooling (env-blocked); when ambiguous it MUST fail toward a real QA failure.

### Key Entities

- **Block record (per card)**: the recorded reason(s) a card is in BLOCKED (review-verdict / env / ci / clarification), each with the signal needed to detect clearance; plus the anti-thrash marker (last recovery attempt / cooldown).
- **Recovery decision**: the derived outcome of re-evaluating a blocked card — `STILL_BLOCKED` / `RECOVER→<stage>` with a reason string.
- **QA verdict (extended)**: adds an ENV_BLOCKED-capture outcome distinct from PASS / FAIL, and a limited-pass flag with an explicit "no visual evidence" note.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card whose sole blocker has cleared auto-recovers to the correct stage within one evaluation cycle, with zero manual board moves required.
- **SC-002**: Operators receive exactly one notification per auto-recovery (no per-cycle repeats).
- **SC-003**: **Zero** cards are auto-unblocked while a genuine human verdict or unanswered clarification is still outstanding.
- **SC-004**: No card flaps in/out of BLOCKED more than once per cooldown window.
- **SC-005**: When capture tooling is unavailable, QA produces an ENV_BLOCKED (recoverable) outcome in 100% of such cases and a hard failure in **zero** of them.
- **SC-006**: **Zero** QA false-passes — no verdict ever claims visual evidence it did not capture.
- **SC-007**: For the #111 class (env-blocked QA + stale-addressed review), the card auto-recovers once tooling returns and the stale review is addressed — without operator intervention.

## Assumptions

- The per-card persisted state can record the block reason(s) + an anti-thrash marker (reuses the existing session-state mechanism; a small backward-compatible addition).
- The cleared-signals are observable from data coordinare already fetches or can cheaply fetch (review context, CI checks, issue comments, env-cache health).
- "Only humans approve / gate" remains a hard project principle; auto-recovery never clears a human verdict.
- QA can obtain a reliable signal that capture tooling (not the app) is what failed.

## Out of Scope

- Changing the human-approval merge gate.
- Auto-answering clarifications on the operator's behalf.
- Provisioning/installing the visual-capture tooling itself (infrastructure, not coordinare).
- Any auto-merge behavior.

## Related Prior Work

- **spec-128** (stale-review handling): US1 reuses its staleness evaluator + `get_pr_review_context`; this spec extends that reach to already-BLOCKED cards.
- **spec-095** (ENV_BLOCKED): US1/US2 reuse its classification + notification surface.
- **spec-120** (QA evidence integrity): US2's limited-pass must honor the evidence floor.
- Note the stale `fix/088-qa-env-blocked-and-daemon-blocked-recovery` branch — check it during planning for any overlapping groundwork to reuse rather than duplicate.
