# MCP management layer

Spec 551 ships a standalone MCP server (`python -m coordinare.mcp`) that proxies the
dashboard REST API, giving an external AI agent (Claude Code, OpenCode, any MCP
client) the full operator surface without a browser or shell on the daemon host.

## Architecture

```
MCP client (agent)                sidecar / process                     daemon
| MCP streamable HTTP  ------->  coordinare.mcp  --- REST (bearer) -->  dashboard :8090
  Bearer COORDINARE_MCP_TOKEN         Bearer COORDINARE_MCP_DASHBOARD_TOKEN
```

- **Proxy, not embed.** The MCP server is stateless and calls the dashboard REST
  contract the UI already exercises. It cannot corrupt daemon state directly;
  the daemon's REST validation remains the single write boundary.
- **Two tokens, two boundaries.** The agent authenticates to the MCP endpoint
  with `COORDINARE_MCP_TOKEN`. The MCP server authenticates to the dashboard
  with `COORDINARE_MCP_DASHBOARD_TOKEN`. A leaked agent credential is not a
  dashboard credential; they rotate independently.
- **16 tools, 5 domains.** Config, symphonies, personas, lifecycle, and state
  inspection — see the table below. Tool consolidation is deliberate: agents
  select better among 16 rich tools than 40 thin ones.

## Running it

Local (the daemon must already be running; `make run`):

```sh
export COORDINARE_MCP_TOKEN=<32+ chars>
export COORDINARE_MCP_DASHBOARD_TOKEN=<the dashboard bearer>
.venv/bin/python -m coordinare.mcp          # listens on :8091
```

Point the client at `http://localhost:8091/mcp` with
`Authorization: Bearer $COORDINARE_MCP_TOKEN`.

Helm: set `mcp.enabled: true` (and `mcp.port` if not the 8091 default). The
chart adds a sidecar on the same image with `COORDINARE_MCP_DASHBOARD_URL`
and `COORDINARE_MCP_HEALTH_URL` preset to pod-local loopback; both tokens come
from the same Secret the main container uses. Enabling `mcp` also adds the
`mcp` port to the Service and a `/mcp` route to the dashboard Ingress when one
is configured.

### stdio (local agents)

Spec 552 adds a stdio transport for same-machine agents: the MCP JSON-RPC
session runs over the spawned process's stdin/stdout, so there is no listener,
no bearer gate, and no port. Boot it with `--transport stdio` (or
`COORDINARE_MCP_TRANSPORT=stdio`); `--listen-host`/`--listen-port` are HTTP-only
and rejected with exit code 2. Point Claude Code or OpenCode at it:

```json
{
  "mcpServers": {
    "coordinare": {
      "command": "/path/to/venv/bin/python",
      "args": ["-m", "coordinare.mcp", "--transport", "stdio"],
      "env": {
        "COORDINARE_MCP_TOKEN": "<32+ chars>",
        "COORDINARE_MCP_DASHBOARD_TOKEN": "<the dashboard bearer>"
      }
    }
  }
}
```

The tool surface, audit lines, and error vocabulary are identical to HTTP; the
credential fingerprint in `mcp.tool_invoked` is the agent token's, since the
local pipe replaces the bearer check. The daemon must still be running and
reachable at `COORDINARE_MCP_DASHBOARD_URL` (default `http://localhost:8090`).

### OAuth resource server (HTTP)

Spec 553: instead of the static agent token, the HTTP surface can validate
bearer tokens against an external OAuth 2.1 authorization server via RFC 7662
token introspection. Opt in per deployment:

```sh
export COORDINARE_MCP_OAUTH_ENABLED=true
export COORDINARE_MCP_OAUTH_ISSUER=https://as.example.com
export COORDINARE_MCP_OAUTH_INTROSPECTION_URL=https://as.example.com/introspect
export COORDINARE_MCP_OAUTH_CLIENT_ID=coordinare-mcp
export COORDINARE_MCP_OAUTH_CLIENT_SECRET=<secret>
export COORDINARE_MCP_OAUTH_RESOURCE_SERVER_URL=https://mcp.example.com/mcp
export COORDINARE_MCP_OAUTH_REQUIRED_SCOPES=coordinare:mcp
export COORDINARE_MCP_OAUTH_TIMEOUT=5.0
```

