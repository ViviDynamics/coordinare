# Specification Quality Checklist: Threat Model, Trust Boundaries, and Cheap Hardening

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-08-27
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

One clarification resolved in session 2026-08-27, logged in the spec: the guard's width. The
issue asked for an Origin/Host check on mutating routes; the spec widens the **host** check to
all routes because the origin check structurally cannot protect reads (a browser sends no
`Origin` on a same-origin GET), leaving configuration exfiltratable via DNS rebinding.

Repository facts the spec relies on, verified against `main` at 2026-08-27. The issue's own line
numbers had drifted, so these were re-derived rather than copied:

- **24 mutating routes** in `src/coordinare/dashboard.py`, including `DELETE /api/symphonies/{name}`,
  `PUT /api/config/global`, and catalog plus routing CRUD. The issue implied a smaller surface.
- `src/coordinare/dashboard.py:3896` already carries an `@app.middleware("http")` request logger,
  so the middleware approach follows an existing pattern rather than introducing one.
- `src/coordinare/__main__.py:1034` hardcodes `host="0.0.0.0"` for the health application, and
  `:949` calls `check_port_available("0.0.0.0", ...)`. The dashboard uses `config.dashboard_host`
  at `:1057` and `:1070`. (Issue said 943 and 1023; both had moved.)
- `src/coordinare/config.py:826` defaults `dashboard_host` to `127.0.0.1`, `:825` defaults
  `dashboard_port` to `8090`.
- `src/coordinare/health.py:20` `create_health_app` serves only `/health`, `/live`, `/ready`,
  `/metrics`. All read-only, but `/metrics` discloses operational detail.
- `@app.get("/events")` at `dashboard.py:4008` is the SSE stream. A method-scoped origin check
  does not touch it; the host check will, which is intended and must not break it.
- **Empirically measured**: a default `TestClient` sends `Host: testserver` and no `Origin`;
  `TestClient(app, base_url="http://127.0.0.1:8090")` sends `Host: 127.0.0.1:8090` and no
  `Origin`. Five existing test files use the default form and will need updating (FR-018).

## Known tension to resolve at planning

FR-026 requires the threat model's checkable claims to be test-covered. The mechanism is a
planning decision: asserting document content risks a brittle test that fails on a reworded
sentence, while asserting only behaviour leaves the document free to drift. The plan should pick
one deliberately rather than defaulting into the first.
