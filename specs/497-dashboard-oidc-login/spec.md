# Feature Specification: Dashboard OIDC login for remote operator access

**Feature Branch**: `497-dashboard-oidc-login`
**Created**: 2026-09-29
**Status**: Draft
**Issue**: [#498](https://github.com/ViviDynamics/coordinare/issues/498)
**Input**: Add OIDC login to the web dashboard so remote access can ride the estate SSO
(Authentik) the way Grafana, Argo CD and Steward already do. Authorization Code flow against a
configured provider; post-exchange session auth covering fetch and EventSource; logout; bounded
session lifetime; shared token path retained as headless fallback; Host/Origin and DNS-rebinding
guard retained; Helm Service stays ClusterIP.

## Context

Spec 143 shipped a shared operator token as the dashboard's boundary and deliberately stopped
short of identity: no user store, no roles, and FR-005's answer to SSO was "a reverse proxy may
perform SSO, then inject the operator Bearer token". That proxy pattern works, but every browser
session is indistinguishable, and it puts SSO wiring outside the app forever.

The cluster deployment (spec 159) exposes the dashboard at coordinare.vividynamics.com through the
cluster tunnel, gated at the edge by Cloudflare Access with an email one-time PIN. That edge gate
is the stopgap. Native OIDC against Authentik (auth.vividynamics.com) makes the dashboard a
first-class SSO app on the estate and lets the access model converge with Grafana, Argo CD and
Steward instead of relying on a Cloudflare-specific control.

Existing state, verified: dashboard auth is the single shared token (`dashboard_auth.py`),
constant-time verified on every route including GET and SSE, with the signed webhook exempt; no
OIDC or authorization-code handling exists anywhere in the codebase; the dashboard ships as a
Helm Service with no Ingress.

## Clarifications

Settled before design (solo run against issue #498; each is the issue's stated rule or the
simplest reading of it):

- **Principal authority (issue item 3)**: an authenticated OIDC principal holds **full operator
  authority**, consistent with spec 143's no-roles stance. OIDC introduces *identity* (who is
  asking, displayed in the session), not *authorization* (what they may do). Everyone who can
  authenticate is an operator. This decision is recorded here and in the spec 144 trust-boundary
  document, per the issue's "record it in the spec, not in code comments".
- **Coexistence of the two auth paths**: token and OIDC MAY be configured together. The token
  stays the headless path (curl, scripts, webhook-style automation); OIDC serves browsers. A
  request is authenticated if it satisfies either. Removing the edge gate (Cloudflare Access) is
  an operator decision after cutover, not part of this feature.
- **Unauthenticated responses**: a browser-facing page load receives a **redirect** to the
  provider; API and SSE routes receive **401** (fetch and EventSource must not silently follow a
  redirect into an HTML login). Both outcomes satisfy the issue's "provider redirect or 401,
  never dashboard content".
- **Session store**: in-memory on the daemon. A daemon restart ends all sessions and users
  re-authenticate. This matches spec 143's simplicity ("rotation requires a daemon restart") and
  avoids a second persistence mechanism.
- **Session lifetime**: bounded and configurable, default 12 hours from issue, sliding not
  required — fixed from authentication.
- **PKCE**: not used. The client is confidential (server-side secret); the issue's acceptance
  requires state and nonce validation, which are implemented. Recording this as an accepted
  residual risk (authorization-code interception is mitigated by TLS and the secret, not PKCE).
- **Dependencies**: discovery, token exchange and ID-token verification ride the existing
  `httpx` and `PyJWT[crypto]`; no new dependency is introduced (constitution: minimal
  dependencies).

## User Scenarios & Testing *(mandatory)*

### User Story 1 — An operator logs in through the estate SSO (Priority: P1)

An operator opens the dashboard's public URL. Because they hold no session, the dashboard
redirects them to the estate identity provider, where they authenticate with their usual SSO
credentials. The provider returns them to the dashboard, and the dashboard behaves as a fully
authorised operator console for the rest of the session.

**Why this priority**: this is the feature. Everything else exists to keep it from becoming the
boundary's weakest point.

**Independent Test**: with OIDC configured and no session, loading the dashboard page lands on
the provider's login, and completing it returns to a working dashboard.

**Acceptance Scenarios**:

1. **Given** OIDC configured and no session, **When** the dashboard page is requested, **Then**
   the response redirects to the configured provider's authorization endpoint, not dashboard
   content.
