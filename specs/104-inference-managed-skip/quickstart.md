# Quickstart: Inference Run-Validation Gating

Replays the website Postgres stall and the regression guards as acceptance scenarios.

## Scenario A — US1: managed-only manifest is recorded, not timed out (SC-001)
1. A project declares postgres + redis (both coordinare-managed). The agent proposes a valid manifest.
2. **Verify:** inference does NOT start postgres/redis; no `subprocess.TimeoutExpired`; the result records both services (success artifacts written). The ~60s doomed `pg_isready` wait is gone (SC-004).

## Scenario B — US2: generic service still validated (SC-002)
1. A project declares a generic service whose `services-start.sh` fails (e.g. bad binary/args).
2. **Verify:** the generic service IS started during validation and the failure rejects the manifest (existing reject/retry path intact). No regression.

## Scenario C — US3: mixed manifest validates only generic (SC-003)
1. A project declares postgres (managed) + a working generic service.
2. **Verify:** only the generic service is started/health-checked; postgres is never started; the manifest is accepted; persisted `services.json` records BOTH.

## Scenario D — empty-after-filter
1. A manifest whose only services are coordinare-managed (postgres/redis).
2. **Verify:** run-validation is skipped entirely; the manifest is accepted (not failed, not hung). (A co-declared external_required service would instead still be validated — its env-var assertion runs.)

## Real-world payoff
The website's postgres+redis manifest is now recorded on every bootstrap instead of timing out, so the env-bootstrap renders the 102 install block, fetches the server, and the spec-101 gate verifies it — closing the "inference returns services=[]" stall regardless of inference model.
