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

## Configuration (env)

| Variable | Default | Notes |
|---|---|---|
| `COORDINARE_MCP_TRANSPORT` | `http` | only `http` is supported today (`stdio` arrives with 552) |
| `COORDINARE_MCP_TOKEN` | — | required; agent-facing bearer, min 32 chars |
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
  HTTP transport is the only one supported today; the future stdio transport
  (552) will have no bearer gate by design — its boundary is the local
  process spawn itself — so it should only run for same-machine agents
  already trusted with full operator privilege.
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
