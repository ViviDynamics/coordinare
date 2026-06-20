# Data Model: Junie Assessor Resilience

**No new persisted state and no schema change.** US1 reuses the existing per-card
`system_error_count` (075-era) for the bounded retry; US3 reuses the spec-095
ENV_BLOCKED classification. No new fields on `PersistedSession` / snapshot.

## 1. Reused persisted state (unchanged)

| Field | Origin | Use here |
|---|---|---|
| `system_error_count` / `system_error_last_at` / `system_error_reason` / `system_error_notified` | existing system-error retry path | US1: bounded assessor retry + backoff; resets on a clean response; blocks after N (handle_system_error) |
| `env_blocked` (per-card) | spec-095 | US3: when exhausted on empty-body, surface as ENV_BLOCKED (model unavailable/overloaded) |

Schema stays at v11 (096). No bump.

## 2. Transient classification (not persisted)

- **Assessor failure shape** — one of: `empty_answer` (finish_reason=length, empty content), `malformed_body` (control chars / un-deserializable), `empty_body` (no response / timeout), `truncated`. Computed at the reason source; drives routing (retry vs ENV_BLOCKED) and the observability record. Carries no model output.

## 3. Reused models / mechanisms

- `_FORMAT_ERROR_PREFIX = "BACKEND_FORMAT_ERROR:"` + `_is_transient_backend_error` (monitor_performer) — the retry gate US1 routes into.
- `handle_system_error` — backoff + budget + block-after-N (US1/US5 bounding).
- spec-095 ENV_BLOCKED path (env_signature / classification) — US3 infra surfacing.
- proxy normalizer chain (`strip_reasoning` #078/#130, envelope-complete 082) + a NEW control-char normalizer — US2 (performer-side; no coordinare state).

## 4. Classification → action (per assessor failure)

```
assessor performer returns error/terminal with reason R
        │
   classify shape(R)
        │
 ┌──────┼─────────────────────────────┬───────────────────────────┐
 malformed_body / empty_answer /     empty_body / timeout        (clean response)
 truncated                                │                          │
        │                                 │                       proceed — unchanged
 tag BACKEND_FORMAT_ERROR →        tag transient →                (no retry, no latency)
 system_error retry (bounded)      system_error retry (bounded)
        │                                 │
   budget exhausted?                budget exhausted?
        │ yes                             │ yes
   block (operator)                  ENV_BLOCKED (095): "assessor model
                                     unavailable/overloaded" (infra, not card)

(every non-clean attempt emits a secret-free shape-tagged record)
```
