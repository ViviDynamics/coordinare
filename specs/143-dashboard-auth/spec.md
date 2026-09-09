# 143: Dashboard authentication

Issue #197. Preserve the unauthenticated loopback default. When a shared operator
token is configured, authenticate every dashboard route, including GET and SSE,
using Bearer tokens or browser-native HTTP Basic (username `operator`, token as
password). Everyone holding the token has full operator authority; no user store
or roles are introduced. The signed webhook retains its existing HMAC boundary.

## Requirements and decisions

- FR-001: Default token is absent; local behavior is unchanged. Refuse non-loopback
  dashboard binds without a configured token before bootstrapping services.
- FR-002: Require constant-time token verification on every route when enabled.
  Reject absent, malformed and wrong credentials with 401; never log the token.
  Browser-native Basic authentication also authenticates EventSource/fetch requests.
- FR-003: Keep Host/Origin protection for DNS rebinding and CSRF. Auth does not
  bypass this guard. Authenticated mutation requests with a foreign Origin fail.
- FR-004: Only the configured signed webhook POST is exempt from dashboard auth;
  webhook HMAC validation remains mandatory in its existing handler.
- FR-005: Configure tokens by environment expansion/Secret. Reverse proxies may
  perform SSO then inject an operator Bearer token after removing inbound auth.
  Document TLS, proxy trust and full operator privileges; keep Helm ClusterIP.
- FR-006: Token minimum length is 32 characters; no token values in API config
  responses or validation diagnostics. Rotation requires a daemon restart.

## Acceptance

Tests cover every mutating route with missing/incorrect auth and foreign Origin,
GET/SSE auth, browser Basic, Bearer, malformed headers, signed webhook exemption,
default local behavior, non-loopback startup refusal and secret redaction.
Existing dashboard latency gates must pass; authentication does no network I/O.
