# Feature Specification: Junie Assessor Resilience

**Feature Branch**: `098-junie-assessor-resilience`  
**Created**: 2026-06-20  
**Status**: Draft  
**Input**: User description: "Junie assessor resilience — tolerate malformed/transient upstream responses instead of terminal-blocking the card."

## Overview

The `assessing` stage runs the Junie harness against a self-hosted model. Junie's response parser is strict: any response it cannot deserialize makes it terminal-error (`Failed to build 'issue.md'`), which immediately **blocks the card** at `assessing`. Junie does no retry, and the parse failure is treated as the card's fault rather than a transient upstream hiccup.

The upstream is flaky under realistic load. A direct probe of the assessor's exact endpoint and model with assessor-sized prompts returned **unusable responses in roughly 3–5 of every 8 requests**, in three distinct shapes:
1. **Empty answer** — the reasoning model spent its whole output budget thinking and emitted no answer (truncated).
2. **Malformed body** — the response contained characters that broke parsing.
3. **Empty body** — the model (heavily shared across several stages) returned nothing at all under concurrent load.

Because a single flaky response strands a card, work that is otherwise fine gets blocked (observed live: a website card blocked repeatedly at `assessing`). Other stages largely avoid this because their responses pass through the coordinare's response-normalizer before the agent sees them; the assessor's path does not.

