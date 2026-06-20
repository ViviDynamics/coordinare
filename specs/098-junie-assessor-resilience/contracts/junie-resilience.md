# Contract: Junie Assessor Resilience

Adds classification + retry around the assessor (junie) error path; reuses the
existing system-error retry budget + spec-095 ENV_BLOCKED. Other backends' paths
are untouched.

## Classification → action table

| Assessor failure | Tagged as | monitor_performer route | After budget exhausted |
|---|---|---|---|
| empty answer (finish=length, empty content) | `BACKEND_FORMAT_ERROR:` | system_error retry (3771) | block (operator) |
| malformed body (control chars / undeserializable) | `BACKEND_FORMAT_ERROR:` | system_error retry | block (operator) |
| truncated | `BACKEND_FORMAT_ERROR:` | system_error retry | block (operator) |
| empty body / timeout | transient (empty-upstream) | system_error retry | **ENV_BLOCKED** (095) |
| clean response | — | normal (no retry) | n/a |

## Invariants (MUST)

1. **No block on first flaky response (FR-001, SC-002):** an assessor parse/empty failure routes to the bounded system-error retry, never the default terminal block, on first occurrence.
2. **Bounded (FR-005, SC-006):** retries capped by the existing system-error budget; after N the card blocks/surfaces exactly once (no churn); a clean response resets the counter.
3. **Empty-upstream → infra (FR-003, SC-003):** persistent empty-body/timeout exhaustion surfaces as ENV_BLOCKED ("assessor model unavailable/overloaded"), not a card-fault terminal error.
4. **Normalize before parse (FR-002):** junie's upstream passes through the normalizer chain (control-char strip + reasoning-promote + envelope-complete); a clean body is unchanged.
5. **Unchanged happy path (FR-006, SC-005):** a clean first-try assessment incurs no extra attempts, latency, or altered output.
6. **Assessor-only (FR-007):** reviewer/qa/security paths unchanged.
7. **Secret-free (FR-008, SC-004):** the `assessor.parse_failure` record + any state carry only the failure shape + attempt number — never raw model output or tokens.

## Observability record: `assessor.parse_failure`

```json
{
  "event": "assessor.parse_failure",
  "card_id": "PVTI_…",
  "stage": "assessing",
  "shape": "empty_answer | malformed_body | empty_body | truncated",
  "attempt": 2
}
```