2. **Given** the provider redirects back with an authorization code, **When** the dashboard
   exchanges it, **Then** the exchange validates state and nonce, and a valid exchange
   establishes a session that authenticates subsequent requests.
3. **Given** a tampered or stale state or nonce on the callback, **When** the exchange is
   attempted, **Then** it is refused, no session is created, and the refusal is logged with a
   reason.

---

### User Story 2 — The boundary holds for every route (Priority: P1)

Unauthenticated requests to any route — the HTML page, API reads, API mutations, the SSE
stream — receive the provider redirect or 401, never dashboard content. The Host/Origin and
DNS-rebinding guard continues to apply across the OIDC round trips.

**Why this priority**: an authentication feature that leaks one unauthenticated route is worse
than none, because it looks like spec 143's coverage while it is not.

**Independent Test**: with OIDC configured, every route is probed without a session and each
returns a redirect or 401; the guard still rejects a foreign Host on reads and a foreign Origin
on mutations.

**Acceptance Scenarios**:

1. **Given** no session and no token, **When** any dashboard route is requested, **Then** the
   response is the provider redirect (browser page loads) or 401 (API/SSE), never dashboard
   content.
2. **Given** the OIDC routes now exist on the dashboard host, **When** the guard evaluates
   them, **Then** Host protection applies as it does to every other route, and the round trip
   from provider back to dashboard is not blocked by the guard.
3. **Given** a session established through OIDC, **When** a mutating request arrives with a
   foreign Origin, **Then** it is refused exactly as it is today (spec 144's guard is unchanged
   by authentication).

---

### User Story 3 — Headless use keeps working unchanged (Priority: P2)

A script or automation authenticates with the shared operator token exactly as it does today.
The existing spec 143 suite passes unmodified against a token-only configuration.

**Why this priority**: the token path is the deployed boundary today; breaking it to add OIDC
would trade a working control for a new one.

**Independent Test**: the full spec 143 test suite runs unchanged and green on a configuration
with a token and no OIDC.

**Acceptance Scenarios**:

1. **Given** a token-only configuration, **When** the dashboard is exercised, **Then** behaviour
   is byte-identical to today: Bearer or Basic on every route, constant-time verification, 401
   otherwise, signed-webhook exemption intact.
2. **Given** both token and OIDC configured, **When** a token-authenticated request arrives,
   **Then** it is accepted without any session.

---

### User Story 4 — A session ends when it should (Priority: P2)

A logged-in operator can log out. A session that sits unused does not live forever. A daemon
restart ends every session.

**Why this priority**: bounded lifetime and logout are what make "identity" mean anything at all
on a shared machine.

**Independent Test**: log in, log out, confirm the next request is unauthenticated; with a
shortened lifetime, confirm an aged session is refused.

**Acceptance Scenarios**:

1. **Given** an authenticated session, **When** logout is invoked, **Then** the session stops
   authenticating requests and the browser session material is cleared.
2. **Given** a session older than the configured lifetime, **When** it is presented, **Then** it
   is refused and the next request is treated as unauthenticated.
3. **Given** a daemon restart, **When** a pre-restart session is presented, **Then** it is
   refused.

---

### User Story 5 — The client secret stays secret (Priority: P1)

The OIDC client secret is configured via environment expansion or a Secret. It never appears in
logs, never in API config responses, and never in validation diagnostics — the same redaction
posture spec 143 gives the operator token, extended to the new credential.

**Why this priority**: a leaked SSO client secret is an attack on the whole estate, not just
coordinare.

**Independent Test**: configure OIDC with a distinctive secret value, exercise config reads,
validation output and error paths, and grep every emitted byte for the value.

**Acceptance Scenarios**:

1. **Given** OIDC configured with a client secret, **When** config API responses and validation
   diagnostics are produced, **Then** the secret value appears in neither.
2. **Given** any log line the dashboard or daemon emits, **When** the secret or received
   provider messages are logged, **Then** the secret value is never present.

---

### Edge Cases

- **The provider is unreachable at login time**: the redirect or exchange fails with a
  diagnostic, no partial session is created, and retrying the page load re-attempts login.
- **The exchange fails validation** (bad state, bad nonce, wrong issuer/audience): refused, no
  session, logged reason.
- **The callback arrives with the guard's Host mismatched**: refused by the guard before any
  OIDC handling, as with every other route.
- **A token-authenticated request and a session-bearing request are equivalent in authority**:
  neither is "more" authenticated; both are full operator.
