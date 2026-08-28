# Implementation Plan: Threat Model, Trust Boundaries, and Cheap Hardening

**Branch**: `144-threat-model-trust-boundaries` | **Date**: 2026-08-27 | **Spec**: [spec.md](./spec.md)
**Issue**: [#198](https://github.com/ViviDynamics/coordinare/issues/198) (launch-blocking)

## Summary

Two deliverables that support each other. **A threat model** telling a self-hoster what coordinare
trusts, what it protects, and what it leaves exposed. **A localhost guard** closing the one
exposure the audit found that needs nothing unusual from the operator: any page in their browser
can currently drive 24 mutating dashboard routes against `127.0.0.1`, including deleting a
symphony and rewriting configuration.

The guard is one middleware, not 24 decorators, because a per-route mechanism is one forgotten
annotation away from a hole. It checks `Origin` on mutating methods (cross-site request forgery)
and `Host` on everything (DNS rebinding), which are different attacks needing different checks.

Supporting work: make the health bind configurable, warn loudly on a non-loopback dashboard bind,
document a token-permission matrix, and fill the section spec 142 reserved in `SECURITY.md`.

## Technical Context

**Language/Version**: Python 3.14 (project minimum 3.12; prod 3.14.5 via uv).
**Primary Dependencies**: None added. Uses FastAPI/Starlette middleware already in the stack,
`ipaddress` from the standard library for loopback classification, and the existing `structlog`
logger and pydantic config models.
**Storage**: N/A. No persisted state, no schema change.
**Testing**: `pytest`. New module `tests/unit/test_144_threat_model_trust_boundaries.py`,
following the spec-142 precedent. Five existing test modules are updated (FR-018).
**Target Platform**: The coordinare daemon process, plus Markdown documentation.
**Project Type**: Single project.
**Performance Goals**: The guard runs on every request, so it must be constant-time header
inspection with no I/O. The permitted sets are computed once at application construction, not
per request.
**Constraints**:
- Must not break the SSE stream, the dashboard's own page load, or its same-origin requests.
- Must not break non-browser callers, which send no `Origin`.
- Must not change any existing workflow's triggers or required status.
- `SECURITY.md`'s reserved section is filled in place, never restructured.
**Scale/Scope**: 1 new middleware, 1 new config field plus an allowlist field, 2 bind sites in
`__main__.py`, 1 new documentation tree (`docs/security/`), 1 README link, 1 `SECURITY.md`
section, 1 new test module, 5 existing test modules updated.

## Constitution Check

*GATE: Must pass before Phase 0. Re-checked after Phase 1.*

| Principle / Gate | Assessment |
|---|---|
| **I. Code Quality First** | PASS. One middleware and one config addition, both small and pure. The guard is extracted to its own module (research D6) so the threat model can reference a single reviewable unit rather than a slice of a 5,000-line file. |
| **II. Testing Discipline (NON-NEGOTIABLE)** | PASS, and load-bearing. Every guard behaviour is tested: foreign origin rejected on mutating routes, absent origin allowed, foreign host rejected on reads, all loopback spellings accepted, port derived from config, SSE unaffected, and the fail-closed property (FR-012) asserted by enumerating the app's routes rather than a fixed list. |
| **III. User Experience Consistency** | PASS. The reader is the user here. One vocabulary across threat model, `SECURITY.md`, and README. Rejections explain which header failed (FR-016) so a misconfiguration is not mistaken for a bug. |
| **IV. Performance by Design** | PASS. Permitted sets precomputed at construction; per-request work is a set membership test on two headers. No I/O, no allocation beyond a parsed header. |
| **V. Clarity Before Action** | PASS. The one materially ambiguous decision (guard width) was resolved with the approver before planning and is logged in the spec's Clarifications. Six further decisions are recorded in research.md rather than assumed. |
| **Gate 1: Lint & Format** | Applies. |
| **Gate 2: Type Check** | Applies. New module fully annotated. |
| **Gate 3: Unit Tests** | Applies. No skipped tests introduced. |
| **Gate 4: Integration Tests** | Partially applies: the guard is exercised through the real FastAPI app via the test client, which is the integration surface that matters. |
| **Gate 5: Coverage** | Applies; does not regress. |
| **Gate 6: Performance** | N/A by benchmark, but see Principle IV. |
| **Gate 7: Accessibility** | N/A. No UI change. |
| **Gate 8: Code Review** | Applies. Structurally unsatisfiable on a solo repository, as recorded for spec 142; not something this feature can resolve. |

**Result: PASS.** One deliberate scope expansion beyond the issue's literal text (host check on
reads) was approved by the approver and is recorded in the spec. Complexity Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/144-threat-model-trust-boundaries/
├── spec.md            # Complete: 26 FRs, 1 clarification, 0 open markers
├── plan.md            # This file
├── research.md        # Phase 0: 6 decisions
├── data-model.md      # Phase 1: in-memory shapes (no persistence)
├── quickstart.md      # Phase 1: how to verify the guard and the docs
├── contracts/
│   └── localhost-guard.md   # The guard's decision table, as a contract
└── checklists/requirements.md
```

### Source Code (repository root)

```text
docs/security/
└── threat-model.md              # NEW (FR-001..FR-006)

