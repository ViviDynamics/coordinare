# Quickstart: Junie Assessor Resilience

Replays the 2026-06-20 #169 assessor block as acceptance scenarios.

## Scenario A — US1: flaky response is retried, not card-blocking (SC-001/002)
1. Assessor's first response is unusable (empty answer / control chars / empty body); next is valid.
2. **Today:** terminal_error → card BLOCKED at `assessing` on the first bad response.
3. **098:** the failure is tagged transient → system-error retry → card proceeds past `assessing` on the valid response. Never blocked on the first bad response.

## Scenario B — US1: bounded, blocks only after budget (SC-002/006)
1. Every assessor response within the budget is unusable.
2. **Verify:** retried up to the cap, then blocked exactly once (no tight loop); a later clean response resets the counter.

## Scenario C — US2: normalization repairs flaky shapes (FR-002)
1. Upstream returns a body with control characters / an empty-but-reasoned answer.
2. **Verify:** the normalized response junie receives is parseable (control chars gone; answer promoted from reasoning; envelope complete). A clean body is unchanged.

## Scenario D — US3: persistent empty upstream → infrastructure (SC-003)
1. The assessor model is overloaded/down → empty-body across all retries.
2. **Verify:** the card surfaces as ENV_BLOCKED ("assessor model unavailable/overloaded"), operator-actionable — not a generic card-fault terminal error.

## Scenario E — observability (SC-004)
1. Trigger any assessor parse failure.
2. **Verify:** an `assessor.parse_failure` record carries the shape + attempt, with no raw model output / tokens.

## Scenario F — happy path unchanged (SC-005)
1. Assessor returns a clean response first try.
2. **Verify:** no extra attempts, no added latency, assessment identical to before; other stages unaffected.
