# Contract: Dispatch-Time Readiness Gate

Defines how the existing dispatch readiness guard in `dispatch_performer.py` (~973-1000) consumes
the tri-state readiness result before dispatching a code-running performer stage. This is a
*behavioral* contract over existing seams — no new graph node, no new dispatch path, no new
persisted field.

## Applicability

| Stage class | Source of truth | Gated? |
|-------------|-----------------|--------|
| Code-running stages (qa, implementing, reviewing, security, documenting, architecting, advocate, assessing, closing_review) | `ROLE_TO_STAGE` in `lifecycle.py` | **Yes** |
| `env_bootstrap` | existing `_is_bootstrap_dispatch` check | **No** (exempt — it is what builds the cache) |

## Decision contract

On each code-running dispatch, after the existing "cache current" check passes, invoke
`daemon._verify_env_cache_clean(symphony, svc)` (one clean-context container exec, 300s timeout)
and branch on the tri-state:

| Readiness result | Action | Hold / log family |
|------------------|--------|-------------------|
| `True` (exit 0) | Proceed with dispatch | (none — normal dispatch) |
| `False` (nonzero) | Trigger re-bootstrap for the **current spec sha** via `EnvCacheService.check_and_trigger`; withhold dispatch and release the slot | `dispatch_performer.env_cache_not_current` → `bootstrap_in_flight` |
| `None` (verify.sh absent) | **MUST NOT block** — proceed on legacy `last_bootstrap_succeeded` (degraded, back-compat) | (none — degraded passthrough) |

The `False` path reuses the **exact** hold/slot-release machinery the not-current path already uses
(single hold point). Readiness is re-evaluated every dispatch; **no readiness verdict is cached**
across dispatches (FR-006).

## Loop bound

The dispatch→bootstrap→dispatch loop is bounded by the existing `env_bootstrap_max_attempts`
budget (`config.py:937`). On `>= max` attempts for the current spec sha, the existing
`env_cache.bootstrap_exhausted` path fires and the card surfaces an actionable env-blocked verdict
(no thrash). Spec-sha change resets the budget via the existing reset path. **No new
readiness-specific counter** (FR-008).

## Defense-in-depth

The spec-088 QA-verdict-integrity gate (`monitor_performer.qa_env_blocked`) remains the independent
false-OK backstop: if an incomplete manifest yields a hollow `verify.sh` pass, 088 still downgrades
a pass-claim with zero execution evidence. 093 closes the *realization race*; 088 catches
*completeness* gaps. The two are complementary, not redundant.

## Observability

Readiness decisions are emitted via structlog with env-var NAMES and file PATHS only (FR-009):
the symphony name, the spec sha, the tri-state outcome, and (on `False`) the captured failing-line
detail from `verify.sh` (which itself carries no secret values). No literal secret values are
logged.

## Field Registry

No new dispatch-payload fields. The identifiers this contract reads are existing seams:

| Field | Type | Owner | Notes |
|-------|------|-------|-------|
| readiness tri-state | `True \| False \| None` | `_verify_env_cache_clean` return | Transient; not persisted. |
| `spec_sha` (current) | `str` | dispatch context / `EnvManifest` | Re-bootstrap is keyed to this. |
| `env_bootstrap_max_attempts` | `int` (default 3, ge=1, le=20) | `config.py:937` | Loop bound; reused unchanged. |
| `bootstrap_exhausted` | derived flag | `EnvCacheService` | Surfaces env-blocked verdict at `>= max`. |
| `last_bootstrap_succeeded` | `bool` | `EnvCacheState` | Legacy fallback ONLY when readiness is `None` (degraded). |
