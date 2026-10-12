# Feature Specification: MCP OAuth 2.1 resource server (phase 3)

**Feature Branch**: `553-mcp-oauth-resource-server`
**Status**: Complete
**Input**: GitHub issue #80 (MCP management layer epic, phases 551/552 merged)

## Summary

Make the HTTP MCP server an OAuth 2.1 resource server: agent-authorization
moves from a single static bearer token to access tokens issued by an
authorization server, validated via RFC 7662 token introspection. The static
token mode stays as the default; OAuth mode is opt-in per deployment.

## Problem

Phase 1's agent boundary is one static bearer token (`COORDINARE_MCP_TOKEN`).
Every agent shares one credential: rotating it evicts all agents, offboarding
one agent means rotating for all, and revocation is all-or-nothing. An OAuth
resource server gives per-agent identities, per-agent revocation, and scoped
access without changing the tool surface.

## Glossary

- **Resource server**: the MCP server; validates bearer access tokens and
  serves protected resources (the 16 tools).
- **Authorization server (AS)**: issues the access tokens; validated here via
  RFC 7662 introspection.
- **Static-token mode**: phase 1 behavior, unchanged and still the default.

## Requirements

### FR1 — Configuration (opt-in, default off)

A `McpOAuthConfig` block under `McpServerConfig.oauth` (or equivalent config
surface) controls the mode. Default state is `enabled: false` with no issuer;
the server boots in static-token mode byte-identically to spec 552 when the
block is absent. Enabling requires an issuer URL and an explicit introspection
endpoint URL (discovery via `<issuer>/.well-known/openid-configuration` is
deliberately not implemented: the introspection URL is deploy-time
configuration, and requiring it explicitly fails at boot rather than per
request). Settings include: introspection client credentials (client_id /
client_secret, RFC 7662 section 2.3), RFC 8707 resource-server URL, required
scopes, and a read timeout. All settings round-trip through the `from_env`
pattern (`COORDINARE_MCP_OAUTH_*`).

### FR2 — Token validation via RFC 7662 introspection

The server validates presented bearer tokens by calling the AS's introspection
endpoint with the configured client credentials. Only `active: true` responses
validate; every other outcome (network error, non-2xx, malformed payload,
timeout) fails closed: the request is unauthenticated, and a structured log
line records the failure kind without the token text. `active: false` denies
without an error log (expected outcome for expired/revoked tokens, one debug-
level line). The validated principal maps to the SDK `AccessToken`
(`client_id`, `scopes`, `expires_at`, `subject`, `claims`).

### FR3 — SDK-first integration

Token validation rides the installed `mcp` SDK's resource-server pieces
(`TokenVerifier` protocol, `BearerAuthBackend`, `RequireAuthMiddleware`) with
an in-repo `TokenVerifier` implementation; no hand-rolled 401/`WWW-Authenticate`
plumbing. When `resource_server_url` is configured, the SDK's
`validate_token_resource` behavior enforces the RFC 8707 audience check; when
absent, the verifier confirms the token's audience is the configured one.

### FR4 — Per-request audit fingerprint and identity

Each authenticated request's `mcp.tool_invoked` audit line carries a
fingerprint derived from the presented access token (same sha256-prefix shape
as static mode), and the principal identity (`client_id`) is available to the
audit context, so per-agent attribution works with rotating tokens. The token
text itself never appears in logs or tool results.

### FR5 — Static-token coexistence is all-or-nothing

When OAuth mode is enabled, the static `COORDINARE_MCP_TOKEN` agent boundary
is disabled (the dashboard token boundary is untouched, per the 551 trust
model). A deployment runs either static-token mode or OAuth mode; no
hybrid/ad-hoc fallback exists to avoid a confused-deputy surface.

### FR6 — Documentation

`docs/mcp.md` documents OAuth mode: env settings, AS expectations
(introspection, scopes, RFC 8707), the fingerprint/identity model, and the
failure-closed behavior. Helm and Dockerfile are unchanged.

## Non-goals

- Token issuance, registration flows, or the AS itself; the MCP server is a
  resource server only (the SDK's authorize/token routes are not mounted).
- JWT local validation (RFC 9068) — introspection only in this phase.
- Introspection result caching — per-request introspection in v1; a bounded
  TTL cache is future work.
- stdio transport — local trust is unchanged (552).

## Success criteria

- SC1: With `enabled: false` (default), every existing test passes unchanged
  and the boot path is identical to 552.
- SC2: With OAuth enabled, a token rejected by the AS yields 401 with a
  `WWW-Authenticate` challenge produced by the SDK stack; an `active` token
  authenticates and the audit line carries the token-derived fingerprint.
- SC3: Any introspection outage fails closed (401), never open.
- SC4: Static-token-mode tests remain green with OAuth config absent.
