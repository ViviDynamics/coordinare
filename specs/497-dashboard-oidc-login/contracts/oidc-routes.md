# Contract: dashboard OIDC routes

All routes are served by the dashboard application on the dashboard host. The localhost
guard applies unchanged (Host check on reads, Origin check on mutations). The auth
middleware exempts these paths by exact name; they carry their own flow integrity via
state and nonce.

## GET /oidc/login

Begins the Authorization Code flow.

**Request**: any. **Auth**: none (this is the login).

**Response 302** → provider authorization endpoint with
`response_type=code`, `client_id`, `redirect_uri`, `scope=openid`,
`state`, `nonce`. Sets two short-lived HttpOnly cookies carrying state and nonce.

**Response 502** when the provider discovery document is unreachable or invalid; body is a
diagnostic that names the failure and contains no secret.

## GET /oidc/callback

Completes the flow.

**Request**: query parameters `code`, `state` (both required).

**Response 302 → `/`** on success: exchange performed server-side against the provider's
token endpoint; ID token verified (signature via JWKS, issuer, audience, nonce); session
created; HttpOnly session cookie set; state/nonce cookies cleared.

**Response 403** when state mismatches, nonce mismatches, the ID token fails verification,
or the exchange fails. Body is a diagnostic; no session is created.

## POST /oidc/logout

**Response 204**: server-side session removed, session cookie cleared. Safe to call when
already logged out.

## Auth-middleware behaviour (context for the contract above)

Given OIDC enabled and no valid session or token on a request:

- `GET /` and other page routes → `302 /oidc/login`
- `/api/*`, `GET /events` → `401` JSON `{"detail": "Dashboard authentication required"}`
- signed webhook path → unchanged HMAC-verified handling

Token-authenticated requests behave exactly as spec 143 defines, at any point in any flow.