src/coordinare/
├── localhost_guard.py           # NEW. Permitted-set derivation + the middleware
├── dashboard.py                 # EDIT. Install the guard alongside the logger at :3896
├── config.py                    # EDIT. health_check_host, trusted_dashboard_hosts
└── __main__.py                  # EDIT. :949 + :1034 use the new field; startup warning

README.md                        # EDIT. Prominent threat-model link (FR-007)
SECURITY.md                      # EDIT. Fill the reserved section in place (FR-008)

tests/unit/
├── test_144_threat_model_trust_boundaries.py   # NEW
├── test_dashboard.py                # EDIT. 3 TestClient constructions -> local base_url
├── test_cancel.py                   # EDIT. 2 constructions
├── test_dashboard_config_api.py     # EDIT. 1
├── test_symphony_api_endpoints.py   # EDIT. TestClient constructions
└── test_symphony_coverage.py        # EDIT. TestClient constructions
```

**Structure Decision**: Single project, matching the repository. Every path verified against
`main` at 2026-08-27.

**`src/coordinare/health.py` is deliberately NOT edited.** An earlier draft of this tree listed it.
The health application is not guarded: it binds all interfaces on purpose (research D4) to serve
load-balancer liveness checks, so applying a localhost guard to it would break the exact use that
decision preserved. Its exposure is addressed by documentation and configurability instead. Its
existing tests (`test_health_coverage.py`, `test_health_endpoints.py`) are therefore unaffected.

The one judgment worth stating: the guard lives in **its own module**, not inline in
`dashboard.py`. Three reasons. The threat model has to point at the mitigation, and pointing at a
named module is better than pointing into a 5,000-line file. The permitted-set derivation is the
part most likely to be got wrong and most deserving of direct unit tests. And the health
application may want the same guard later, which an inline closure would not permit without
duplication.

## Phase 0: Research

See [research.md](./research.md). Six decisions. Two are load-bearing and worth naming here:

- **D4 (health bind)** found a genuine conflict: `docker-compose.yml` publishes no ports and
  defines no healthcheck, but `README.md:119` documents the health endpoint as a load-balancer
  target. Changing the default to loopback would therefore be a silent breaking change for an
  explicitly documented use. The default is left as-is, made configurable, and the exposure is
  documented and warned about instead.
- **D5 (keeping the document true)** rejects asserting prose. Tests assert **behaviour**, and the
  threat model cites the test names that hold each claim up, so a reader can check the claim and a
  future editor can see which claims are load-bearing.

## Phase 1: Design

See [data-model.md](./data-model.md), [contracts/localhost-guard.md](./contracts/localhost-guard.md),
and [quickstart.md](./quickstart.md).

## Phase 2

`/speckit.tasks` generates `tasks.md`. Not created by this command.