This feature makes the assessor **resilient**: a flaky response is retried (bounded) instead of blocking the card, the response is normalized before the agent consumes it, a persistent upstream outage is surfaced as an infrastructure condition (not the card's fault), and the failure shape is observable.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A flaky assessor response is retried, not card-blocking (Priority: P1)

When the assessor's upstream returns a response the harness cannot use (empty answer, malformed body, or empty body), the coordinare retries the assessment a small, capped number of times before considering the card blocked. A single bad response never strands a card.

**Why this priority**: This is the exact failure that blocked a live card repeatedly. With ~40–60% of responses flaky under load, a no-retry terminal block means cards routinely get stuck at `assessing` for reasons that clear on the next attempt. Implementing only this restores throughput — it is the MVP.

**Independent Test**: Drive the assessor against a source that returns a bad response then a good one; verify the card is retried and proceeds past `assessing` on the good response, without being blocked.

**Acceptance Scenarios**:

1. **Given** the assessor's first response is unusable (empty/malformed/empty-body) and the next is valid, **When** the stage runs, **Then** the card is retried and advances past `assessing` — it is never blocked on the first bad response.
2. **Given** every assessor response within the retry budget is unusable, **When** the budget is exhausted, **Then** the card is blocked in an actionable, operator-visible state (not an unbounded retry loop).
3. **Given** the assessor's response is valid on the first try, **When** the stage runs, **Then** behavior is unchanged (no extra retries, no added latency).

---

### User Story 2 - The assessor response is normalized before the harness consumes it (Priority: P2)

The assessor's upstream response passes through the same normalization the other stages already rely on, so common flaky shapes are repaired before the strict harness parser sees them: stray control characters are removed, an answer that arrived only in the model's "thinking" channel (with an empty final answer) is promoted into the answer, and a complete, well-formed response envelope is guaranteed.

**Why this priority**: Two of the three observed failure shapes (malformed body, empty-but-reasoned answer) are exactly what the existing normalizer handles for other stages. Routing the assessor through it converts most flaky responses into usable ones, so retries (US1) are rarely needed. Builds on US1.

**Independent Test**: Feed the assessor path a response with control characters / an empty answer carrying reasoning; verify the normalized response the harness receives is clean and parseable (control chars gone, answer populated, envelope complete).

**Acceptance Scenarios**:

1. **Given** an upstream response containing control characters, **When** it is normalized, **Then** the harness receives a parseable response with the control characters removed.
2. **Given** an upstream response with an empty answer but a populated reasoning channel, **When** it is normalized, **Then** the answer is populated from the reasoning (or, if nothing usable, it is treated as a retryable bad response per US1).
3. **Given** a clean upstream response, **When** it is normalized, **Then** it passes through unchanged.

---

### User Story 3 - A persistent upstream outage is surfaced as infrastructure, not the card's fault (Priority: P3)

When the assessor's upstream is persistently empty/timing-out (the shared model is overloaded or down), and retries do not recover, the card is surfaced as an **infrastructure/environment** condition — "assessor model unavailable / overloaded," actionable by an operator — rather than a generic terminal error blamed on the card. Every parse failure records its **shape** for visibility.

**Why this priority**: Distinguishes "the model is down" (operator must act on capacity) from "this card is bad," and makes the failure debuggable. Rides on US1/US2 and aligns with the spec-095 ENV_BLOCKED line.

**Independent Test**: Make the assessor upstream persistently empty; verify the card ends in an operator-visible infrastructure state with a recorded reason, and that each attempt logged its failure shape (secret-free).

**Acceptance Scenarios**:

1. **Given** the assessor upstream returns empty/timeout across all retries, **When** the budget is exhausted, **Then** the card is surfaced as an infrastructure/environment condition ("assessor model unavailable/overloaded"), not a generic card-fault terminal error.
2. **Given** any assessor parse failure, **When** it occurs, **Then** a record captures the failure shape (empty-answer / malformed-body / empty-body / truncated) with **no** secret values and **no** raw model output.

---

### Edge Cases

- **First-try success**: no retries, no normalization changes, no added latency (behavior identical to today).
- **Intermittent flakiness**: bad-then-good within the budget → card proceeds (the common case).
- **Persistent malformed (not empty)**: after the budget, blocked as actionable — distinguished from the empty/overload case (which surfaces as infrastructure).
- **Retry budget exhausted**: bounded — the card is blocked/surfaced once, never re-retried in a tight loop (anti-thrash, consistent with the other reliability gates).
- **Normalization makes it parseable**: the retry is not even needed — the normalized response is consumed directly.
- **Empty-but-reasoned answer with nothing salvageable**: treated as a retryable bad response, not promoted to a meaningless answer.
- **Only the assessor is affected**: the other stages' paths are unchanged.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: An assessor response-parse / deserialize / empty-response failure MUST be treated as a transient, bounded-retry condition — the coordinare retries the assessment up to a capped number of attempts before the card is considered blocked. A single flaky response MUST NOT block the card.
- **FR-002**: The assessor's upstream response MUST be normalized before the harness consumes it, using the coordinare's existing response-normalizer behavior (or an equivalent pre-parse sanitizer): remove stray control characters; repair an empty-but-reasoning-only answer (promote the reasoning into the answer) or, if not salvageable, mark it retryable; and guarantee a complete, well-formed response envelope.
- **FR-003**: A persistently empty/timed-out assessor upstream (model unavailable/overloaded) that does not recover within the retry budget MUST be surfaced as an infrastructure/environment condition (operator-actionable), consistent with the spec-095 ENV_BLOCKED handling — NOT a generic terminal error attributed to the card.
- **FR-004**: Each assessor parse failure MUST emit an observability record capturing the failure **shape** (empty-answer / malformed-body / empty-body / truncated) and the attempt number.
- **FR-005**: Retries MUST be capped (no unbounded churn); after the cap, the card is blocked/surfaced exactly once in an actionable, operator-visible state until its inputs change.
- **FR-006**: When the assessor response parses normally, behavior MUST be unchanged — no extra retries, no added latency, no altered assessment.
- **FR-007**: This feature MUST be scoped to the assessor path — the other stages' (reviewer/qa/etc.) behavior MUST be unchanged.
- **FR-008**: Persisted state, logs, and notifications MUST carry only names / shapes / reasons / ids — never secret values or raw model output (carried invariant from 088/090/095).
- **FR-009**: No new external dependency; the single-host single-process snapshot state model is unchanged (any retry/attempt counter follows the existing per-card counter pattern, e.g. the 075/090 bounce/repair counters).

### Key Entities *(include if feature involves data)*

- **Assessor attempt counter**: a per-card, bounded count of assessor retry attempts (parallel to existing per-card counters), reset when the card's inputs change; drives FR-001/FR-005.
- **Assessor failure record**: the observable shape of a parse failure — one of empty-answer / malformed-body / empty-body / truncated — plus attempt number, free of secret values and raw output (FR-004/FR-008).
- **Normalized assessor response**: the upstream response after control-char stripping / reasoning-promotion / envelope-completion, as consumed by the harness (FR-002).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With an upstream that is flaky ~40–60% of the time, the assessor stage completes successfully for a card within the retry budget in the large majority of cases — a single flaky response causes **zero** card blocks.
- **SC-002**: A card is blocked at `assessing` only after the retry budget is exhausted — never on the first bad response.
- **SC-003**: When the upstream is persistently empty/down, 100% of such cards are surfaced as an infrastructure/environment condition (operator-actionable), not as a generic card-fault terminal error.
- **SC-004**: 100% of assessor parse failures emit a shape-tagged record; 0% of those records contain secret values or raw model output.
- **SC-005**: A clean first-try assessment is byte-identical in behavior to before this feature (no extra attempts, no added latency) — and all other stages are unaffected.
- **SC-006**: Retries are bounded — no card incurs more than the configured cap of assessor attempts for a given set of inputs (no churn).

## Assumptions

- The coordinare already has a response-normalizer layer (used by the other backends) that strips reasoning leak, completes the response envelope, and can be extended to strip control characters; the assessor path can be routed through it or an equivalent pre-parse sanitizer.
- The coordinare already distinguishes infrastructure/environment failures from card failures (spec-095 ENV_BLOCKED), which the "model unavailable/overloaded" case reuses.
- Per-card bounded counters already exist (075 bounce / 090 repair); the assessor attempt counter follows the same pattern, persisted in the existing snapshot.
- The exact failing-response triggers were reproduced against the live assessor endpoint (empty answer / malformed body / empty body); the precise normalization + retry boundary is finalized in research/planning.

## Dependencies

- Spec 073/078 (response normalizers) + #130 (reasoning-content promotion) + 082 (envelope completion) — the normalization behavior FR-002 reuses/extends.
- Spec 095 (ENV_BLOCKED) — the infrastructure-condition surfacing FR-003 reuses.
- The existing per-card counter + snapshot mechanism (075/090) — for the bounded retry counter.

## Out of Scope

- Modifying the third-party Junie harness itself.
- Fixing the upstream model / runtime output quality, or provisioning host capacity / changing the assessor model (operator-owned; model contention is noted as a contributing factor but not changed here).
- The assessor persona contract.
- Any change to the other stages' (reviewer/qa/security) paths.
