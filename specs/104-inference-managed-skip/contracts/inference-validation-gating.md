# Contract: Inference Run-Validation Gating

Governs which services `infer_services` starts during its candidate-manifest run-validation, and what gets persisted.

## Field Registry

(No dispatch/payload field changes — this spec changes inference control flow only. Registry intentionally empty.)

## Gating contract

| Aspect | Requirement |
|---|---|
| Validated set | All services whose `kind not in COORDINARE_MANAGED_KINDS` — i.e. generic AND external_required services (external start scripts only assert `required_env_vars`; they never start a binary, so they're safe to validate) |
| Managed services (postgres/redis) | NEVER started/health-checked at inference time; readiness deferred to spec-101 gate at bootstrap |
| Empty validated set | When only coordinare-managed kinds remain: skip `validate()`; accept the manifest (write success artifacts) — never fail/hang |
| Persisted artifacts | Always rendered from the FULL candidate manifest (managed services remain declared) |
| Generic service start/health failure | Still rejects the manifest (existing reject/retry path; no regression) |
| Managed-kind source of truth | Reuse `COORDINARE_MANAGED_KINDS` (schema.py); no parallel definition |

## Invariants (MUST)

1. **No managed start at inference (FR-001/FR-004, SC-003):** postgres/redis are excluded from the validation render; they are never executed during inference.
2. **Generic validation preserved (FR-002, SC-002):** a generic service that fails start/health still rejects the manifest.
3. **Skip-when-empty (FR-003, SC-001):** a manifest whose only services are coordinare-managed (postgres/redis) validates nothing and is accepted (no `TimeoutExpired`, no `services=[]`).
4. **Full-manifest persistence (FR-004):** the written `services.json` + scripts describe all declared services so coordinare (091/102) hosts them and spec-101 verifies them.
5. **Secret-free / no new dep (FR-007):** logs carry kinds/counts/names only; no new dependency; shared package stays source of truth.

## Example (website case)

Candidate manifest: `[postgres(kind=postgres), redis(kind=redis), mailhog(external_required=true)]`.
Validated set after excluding managed kinds = `[mailhog]` → only mailhog's env-var assertion runs (postgres/redis are never started) → manifest accepted → `services.json` records postgres+redis+mailhog → env-bootstrap renders the 102 install block → spec-101 gate verifies postgres/redis at bootstrap.