- **OIDC configured with a non-loopback bind but the token absent**: valid; OIDC is a
  sufficient boundary. The spec 143 refusal fires only when neither path is configured.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard MUST accept an OIDC configuration block: discovery URL, client
  identifier, client secret (environment expansion or Secret), and redirect URL on the dashboard
  host. When the block is present, browser login follows the Authorization Code flow against the
  provider's discovery document.
- **FR-002**: Unauthenticated requests to the dashboard page MUST receive a redirect to the
  provider; unauthenticated API and SSE requests MUST receive 401. No route may return
  dashboard content unauthenticated.
- **FR-003**: The authorization-code exchange MUST validate state and nonce against values the
  dashboard issued, and MUST verify the ID token's signature, issuer and audience before any
  session is created.
- **FR-004**: Post-exchange authentication MUST be a session that covers fetch and EventSource
  the same way browser Basic does today, carried by an HttpOnly, SameSite=Lax cookie that is
  Secure whenever the request is.
- **FR-005**: The dashboard MUST offer logout, clearing the session server-side and the cookie
  client-side.
- **FR-006**: Session lifetime MUST be bounded and configurable (default 12 hours), and the
  session store MUST be in-memory: a daemon restart ends all sessions.
- **FR-007**: The shared operator token path MUST be unchanged. A token-valid request MUST
  authenticate without a session, token and OIDC MAY coexist, and the existing spec 143 suite
  MUST pass unmodified under a token-only configuration.
- **FR-008**: The Host/Origin and DNS-rebinding guard MUST be retained unchanged across the
  OIDC round trips, applying to the new routes as it does to every route (spec 144's guard
  semantics, including the exact-path signed-webhook exemption, are unchanged).
- **FR-009**: The OIDC client secret MUST never be logged and never appear in API config
  responses or validation diagnostics (extends spec 143 FR-006 to the new credential).
- **FR-010**: An authenticated OIDC principal MUST hold full operator authority. No user store,
  roles or per-principal authorization are introduced; the session records the principal's
  subject for display only.
- **FR-011**: The Helm Service MUST remain ClusterIP with no Ingress; the dashboard's public
  placement remains the cluster tunnel plus the operator's own edge gate, documented as today.
- **FR-012**: The trust-boundary documentation (spec 144's document) MUST be updated to
  describe the new principal model: OIDC identity, full-operator authority, token coexistence,
  and the session boundary.

### Key Entities *(include if feature involves data)*

- **OIDC configuration block**: discovery URL, client identifier, client secret, redirect URL,
  session lifetime. Present or absent; no partial states.
- **Session**: the post-exchange authentication material — an opaque server-side record keyed by
  a cookie value, carrying the principal's subject, an absolute expiry, and nothing else.
- **Principal**: the authenticated identity. Full operator authority; no roles.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every dashboard route probed without credentials returns the provider redirect or
  401 — dashboard content is never served unauthenticated — verified by test over the full route
  set, not by inspection.
- **SC-002**: A login round trip completes against a live conformant provider (or its faithful
  test double) with state and nonce validation exercised, including one tampered-state refusal.
- **SC-003**: The existing spec 143 suite passes unchanged and green under a token-only
  configuration.
- **SC-004**: Grep over every log emission, config API response and validation diagnostic
  produced during the test run finds zero occurrences of the configured secret value.
- **SC-005**: An aged session, a logged-out session and a pre-restart session are each refused
  on their next use.
- **SC-006**: The trust-boundary document names the OIDC principal model, and its claims about
  the new paths are covered by tests the way spec 144 FR-026 requires.

## Assumptions

- Authentik at auth.vividynamics.com is the first target provider, but nothing in the feature
  may require it specifically: discovery-driven OIDC against a conformant provider is the
  contract. The validated provider for CI is a test double.
- The dashboard host serving the redirect URL is trusted to be the same host the browser
  reached; the redirect URL is derived from or validated against the dashboard's configured
  public host.
- Sessions need no persistence across restarts. Operators who restart frequently can raise the
  lifetime; re-authenticating through SSO is cheap by construction.
- The edge gate (Cloudflare Access) may remain in place during and after cutover. Removing it is
  an operator decision outside this feature.

## Out of Scope

- Roles, per-user authorization, or any user store beyond the identity the provider asserts.
- PKCE, refresh tokens, and token-base access-token validation at the API layer.
- Persisting sessions across daemon restarts.
- Removing or automating the Cloudflare Access edge gate.
- Changes to the reverse-proxy token-injection pattern of spec 143 FR-005, which remains
  supported as documented.