Every request's bearer token is introspected at the authorization server
(`client_id`/`client_secret` produce the Basic-auth credentials when both are
set); inactive tokens, malformed responses, unreachable introspection endpoints,
and audience mismatches all deny with 401. Nothing is cached and nothing falls
open: an AS outage means agents wait, not that gates lift. With
`COORDINARE_MCP_OAUTH_RESOURCE_SERVER_URL` set, tokens are additionally checked
for RFC 8707 audience binding. `COORDINARE_MCP_OAUTH_REQUIRED_SCOPES` becomes a
403 (`insufficient_scope`) when the token lacks any listed scope.

The static `COORDINARE_MCP_TOKEN` is not required in this mode and is not
accepted on the wire; tokens must come from the authorization server. The audit
line gains a `caller_client_id` field (the introspected token's client id) and
the credential fingerprint is of the presented token, so per-agent activity
stays attributable. stdio ignores this block entirely: local trust stays
static-token (552 FR5).

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `COORDINARE_MCP_TRANSPORT` | `http` | `http` or `stdio` (552) |
| `COORDINARE_MCP_TOKEN` | — | required; agent-facing bearer, min 32 chars |
| `COORDINARE_MCP_OAUTH_*` | off | OAuth resource server (553); see above |
| `COORDINARE_MCP_DASHBOARD_TOKEN` | — | required; presented to the dashboard |
| `COORDINARE_MCP_HOST` | `0.0.0.0` | |
| `COORDINARE_MCP_PORT` | `8091` | must differ from 8080/8090 |
| `COORDINARE_MCP_DASHBOARD_URL` | `http://localhost:8090` | |
| `COORDINARE_MCP_HEALTH_URL` | `http://localhost:8080` | used by `get_metrics` |
| `COORDINARE_MCP_READ_TIMEOUT` | `10` s | budget for read tools |
| `COORDINARE_MCP_LIFECYCLE_TIMEOUT` | `60` s | budget for `control` and bootstrap chains |

`${VAR}` placeholders inside values are expanded the way `config.yaml` does.

## Tool surface

| Domain | Tools | Safety hints |
|---|---|---|
| config | `get_config`, `update_config`, `validate_config` | read / destructive+non-idempotent / read |
| symphonies | `list_symphonies`, `get_symphony`, `upsert_symphony`, `delete_symphony`, `validate_symphony` | read / destructive / destructive / read |
| personas | `get_personas`, `upsert_persona`, `delete_persona` | read / destructive / destructive |
| lifecycle | `control` | destructive, non-idempotent (one-shot actions) |
| state | `get_state`, `get_performer_logs`, `get_activity`, `get_metrics` | read |

`upsert_symphony` optionally chains a bootstrap step (`bootstrap: env | wiki`)
after the write, using the lifecycle budget for the chain and aborting on the
first failure. Writes that need optimistic concurrency (`update_config`) take a
`base_hash` from the latest `get_config`; a 409 surfaces as a `conflict` error
whose recovery hint is to re-read.

## Security notes

- Bearer checks are constant-time compares with a 32-character minimum. The
  stdio transport has no bearer gate by design: its boundary is the local
  process spawn itself, so only run it for same-machine agents you already
  trust with full operator privilege.
- In OAuth mode the bearer boundary is RFC 7662 introspection, checked per
  request and fail-closed: introspection failures deny the request rather than
  admit it. Token values never reach logs; only fingerprints and client ids do.
- The internal hop's token is never echoed into tool results or logs.
- Secret redaction happens in the dashboard; the MCP layer cannot and does not
  un-redact anything `get_config` returns.
- The MCP protocol's requests carry no browser `Origin`, so the dashboard's
  Origin guard does not apply; the bearer check is the boundary. Treat the
  agent token as full operator privilege.
- Every mutating tool call emits one `mcp.tool_invoked` structured-log line
  (tool, action, outcome, error kind, credential fingerprint, duration) — the
  audit trail for "who changed what".
- Errors map to a shared vocabulary (`auth_failed`, `conflict`, `validation`,
  `daemon_error`, `daemon_unreachable`, timeout) with recovery hints, uniformly
  across all tools.

## Deployment posture

The Helm sidecar shares the daemon image and pod, so it inherits the pod's
security context and needs no extra RBAC. It is ClusterIP-only by default;
publishing it (or the dashboard) through an Ingress is an operator decision the
chart supports but does not make for you. See `deploy/helm/coordinare/README.md`
for the exposure trade-offs.

The quickstart walkthrough is `specs/551-mcp-management-layer/quickstart.md`.
