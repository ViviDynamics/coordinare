# Implementation Plan: Live Config Editing in the Dashboard UI

**Branch**: `081-config-ui` | **Date**: 2026-06-06 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/081-config-ui/spec.md`

## Summary

Extend the coordinare dashboard so an operator can view and live-edit the **entire** editable
configuration surface — not just today's 11-field `_global_cfg_editable` slice. The feature
introduces a **config descriptor layer**: a server-side, introspection-derived catalog that
turns each pydantic config field into a UI-renderable descriptor (label, help, type, current
value, default, range/enum, editable/restart-required/secret flags, owning section, backing
store). The dashboard renders descriptors grouped into sections and edits flow back through
validated, atomic, optimistic-concurrency-guarded writes — applied live via the existing
`POST /api/config/reload` path where safe, flagged restart-required otherwise.

Three config tiers gain full CRUD:
1. **Coordinare config.yaml** — global tuning (expanded beyond the current allow-list),
   personas (already present), and per-symphony overrides.
2. **Spec-080 model catalogs** — `endpoints`, `model_endpoints`, `modes` (root-level in
   config.yaml), with referential-integrity enforcement and delete-protection while referenced.
3. **Spec-078 self-hosted routing table** — a performer-scoped YAML referenced by
   `SELFHOSTED_ROUTING_CONFIG`. The dashboard writes the host-side file that performer endpoints
   mount; the next performer job picks it up at job start.

Technical approach: a new `config_descriptors` module derives descriptors from the existing
pydantic models (no schema redesign — out of scope), a new set of `/api/config/*` FastAPI
endpoints extend (not replace) the existing surface, validation reuses the pydantic models
verbatim, writes reuse the existing tempfile+`os.replace` atomic pattern extended with an
`If-Match`-style content-hash precondition, and secret masking is applied centrally in the
descriptor serializer.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv)
**Primary Dependencies**: pydantic 2.x (config models + validation — reused, not modified),
FastAPI (dashboard app, `src/coordinare/dashboard.py`), PyYAML (config read/write), structlog
(observability). Frontend is the existing dashboard's server-rendered/JS layer (no new FE
framework introduced). The spec-078 routing models live in
`agent/performer/src/performer/proxy/routing.py`.
**Storage**: `config.yaml` (coordinare config + spec-080 catalogs) written atomically via
tempfile + `os.replace`; the performer routing-config YAML referenced by
`SELFHOSTED_ROUTING_CONFIG` (host-side file mounted into performer containers). No new datastore.
**Testing**: pytest (`.venv/bin/pytest`). Coordinare suite (`tests/unit/`) and performer suite
run **separately** (conftest collision). Contract tests for the new endpoints; unit tests for
the descriptor layer, secret masking, optimistic-concurrency guard, and referential-integrity
delete-protection; integration test for the write→reload→reflect cycle.
**Target Platform**: Linux server (single-host, single-process coordinare daemon + FastAPI
dashboard on ports 9090/9091).
**Project Type**: Web application (FastAPI backend + dashboard frontend) within the existing
coordinare monorepo — extends `src/coordinare/dashboard.py`, does not introduce a new service.
**Performance Goals**: Full config descriptor payload < 300 ms p95 server-side; view interactive
< 1.5 s p95 in browser; single save round-trip (validate + atomic write, excluding hot-reload
propagation) < 500 ms p95 (see spec SC-010).
**Constraints**: Never expose/log secrets; never substitute `${VAR}` placeholders with expanded
values on save; atomic writes only (no truncated/corrupt config under mid-write failure);
optimistic concurrency on every save; extend — never break — the existing `/api/config/*` and
`/api/personas/*` endpoints; no config-schema redesign; no auth/RBAC changes (existing dashboard
access model assumed).
**Scale/Scope**: Single operator-facing dashboard; config files on the order of hundreds of
settings across ~6 sections; catalogs and routing tables on the order of tens of entries each.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Evaluated against Constitution v1.1.0.

| Principle | Assessment | Status |
|-----------|------------|--------|
| **I. Code Quality First** | New `config_descriptors` module has a single responsibility (model→descriptor introspection); endpoints extend the existing module following its established async-handler pattern; type annotations on all public interfaces; no dead code; no new heavyweight dependency (reuses pydantic/FastAPI/PyYAML). | ✅ PASS |
| **II. Testing Discipline** | Plan mandates unit tests (descriptor derivation, masking, concurrency guard, referential-integrity delete-protection), contract tests (new endpoints), integration test (write→reload→reflect). Coverage must not decrease. Test names behavior-based. Deterministic (file fixtures, no live performer). | ✅ PASS |
| **III. UX Consistency** | UI must meet WCAG 2.1 AA, source visual values from design tokens (no hardcoded colors/spacing), give inline actionable validation errors (never raw stack traces/pydantic dumps), and provide loading/save-state feedback. A UX accessibility checklist is required before implementation. Editable vs read-only and staged-vs-live (restart-required) states must be visually consistent with the existing dashboard. | ✅ PASS (gated by ux checklist) |
| **IV. Performance by Design** | Measurable budgets now in spec Success Criteria (SC-010). Plan requires a CI benchmark for the descriptor payload and save round-trip. Descriptor catalog is built once per request from in-memory config (no N+1). | ✅ PASS |
| **V. Clarity Before Action** | All three original `NEEDS CLARIFICATION` markers resolved in the 2026-06-06 clarify session (concurrency, `${VAR}` handling, routing-config editability) plus the 080-catalog scope question. Zero markers remain. | ✅ PASS |

**Initial gate: PASS.** No violations; Complexity Tracking not required.

**Post-design re-check (after Phase 1): PASS.** The Phase 0/1 artifacts introduce no new
violations and no new dependencies. Specifically: (I) the three new modules retain single
responsibilities and reuse the existing atomic-write pattern — no new heavyweight deps
(research D1/D5); (II) the contract enumerates the exact unit/contract/integration test
targets, and `data-model.md`'s state-transition table makes each save outcome deterministically
testable; (III) the API error model (`config-api.md`) mandates secret-free, actionable,
stack-trace-free messages and an explanatory routing empty-state — the UX accessibility
checklist remains the gating artifact before implementation; (IV) SC-010 budgets are carried
into both `contracts/config-api.md` (per-endpoint) and `quickstart.md` §7 with a CI benchmark;
(V) all clarifications remain resolved — Phase 0 closed the last open design question (078
coordinare→performer plumbing, research D2) with zero new markers. Complexity Tracking still
empty.

## Project Structure

### Documentation (this feature)

```text
specs/081-config-ui/
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
│   └── config-api.md    # New/extended /api/config/* endpoint contracts
├── checklists/
│   └── requirements.md  # Spec quality checklist (complete)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created here)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                      # EXISTING — pydantic models (ProjectConfiguration,
│                                   #   CoordinareConfiguration, PersonasConfig, PerformersConfig,
│                                   #   Endpoint, ModelEndpoint, Mode, SymphonyConfig).
│                                   #   Source of truth for descriptor derivation. NOT redesigned.
├── config_descriptors.py          # NEW — derives UI descriptors from the pydantic models;
│                                   #   owns section grouping, editable/restart/secret flags,
│                                   #   range/enum extraction, ${VAR} literal preservation,
│                                   #   and secret masking.
├── services/
│   ├── persona_service.py         # EXISTING — atomic persona write (pattern reused)
│   └── config_write_service.py    # NEW — atomic write-back + optimistic-concurrency guard
│                                   #   for config.yaml AND the performer routing-config file;
│                                   #   shared by all save endpoints.
├── routing_config_service.py      # NEW — locates/reads/writes the host-side routing YAML
│                                   #   referenced by performer endpoints' SELFHOSTED_ROUTING_CONFIG
│                                   #   volume mount; validates against the spec-078 models.
└── dashboard.py                   # EXISTING — extend with new /api/config/* endpoints
                                    #   (sections, catalogs CRUD, routing CRUD); existing
                                    #   /api/config/global, /api/personas/*, /api/config/reload
                                    #   preserved unchanged.

agent/performer/src/performer/proxy/routing.py   # EXISTING — spec-078 models reused for
                                                  #   coordinare-side validation (imported or mirrored).

tests/unit/
├── test_config_descriptors.py     # NEW — descriptor derivation, masking, section grouping
├── test_config_write_service.py   # NEW — atomic write, optimistic-concurrency rejection
├── test_routing_config_service.py # NEW — routing read/write/validate, absent-config handling
├── test_dashboard_config_api.py   # NEW — contract tests for new endpoints
└── test_dashboard_config_integration.py  # NEW — write→reload→reflect cycle
```

**Structure Decision**: Web application within the existing coordinare monorepo. The feature
extends the FastAPI dashboard (`src/coordinare/dashboard.py`) rather than adding a service. New
server-side logic is isolated into three single-responsibility modules (`config_descriptors`,
`config_write_service`, `routing_config_service`) so the dashboard module stays an HTTP layer.
The spec-078 routing models are reused from the performer package for validation parity — the
coordinare validates with the same models the performer enforces at job start.

## Complexity Tracking

> No constitution violations. Table intentionally empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| (none)    | —          | —                                   |
