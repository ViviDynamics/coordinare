# Tasks: MCP OAuth 2.1 resource server (phase 3)

## 1. OAuth configuration (FR1)

- [x] RED: `tests/unit/test_553_mcp_oauth_config.py` — `McpServerConfig` with
  no `oauth` key boots static mode (`oauth.enabled is False`); a full OAuth
  block validates; `from_env` round-trips `COORDINARE_MCP_OAUTH_*` variables;
  `enabled: true` without `issuer` fails validation.
- [x] GREEN: `McpOAuthConfig` on `McpServerConfig` (nested, default instance),
  env round-trip wired.

## 2. Introspection verifier (FR2)

- [x] RED: `tests/unit/test_553_mcp_oauth_verifier.py` — MockTransport matrix:
  active→AccessToken mapping; inactive→None (debug, no error log); non-2xx /
  malformed / transport error / timeout → None + `mcp_oauth.introspection_failed`
  error log; RFC 7662 request shape (POST form, client credentials); token text
  absent from all logged output.
- [x] GREEN: `src/coordinare/mcp/oauth.py::IntrospectionTokenVerifier`.

## 3. ASGI assembly (FR3, FR4, FR5)

- [x] RED: `tests/unit/test_553_mcp_oauth_asgi.py` — with OAuth enabled:
  unauthenticated request → 401 + `WWW-Authenticate` challenge (SDK stack);
  valid-token request (mocked verifier) → tool result; `_caller_fingerprint`
  equals the presented token's fingerprint; `client_id` reaches the audit
  context; static `COORDINARE_MCP_TOKEN` is rejected when OAuth is on.
  With OAuth absent: 551/552 MCP suites unchanged.
- [x] GREEN: branch in `build_asgi_app`; thin fingerprint middleware inside the
  SDK auth stack.

## 4. Documentation (FR6)

- [x] `docs/mcp.md`: OAuth mode section — env table rows, AS expectations
  (introspection, scopes, RFC 8707), failure-closed behavior, all-or-nothing
  static-token rule; keep stdio section's "no bearer gate" phrasing accurate
  (stdio is out of scope for OAuth).

## 5. Ship

- [ ] Gates: ruff, baseline ratchet, mypy, full unit suite, coverage ≥ 90%.
- [ ] Preflight pass; branch `553-mcp-oauth-resource-server`; PR `Closes #80`
  with verification summary and quality-gate changes (none expected).
- [ ] CI green; adversarial review over the full diff; re-verify every claim;
  resolve findings; merge.
