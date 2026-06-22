# Contract: Env-Bootstrap Service-Readiness Completion Gate

Inserts a required-service readiness check at env_bootstrap completion. Reuses
the 091 start/health scripts + the existing bootstrap-failure → cache-not-ready
path. Symphonies with no declared services are unchanged.

## Gate decision table

| Declared required services | Manifest | services-start + health | Bootstrap outcome |
|---|---|---|---|
| none | — | — | `env_bootstrap_complete` (UNCHANGED) |
| ≥1 required | rejected/empty | — | **error** — `"required services declared but manifest rejected: <reason>"` |
| ≥1 required | present | all required connectable (health 0) | `env_bootstrap_complete` |
| ≥1 required | present | a required service start/health fails | **error** — `"service not connectable: <name>"` |
| optional only / mixed | present | optional fails health | warn + continue (not blocked) |

An `error` outcome → `on_bootstrap_complete(success=False)` → `readme_sha=None`, cache **not** ready → not dispatchable (093) + operator-actionable surface (095) → re-bootstrap.

## Invariants (MUST)

1. **Connectable-before-complete (FR-001/FR-002, SC-001):** a cache is not marked ready unless every required declared service is started AND passes its connect/health check; "installed/packages downloaded" alone is insufficient.
2. **Rejected manifest = failure (FR-003):** a rejected/empty services manifest for a required-services symphony fails the bootstrap with the validation reason — never silently complete.
3. **No-dispatch into broken env (FR-005, SC-002):** a required-service-unready cache is not current/dispatchable; the condition is operator-actionable (ENV_BLOCKED line).
4. **No-services unchanged (FR-007, SC-003):** a symphony declaring no services bootstraps exactly as before; the gate is declaration-driven.
5. **Required vs optional (FR-004):** required failing blocks; optional failing warns-and-continues.
6. **Bounded (FR-006, SC-005):** start+health are timeout-bounded with brief retry — a down service surfaces within the bound (never hangs), a slow-but-healthy one isn't false-failed.
7. **Secret-free (FR-008, SC-004):** readiness records/logs/notifications carry only service name / status / reason / shape — never DB passwords or raw output.
8. **No new dependency / no breaking schema change (FR-009).**

## Observability record (secret-free)

```json
{ "event": "env_bootstrap.service_readiness",
  "service": "postgres", "required": true,
  "installed": true, "started": false, "connectable": false,
  "reason": "not connectable: services-health.sh exit 1",
  "bootstrap_outcome": "error" }
```
