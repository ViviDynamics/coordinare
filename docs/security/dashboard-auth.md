# Dashboard authentication

Coordinare keeps unauthenticated loopback access as its default. A non-loopback
`dashboard_host` requires a shared operator token. When enabled, all dashboard
pages, API routes and SSE streams require authentication. The health server is
separate; keep its metrics within your operational network.

Generate a token with `openssl rand -hex 32`, save it as
`COORDINARE_DASHBOARD_AUTH_TOKEN` in your uncommitted `.env` or deployment Secret,
and reference it in configuration:

```yaml
dashboard_host: 127.0.0.1
dashboard_auth_token: ${COORDINARE_DASHBOARD_AUTH_TOKEN}
```

Restart Coordinare. The browser prompts for username **operator** and the token as
its password. Browser-native authentication covers fetch requests and EventSource
without storing credentials in dashboard JavaScript. API clients may supply
`Authorization: Bearer <token>`. No token is accepted in a URL/query parameter.
Tokens must contain at least 32 characters. Rotate the token and restart to revoke
previous credentials; browsers may prompt again or need their saved credentials
updated. Every token holder has full operator privileges, including configuration
changes that select commands and images. There are no per-user roles.

Host and Origin checks remain active after authentication. A token does not permit
a foreign website to mutate the dashboard. The configured GitHub webhook POST
continues using its separate HMAC signature instead of dashboard credentials.

## Remote access and SSO

Use TLS before sending credentials across a network. For a reverse proxy, configure
an exact `trusted_dashboard_hosts` entry matching the public hostname and preserve
that Host header. Forward the original scheme as `X-Forwarded-Proto: https` and preserve Host.
Set Uvicorn's `FORWARDED_ALLOW_IPS` environment variable to the exact proxy IPs or
restricted proxy CIDRs (for example `10.42.0.8,10.42.1.0/24`). In Kubernetes, put
that variable into the configured Secret/environment alongside the dashboard
token and restrict backend traffic to those ingress sources. Do not use `*`.
Uvicorn otherwise trusts only `127.0.0.1`, so a remote proxy's HTTPS scheme is
ignored and legitimate HTTPS-origin mutations fail the Origin check. Coordinare
does not trust arbitrary `X-Forwarded-Host` values.

The proxy may perform your organization's SSO and inject
`Authorization: Bearer <operator-token>` on the internal request. Strip any inbound
Authorization header before injecting your own, restrict backend network access to
the proxy, and use a Secret to supply the token. Anyone accepted by this proxy gets
full operator access. Do not use an unverified username header as authentication.

Docker deployments binding `0.0.0.0` must pass the token through the container's
environment and reference it in config. Helm references
`${COORDINARE_DASHBOARD_AUTH_TOKEN}` from its configured Secret automatically and
keeps the dashboard Service as ClusterIP; ingress stays opt-in.
