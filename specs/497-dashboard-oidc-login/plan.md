# Plan

OIDC Authorization Code flow for the dashboard, layered onto the existing spec 143
middleware rather than beside it. `DashboardAuthentication` (src/coordinare/dashboard_auth.py)
stays the single outermost auth middleware and gains an optional OIDC mode: when a
`DashboardOidcConfig` is present, an unauthenticated request that is not carrying a valid
operator token either redirects to the provider (browser page loads) or receives 401
(API and SSE). The token path is untouched, so spec 143 behaviour is a strict subset of the
new behaviour and its suite passes unchanged.

## Technical Context

**Language/Version**: Python 3.12 (FastAPI/Starlette ASGI middleware, pydantic v2 config).
**Primary Dependencies**: existing `httpx` (discovery + token exchange), `PyJWT[crypto]`
(ID-token verification against the provider's JWKS). No new dependency.
**Storage**: none new. Sessions are an in-memory dict on the daemon process; a restart ends
them (spec decision).
**Testing**: pytest; the provider is a local ASGI test double (httpx MockTransport for the
client side, a stub ASGI app for the provider side) so no test touches a real network.
**Target Platform**: the daemon's dashboard server (uvicorn), reached directly on loopback or
through the estate tunnel; the Helm Service stays ClusterIP.

## Constitution Check

- Minimal dependencies: zero new runtime dependencies. PASS.
- Testing discipline: every FR maps to unit tests; provider interactions are mocked/faked
  (deterministic). PASS.
- Type safety: pydantic model for the config block; full annotations. PASS.
- Code review gate 8: solo lane substitutes the adversarial review pass recorded in the PR
  body (work-issue-speckit solo configuration). NOTED, accepted for solo mode.

## Project Structure

```text
src/coordinare/config.py                  # DashboardOidcConfig model + ProjectConfiguration field
src/coordinare/dashboard_oidc.py          # provider integration: discovery, redirect, exchange,
                                         # ID-token verification, in-memory session store
src/coordinare/dashboard_auth.py          # middleware gains OIDC mode (token OR session OR redirect/401)
src/coordinare/dashboard/routers/         # login/callback/logout routes registered on the app
src/coordinare/__main__.py                # wiring + load-time validation of the OIDC block
config.example.yaml                      # commented oidc block (disabled by default)
docs/security/threat-model.md            # principal model update (spec 144 FR-012 duty)
tests/unit/test_497_dashboard_oidc*.py   # FR-by-FR coverage
```

## Design notes

- **Config** (`DashboardOidcConfig`): `discovery_url`, `client_id`, `client_secret`
  (SecretStr), `redirect_url`, `session_hours` (default 12). Environment expansion is what
  config.yaml already does at load time; the secret never re-enters any response or
  diagnostic. Validation at load: https discovery URL, non-empty credentials, redirect URL
  host must be the dashboard host or an existing trusted dashboard host.
- **Routes**: `GET /oidc/login` (builds the provider redirect with state+nonce cookies),
  `GET /oidc/callback` (exchange, verify, set session cookie, redirect to `/`),
  `POST /oidc/logout` (drop session, clear cookie). These are exempt from the auth
  middleware the way the signed webhook is (exact-path exemptions), but the localhost guard
  is NOT exempted for them — spec 144 semantics apply unchanged.
- **Sessions**: opaque random cookie value → (subject, absolute expiry). HttpOnly,
  SameSite=Lax (the redirect round trip needs it), Secure when the request is.
- **Latency**: authenticated request paths perform no network I/O — session lookup and token
  comparison are in-memory, preserving spec 143's latency posture. All provider I/O happens
  on the unauthenticated login/callback routes only.
- **Threat model**: `docs/security/threat-model.md` gains the OIDC principal model: identity
  without roles, full operator authority, token coexistence, in-memory sessions, and the
  accepted no-PKCE residual risk.

See research.md for the decisions behind the flow details, data-model.md for entities, and
contracts/oidc.md for the route contract.
