# Specification Quality Checklist: License and Legal Posture for Public Release

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

Six clarifications resolved in session 2026-08-27 (logged in the spec's Clarifications
section):

1. **FR-011** — external pull requests are commented on, labelled `external-contribution`, and
   closed automatically. The label keeps the refused set queryable.
2. **FR-015** — no security email is published. GitHub private vulnerability reporting is the
   only route, which makes enabling that repository setting (FR-016) load-bearing.
3. **FR-025** — the auto-close carves out security-related submissions, and the detection fails
   towards human review.
4. **FR-005a** — the README prohibition must say it covers providing access to coordinare's
   functionality, and that it applies whether or not money changes hands.
5. **FR-005b** — the README must not adjudicate the boundary cases; `LICENSE` governs.
6. **FR-024** — a commercial-licensing pointer sits alongside the prohibition. Not a public
   issue template (a commercial enquiry is sensitive to the enquirer) and not a published
   personal address.

ELv2 reconfirmed 2026-08-27, on the grounds that it carries no change date or conversion clause,
unlike BSL and FSL.

Two decisions remain open and both are needed before `/speckit.plan`:

- **Accepted license set** for the FR-020 dependency check. The tree already contains
  LGPL-3.0-only (`psycopg`, `psycopg-pool`, via the declared runtime dependency
  `langgraph-checkpoint-postgres`), MPL-2.0 (`certifi`, `orjson`, `pathspec`), and
  LGPL-2.1-or-later (`semgrep`, distribution status unverified). The choice determines whether
  the check rejects a working feature.
- **Whether external issue forwarding is in scope for spec 142**, and if so its transport. A
  destination held in a repository secret is workable because `issues` events run in the
  base-repo context where secrets are available, unlike fork `pull_request` events.

Everything else was resolved with a documented default in the Assumptions section rather than
a marker: copyright year (2026), supported-versions posture (pre-1.0, latest release plus
`main`, no backports), audit scope (distributed dependencies only, not dev tooling), and where
the enforcement checks live (existing `tests/unit/` suite, no new infrastructure).

Named repository facts the spec relies on, verified on `main` at 2026-08-27:

- `README.md:243` already links a `LICENSE` file that does not exist (broken link today).
- `pyproject.toml` declares no `license` field and no classifiers.
- `packages/service_inference` is a first-party in-tree package (`coordinare-service-inference`),
  consumed by both the daemon and the performer via a path source, so it is distributed.
- The tree contains accurate third-party "open source" references (`docs/opencode-sdk.md`,
  `src/coordinare/services/scoring.py:102`) that a naive wording guard would falsely flag.
