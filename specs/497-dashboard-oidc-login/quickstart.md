# Quickstart: dashboard OIDC login

## Configure

In `config.yaml` (root level, as in `config.example.yaml`):

```yaml
dashboard_host: "127.0.0.1"             # or the container bind; unchanged
dashboard_port: 8090
dashboard_oidc:
  discovery_url: https://auth.vividynamics.com/.well-known/openid-configuration
  client_id: coordinare-dashboard
  client_secret: ${COORDINARE_OIDC_CLIENT_SECRET}   # export it, or mount a Secret
  redirect_url: https://coordinare.vividynamics.com/oidc/callback
  session_hours: 12
```

Notes:

- The redirect URL host must be the dashboard host or one of `trusted_dashboard_hosts` —
  the same list the tunnel operator already configures for the guard.
- Keep `dashboard_auth_token` if scripts drive the API; both paths coexist.
- The Helm Service stays ClusterIP; public reachability is the operator's tunnel and edge
  gate, unchanged.

## Validate before start

```sh
python -m coordinare config validate
```

Refusals name the offending field (scheme, empty credential, redirect host not trusted) and
never echo the secret.

## Try it

1. Start the daemon and open the dashboard's public URL.
2. Unauthenticated: you are redirected to the provider; sign in with SSO.
3. You land back on the dashboard, authenticated. The session is bounded by `session_hours`.
4. `curl -I https://…/api/state` without credentials → 401; with the operator token → 200.
5. Logout clears the session; the next page load redirects to the provider again.

## Manual verification against real Authentik (once, not in CI)

Point `discovery_url` at a scratch Authentik application, complete the round trip in a
browser, and confirm in the daemon logs that no log line contains the client secret value
(search for the literal you exported).
