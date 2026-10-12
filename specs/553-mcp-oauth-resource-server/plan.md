# Plan: MCP OAuth 2.1 resource server (phase 3)

## Approach

Three layers, each independently testable, in dependency order:

1. **Config** (`src/coordinare/mcp/config.py`): `McpOAuthConfig` pydantic model,
   default-off, nested on `McpServerConfig.oauth`, `from_env` round-trip.
2. **Verifier** (`src/coordinare/mcp/oauth.py`): `IntrospectionTokenVerifier`
   implementing the SDK `TokenVerifier` protocol against an RFC 7662 endpoint
   via `httpx.AsyncClient`, fail-closed, structured logs, token-text redaction.
3. **Assembly** (`src/coordinare/mcp/server.py`): `build_asgi_app` branches on
   `config.oauth.enabled` — SDK `RequireAuthMiddleware` +
   `BearerAuthBackend(verifier, resource_server_url)` around the existing
   `MCPServer` app, plus a thin per-request fingerprint middleware. Static path
   untouched.

## Design decisions

- **Introspection over local JWT validation.** The issue names "token
  validation against an authorization server"; RFC 7662 needs no key
  management, no JWKS rotation, and works with any OAuth 2.1 AS. Local JWT
  validation is a future optimization (spec FR: non-goals).
- **SDK bearer stack, not hand-rolled.** `BearerAuthBackend` +
  `RequireAuthMiddleware` produce the correct 401/`WWW-Authenticate` behavior
  and RFC 8707 resource checks; we only implement `verify_token`.
- **Fail closed.** Any introspection failure returns None → 401. Distinguishing
  "AS says inactive" (debug) from "AS unreachable" (error, event
  `mcp_oauth.introspection_failed`) keeps operator signal high without leaking
  token text.
- **All-or-nothing with the static token.** When `oauth.enabled`, the static
  agent token is ignored (FR5). This avoids a confused-deputy surface; the
  dashboard token boundary is untouched.
- **No per-process verifier cache.** Per-request introspection keeps revocation
  semantics exact; a bounded TTL cache is future work behind the same
  `TokenVerifier` interface.
- **Fingerprint = hash of the presented token**, set per request by our thin
  middleware inside the SDK auth stack (only authenticated requests reach it),
  matching the static-mode audit shape (FR4).

## Test strategy

- Config: validation + env round-trip (mirror `test_552_mcp_stdio_config.py`).
- Verifier: `httpx.MockTransport`-backed AS responses — active, inactive,
  non-2xx, malformed, timeout/network → fail-closed matrix; assert token text
  never appears in logged output; assert client credentials sent per RFC 7662.
- Assembly: `build_asgi_app` with OAuth config over an in-process ASGI client —
  401 without/with bad token (challenge header present), 200-style success with
  a mocked verifier, fingerprint contextvar set from the presented token,
  static token rejected when OAuth enabled.
- Compatibility: run the 551/552 MCP test files with no OAuth config — they
  must pass unchanged (SC1/SC4).

## Tasks

1. `McpOAuthConfig` (FR1) — red→green.
2. `IntrospectionTokenVerifier` (FR2) — red→green.
3. ASGI assembly + fingerprint middleware (FR3, FR4, FR5) — red→green.
4. Docs (FR6) — `docs/mcp.md` OAuth section.
5. Gates: ruff + baseline + mypy + full unit suite + coverage; preflight; PR
   with `Closes #80`; CI watch; adversarial review; merge.
