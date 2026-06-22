# Tasks: Inference Skips Run-Validation for Coordinare-Managed Stateful Services

**Input**: design docs in `/specs/104-inference-managed-skip/`
**Tests**: INCLUDED — Constitution II (TDD).
**Organization**: by user story (US1 P1 managed-only accepted → US2 P2 generic still validated → US3 P3 mixed/skip-empty). Single gating change in `infer_services` + a pure filter helper. **No schema change, no templater change, no new dep.**

## Path Conventions

Package: `packages/service_inference/src/coordinare_service_inference/`. Tests: `tests/` (find the existing inference/validation suite first).

---

## Phase 1: Setup

- [X] T001 Confirm injection point + helpers by reading `packages/service_inference/src/coordinare_service_inference/__init__.py` (`infer_services`, the `render(manifest)` → `validate(scripts, ...)` block ~L297, the success-artifacts write `_write_success_artifacts`), `schema.py` (`COORDINARE_MANAGED_KINDS`, `ServiceEntry.external_required`, `ServicesManifest`), `templater.py` `render`, `validator.py` `validate`/`ValidationResult`. Find the existing inference test module(s).

---

## Phase 2: User Story 1 — Managed-only manifest is recorded, not timed out (Priority: P1) 🎯 MVP

**Goal**: a postgres+redis (managed-only) manifest is validated without starting them, completes, and records the services.

- [X] T002 [US1] Write FAILING test(s): (a) a pure helper `services_requiring_inference_validation(manifest)` returns `[]` for a postgres+redis (managed-only) manifest, includes external+generic otherwise; (b) `infer_services` on a managed-only manifest does NOT invoke the script-running validator (patch/spy `validate`) and returns a success result recording postgres+redis. MUST fail before T003/T004.
- [X] T003 [US1] Add the pure helper `services_requiring_inference_validation(manifest) -> list[ServiceEntry]` (in `schema.py` next to `COORDINARE_MANAGED_KINDS`) — entries whose `kind not in COORDINARE_MANAGED_KINDS` (external_required services stay validated). Reuse the existing frozenset (FR-006).
- [X] T004 [US1] In `infer_services`, compute the validated subset via the helper; build a validation-only manifest (full manifest with `services` = subset); if the subset is empty, SKIP `validate()` and take the success path (write artifacts from the FULL manifest); else `render(validation_manifest)` → `validate(...)`. Always write success artifacts from the FULL manifest. Make T002 pass.

**Checkpoint**: US1 — managed-only manifests are recorded; the website stall path is closed.

---

## Phase 3: User Story 2 — Generic services still validated (Priority: P2)

- [X] T005 [US2] Write test: `infer_services` with a generic service whose start/health FAILS still rejects the manifest (validator runs for the generic service; reject/retry path unchanged). Make pass (should pass with T004; assert no regression).

---

## Phase 4: User Story 3 — Mixed manifest + skip-empty (Priority: P3)

- [X] T006 [US3] Write tests: (a) mixed manifest (postgres + working generic) → only the generic service is started (assert the rendered validation scripts/`validate` input exclude postgres), manifest accepted, persisted `services.json` records BOTH; (b) manifest with only coordinare-managed kinds (postgres+redis) → `validate()` not called, manifest accepted. Make pass.

---

## Phase 5: Polish & Cross-Cutting

- [X] T007 [P] Full inference/service suite green (`coordinare_service_inference` tests + any coordinare tests that touch inference); confirm no regression to the agent `_verify_binaries_resolvable` exemption or artifact writing.
- [X] T008 `.venv/bin/ruff check` edited files; walk `quickstart.md` A–D; confirm each SC has a covering test; verify secret-free + no new dependency + no schema/templater change.
- [X] T009 A couple of adversarial review rounds (diverse-lens + refute-verify) before merge — focus: managed services truly never started (incl. mixed), generic validation regression intact, skip-empty accepts (not silently passes a broken generic), full-manifest persistence (postgres/redis still declared), reuse of COORDINARE_MANAGED_KINDS, secret-free.

---

## Dependencies & Execution Order

- **US1 (P1)** = MVP (helper + gating + skip-empty).
- **US2 (P2)** = regression guard (generic still validated).
- **US3 (P3)** = mixed + skip-empty edge.
- **Polish** last.

## Implementation Strategy

MVP-first: **US1** — add the filter helper and gate the validate step on it (skip when empty), persisting the full manifest. That alone closes the website stall. **US2/US3** are guards confirming generic validation is preserved and managed services are never started. Focused change in `infer_services`; no template/schema/dep change. Spec-101 remains the runtime verifier for managed services.
