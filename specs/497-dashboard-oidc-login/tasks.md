# Tasks: Dashboard OIDC login

**Input**: Design documents from `/specs/497-dashboard-oidc-login/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/oidc-routes.md, quickstart.md

**Tests**: Included — the constitution mandates test-first for every FR and the issue's
acceptance criteria are test-shaped. Provider interactions run against deterministic test
doubles; no test touches a live network.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to
- Include exact file paths in descriptions

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 Add `DashboardOidcConfig` (discovery_url, client_id, client_secret SecretStr, redirect_url, session_hours default 12) with validation (https discovery, non-empty credentials, redirect host ∈ dashboard/trusted hosts) to src/coordinare/config.py beside the existing dashboard fields
- [ ] T002 [P] Add a commented, disabled `oidc` block to config.example.yaml showing environment expansion for the secret

## Phase 2: Foundational (Blocking Prerequisites)

- [ ] T003 Build the provider test double (discovery document, token endpoint, JWKS serving a fixed RSA key) and httpx mock transport wiring for reuse across suites in tests/unit/dashboard/oidc_double.py
- [ ] T004 Implement the in-memory session store (opaque cookie value → subject, absolute expiry; create/lookup/evict) in src/coordinare/dashboard_oidc.py
- [ ] T005 Implement discovery fetch, authorization-redirect construction (state+nonce cookies), token exchange, and ID-token verification (signature via JWKS, issuer, audience, nonce) in src/coordinare/dashboard_oidc.py

## Phase 3: User Story 1 — An operator logs in through the estate SSO (Priority: P1) 🎯 MVP

**Goal**: the full Authorization Code round trip works against a conformant provider.

**Independent Test**: with OIDC configured and no session, page load lands on the provider;
completing the round trip returns a working dashboard session.

### Tests for User Story 1 (write first, watch them fail)

- [ ] T006 [P] [US1] Failing test: unauthenticated page load 302s to the provider authorization URL with correct client_id, redirect_uri, scope, state, nonce (tests/unit/test_497_oidc_login.py)
- [ ] T007 [P] [US1] Failing test: callback with valid code exchanges, verifies issuer/audience/nonce via the double's JWKS, sets an HttpOnly SameSite=Lax session cookie, redirects to / (tests/unit/test_497_oidc_login.py)
- [ ] T008 [P] [US1] Failing test: tampered state, stale state cookie, nonce mismatch, and wrong issuer/audience each refuse with 403, create no session, and log a reason (tests/unit/test_497_oidc_login.py)

### Implementation for User Story 1

- [ ] T009 [US1] Register GET /oidc/login and GET /oidc/callback routes with the middleware exempting them (exact-path) in src/coordinare/dashboard/routers/oidc.py and src/coordinare/dashboard_auth.py
- [ ] T010 [US1] Wire DashboardOidcConfig through create_dashboard_app and __main__ with load-time refusal on invalid blocks in src/coordinare/dashboard/app.py and src/coordinare/__main__.py

**Checkpoint**: the round trip completes end-to-end against the test double.

## Phase 4: User Story 2 — The boundary holds for every route (Priority: P1)

**Goal**: no unauthenticated route serves dashboard content; the guard is unchanged.

**Independent Test**: probe every route unauthenticated; each returns 302 (page) or 401 (API/SSE); foreign Host/Origin still refused.

### Tests for User Story 2

- [ ] T011 [P] [US2] Failing test: with OIDC enabled, /api/* and GET /events unauthenticated → 401 JSON, page routes → 302 to /oidc/login, over the full route inventory (tests/unit/test_497_oidc_boundary.py)
- [ ] T012 [P] [US2] Failing test: guard semantics unchanged with OIDC on — foreign Host on reads and foreign Origin on mutations refused, including on /oidc/callback (tests/unit/test_497_oidc_boundary.py)

### Implementation for User Story 2

- [ ] T013 [US2] Implement redirect-vs-401 split by route family and the no-network-I/O authenticated path (in-memory session lookup only) in src/coordinare/dashboard_auth.py

**Checkpoint**: boundary verified by enumeration, not inspection.

## Phase 5: User Story 3 — Headless use keeps working unchanged (Priority: P2)

**Goal**: token path byte-identical to spec 143; coexistence works.

**Independent Test**: full spec 143 suite green, unmodified, token-only config.

### Tests for User Story 3

- [ ] T014 [P] [US3] Test: with both token and OIDC configured, token-authenticated requests are accepted with no session, and 401 without either (tests/unit/test_497_oidc_token_coexistence.py)

### Implementation for User Story 3

- [ ] T015 [US3] Confirm/adjust middleware ordering so a valid Bearer/Basic token short-circuits OIDC handling in src/coordinare/dashboard_auth.py; run the spec 143 suite unchanged and record the result

**Checkpoint**: token-only and token+OIDC both correct.

## Phase 6: User Story 4 — A session ends when it should (Priority: P2)

**Goal**: logout, bounded lifetime, restart ends sessions.

**Independent Test**: logout then next request unauthenticated; aged session refused; restart (fresh store) refuses old cookies.

### Tests for User Story 4

- [ ] T016 [P] [US4] Failing test: POST /oidc/logout removes the server-side session and clears the cookie; next use is unauthenticated (tests/unit/test_497_oidc_session.py)
- [ ] T017 [P] [US4] Failing test: a session past session_hours is refused and evicted; a pre-restart session is refused after a fresh store (tests/unit/test_497_oidc_session.py)

### Implementation for User Story 4

- [ ] T018 [US4] Register POST /oidc/logout (204, clear cookie) and wire expiry checks into lookups in src/coordinare/dashboard/routers/oidc.py and src/coordinare/dashboard_oidc.py

## Phase 7: User Story 5 — The client secret stays secret (Priority: P2)

**Goal**: secret redaction extended to the new credential.

**Independent Test**: distinctive secret value; grep every response/log/diagnostic for it.

### Tests for User Story 5

- [ ] T019 [US5] Failing test: config API responses and `coordinare config validate` output never contain the secret marker; provider exchange and error paths never log it (tests/unit/test_497_oidc_secrets.py)

### Implementation for User Story 5

- [ ] T020 [US5] Extend the existing redaction set for the OIDC fields in src/coordinare/dashboard/routers/config.py (or wherever the config-response filter lives) and audit log call sites in src/coordinare/dashboard_oidc.py

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T021 Update docs/security/threat-model.md with the OIDC principal model (identity without roles, full operator authority, token coexistence, in-memory sessions, no-PKCE residual risk) — spec 144 FR-012 duty
- [ ] T022 [P] Document the Helm chart env expansion for the client secret (config.example.yaml values block; Service stays ClusterIP)
- [ ] T023 Run make lint, make test (full unit tree) and the quickstart.md validation steps; fix findings
- [ ] T024 Verify spec 143 suite passes unmodified (SC-003) and record the evidence in the PR body

## Dependencies & Execution Order

- Phase 1 → Phase 2 → stories in priority order (US1 → US2 → US3 → US4 → US5).
- T003 (test double) blocks all story tests; T004/T005 block T009.
- US2–US5 tests can be written in parallel once T003 lands; implementations share the
  middleware (T013 before T018 refinements).
- Polish last: T021–T024 depend on all stories.

## Parallel Opportunities

- T001/T002, T006–T008, T011/T012, T014, T016/T017 are independent test files.
- The test double (T003) and session store (T004) are separable files.

## Implementation Strategy

MVP = Phases 1–4 (config, double, store, flow, boundary): that alone makes the dashboard an
SSO app. Phases 5–7 harden the edges the issue explicitly calls out; none may be skipped —
US3 and US5 are acceptance criteria of the issue, not garnish.
