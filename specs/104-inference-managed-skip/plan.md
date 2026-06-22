# Implementation Plan: Inference Skips Run-Validation for Coordinare-Managed Stateful Services

**Branch**: `104-inference-managed-skip` | **Spec**: [spec.md](spec.md)

## Technical Context

**Language**: Python 3.14 (project min 3.12; prod 3.14.5 via uv)
**Primary deps**: the existing `coordinare_service_inference` package (`infer_services` orchestrator in `__init__.py`, `render` in `templater.py`, `validate` in `validator.py`, `COORDINARE_MANAGED_KINDS` + `ServicesManifest`/`ServiceEntry` in `schema.py`). No new external dependency.
**State**: none — inference is stateless; persisted coordinare state unchanged.
**Testing**: `.venv/bin/pytest` (package tests live under `tests/`); TDD per Constitution II.
**Scope**: a single gating change in `infer_services`' validate step + a small manifest-filter helper. No templater change, no schema change.

## Constitution Check

- **TDD (II)**: failing tests first (managed-only accepted; generic still validated; mixed validates only generic; empty-after-filter skips). PASS.
- **Interface-first / minimal surface**: reuse `COORDINARE_MANAGED_KINDS` (no parallel "managed" notion); reuse `render`/`validate` unchanged — only the *input* to validation is filtered. PASS.
- **Secret invariant**: logs carry kinds/counts/names only. PASS.
- **No new dependency / shared package stays source of truth**: PASS.

## Design

The fix lives entirely in `infer_services` (`__init__.py`), at the point it renders + validates a candidate manifest (~L297). Today:

```python
scripts = render(manifest)
last_validation = validate(scripts, working_dir=None, env=env_overrides or None)
```

`render` emits start/health/stop covering **all** services; `validate` runs them; a coordinare-managed (postgres/redis) service's start can't succeed at inference time (binary installed post-inference) → uncaught `subprocess.TimeoutExpired`.

### Change

1. Add a pure helper (in `schema.py`) `services_requiring_inference_validation(manifest) -> list[ServiceEntry]` returning entries whose `kind not in COORDINARE_MANAGED_KINDS` — i.e. generic AND external_required services (external start scripts only assert `required_env_vars`, so they're safe to validate; only coordinare-managed kinds are excluded). (Reuses the existing frozenset — FR-006.)
2. In `infer_services`, derive a **validation-only manifest** = the candidate manifest with `services` replaced by that filtered list (other fields copied).
3. If the filtered list is **empty** → skip `validate()` entirely; treat as a passing validation (set a synthetic ok result / take the success branch) and write success artifacts from the **full** manifest. (FR-003)
4. If non-empty → `render(validation_manifest)` → `validate(...)` only those scripts. On ok, write success artifacts from the **full** manifest (so postgres/redis start/health/stop scripts are still persisted for coordinare's bootstrap). (FR-002/FR-004)
5. On validation failure of the generic subset → existing reject/retry path unchanged (FR-002 regression guard).

**Key invariant**: the *persisted* `services.json` + rendered scripts always come from the FULL manifest (managed services remain declared); only the *validation input* is filtered. Coordinare (091/102) + the spec-101 gate own managed-service install + runtime readiness.

### Why filter the rendered scripts, not patch the templates

Filtering the manifest fed to `render()` for validation keeps `render`/`validate` and the 091 templates untouched (no template change — out of scope), and naturally handles mixed manifests: the validation scripts simply omit the managed services.

## Phase 0 — research.md

One decision: *where* to filter. Chosen: filter the manifest fed to validation (render a validation-only manifest), not patch templates or `validate()`. Rationale + alternatives in [research.md](research.md).

## Phase 1 — contracts + quickstart

- [contracts/inference-validation-gating.md](contracts/inference-validation-gating.md): the gating contract (which entries are validated; skip-when-empty; full-manifest artifacts).
- [quickstart.md](quickstart.md): replays the website managed-only case + generic regression + mixed.
- data-model: no change (reuses ServicesManifest/ServiceEntry/COORDINARE_MANAGED_KINDS).

## Phase 2 — tasks (see tasks.md)

MVP = US1 (managed-only accepted). US2/US3 are guards on the same change.
