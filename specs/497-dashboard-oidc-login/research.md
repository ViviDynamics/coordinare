# Research

## D1: OIDC client implementation — library vs hand-rolled

**Decision**: hand-roll the flow on `httpx` + `PyJWT[crypto]`.

**Rationale**: the Authorization Code flow is three HTTP calls and one JWT verification.
Coordinare already depends on both libraries; `PyJWT`'s `PyJWKClient` fetches a provider
JWKS and verifies signatures, issuer and audience. A dedicated OIDC library (authlib,
mozilla-django-oidc) would add a runtime dependency for code we would still have to wire
into a Starlette middleware ourselves.

**Alternatives considered**: authlib (full-featured, but its client is request-library-bound
and its middleware expectations do not match our single-middleware design); python-jose
(weaker maintenance record than PyJWT).

## D2: Where state and nonce live across the round trip

**Decision**: short-lived signed cookies set on `/oidc/login`, read and consumed on
`/oidc/callback`.

**Rationale**: the dashboard has no server-side storage requirement between redirect and
callback (sessions are in-memory but the state must survive process restarts of the *browser*
session, not the daemon). Cookies scoped to the OIDC paths avoid touching the operator's
existing dashboard cookies. State is random per login; nonce is random per login and checked
against the ID-token claim on exchange.

**Alternatives considered**: server-side pending-login table (loses nothing to cookie
tampering but adds state that must be bounded and cleaned; the cookie approach is stateless
across daemon restarts, which is strictly better).

## D3: Session cookie name and shape

**Decision**: one opaque HttpOnly cookie (default name `coordinare_session`), SameSite=Lax,
Secure when the request is TLS, Max-Set-At-Authentication plus server-side absolute expiry.

**Rationale**: SameSite=Lax is required for the provider round trip (the callback is a
top-level navigation). Opaque value only — no identity claims in the browser. The server-side
record carries the subject for display and the absolute expiry; the cookie itself carries no
expiry beyond the browser session so the server remains authoritative.

## D4: Middleware composition

**Decision**: extend `DashboardAuthentication` rather than adding a second middleware.

**Rationale**: auth must remain a single outermost decision point so the route set is covered
uniformly; two middlewares would need their own precedence rules, and the spec 144 guard sits
below with semantics that must not change. The middleware's exemption set grows from
`{webhook}` to `{webhook, oidc routes}` by exact path.

**Alternatives considered**: FastAPI dependencies per-route (per-route opt-in is exactly what
spec 144 FR-012 rejects for the guard; the same argument applies to auth).

## D5: Redirect target for page loads vs API

**Decision**: `GET /` and other browser-facing page loads receive `302 → /oidc/login` when
unauthenticated; `/api/*` and `/events` receive 401 JSON, matching spec 143's error shape.

**Rationale**: fetch and EventSource would follow a redirect into HTML and fail confusingly;
an explicit 401 lets the dashboard's existing fetch error path prompt a reload. The distinction
is by route family, not by `Accept` header sniffing.

## D6: Provider test double for tests

**Decision**: a minimal ASGI app serving discovery, token and JWKS endpoints, driven through
httpx's mock transport in unit tests; no live network dependency in CI.

**Rationale**: deterministic, offline, and exercises our actual client code path including
key rotation semantics (the double serves a fixed signing key). A live Authentik round trip
remains a manual verification step in quickstart.md, not an automated test.
