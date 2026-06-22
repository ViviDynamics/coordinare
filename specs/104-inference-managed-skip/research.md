# Research: Inference Run-Validation Gating for Coordinare-Managed Services

## Decision: filter the manifest fed to validation (render a validation-only manifest)

When `infer_services` validates a candidate manifest, exclude coordinare-managed (postgres/redis) services from the manifest passed to `render()` for the validation run. Validate only the remaining (generic) services; if none remain, skip validation. Always write the success artifacts (`services.json` + scripts) from the **full** candidate manifest.

**Rationale**:
- Keeps `render()` (templater.py) and `validate()` (validator.py) and the 091 shell templates **unchanged** — template changes are explicitly out of scope.
- Naturally handles mixed manifests: the validation-only scripts simply omit managed services, so postgres/redis are never started at inference time, while a generic service is still genuinely exercised.
- Reuses the single source of truth `COORDINARE_MANAGED_KINDS` (schema.py) — the same frozenset the existing `_verify_binaries_resolvable` exemption uses (agent.py) — so there is no parallel notion of "managed."
- The persisted manifest/scripts still describe ALL services (managed included), so coordinare's env-bootstrap (091/102) renders the install block and the spec-101 gate verifies runtime readiness — the correct owner of managed-service start/health.

## Alternatives considered

- **Patch the 091 services-start templates to no-op managed services during a "validation mode"**: rejected — touches the templater (out of scope), adds a mode flag to shared templates, and risks divergence between validation-render and bootstrap-render.
- **Make `validate()` itself skip managed services**: rejected — `validate()` operates on already-rendered scripts (RenderedScripts), not on the typed manifest; it has no clean notion of per-service kind at that layer. Filtering belongs upstream where the typed manifest is available.
- **Set `run_validation=False` whenever any managed service is present**: rejected — would also skip validating co-declared generic services, losing the regression guard (FR-002). Filtering preserves generic-service validation.
- **Catch `subprocess.TimeoutExpired` in `infer_services` and treat as pass**: rejected — masks genuine generic-service hangs as success; the structural fix is to not start un-installable services at all.

## Confirmed facts (from code, 2026-06-22)

- `infer_services` (`__init__.py` ~L297) calls `render(manifest)` then `validate(scripts, ...)` with `run_validation=True` by default.
- `validate()` (validator.py:45) runs start→health→stop via `_run` = `subprocess.run(["bash", script], timeout=...)`; an over-budget phase raises an **uncaught** `subprocess.TimeoutExpired`.
- The performer's `_run_service_inference` (main.py:454) catches any exception as `unexpected_error: <type>` and returns no services — this is the `services=[]` outcome.
- `COORDINARE_MANAGED_KINDS = frozenset({"postgres", "redis"})` (schema.py:39); already used at agent.py:272 to exempt managed kinds from the binary-resolvability check.
- The postgres `services-start.sh.j2` start phase runs `initdb`/`<binary>` then a bounded 60s `pg_isready` loop — guaranteed to consume the validation timeout when the server isn't installed.
