# Phase 1 Quickstart: MCP management layer

Feature: `551-mcp-management-layer` | Branch: `551-mcp-management-layer` | Date: 2026-10-11

## 1. Run the server locally against a dev daemon

```sh
set -a && source .env && set +a   # provides the tokens the config expands

export COORDINARE_MCP_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export COORDINARE_MCP_DASHBOARD_TOKEN="$COORDINARE_DASHBOARD_TOKEN"   # existing operator token

.venv/bin/python -m coordinare.mcp --transport http --listen-port 8091 \
    --dashboard-url http://localhost:8090 \
    --agent-token "$COORDINARE_MCP_TOKEN" \
    --dashboard-token "$COORDINARE_MCP_DASHBOARD_TOKEN"
```

The daemon must already be running (`make run`) so the dashboard API on 8090 is up.

## 2. Point an agent at it

Claude Code (`.mcp.json` or `claude mcp add`):

```json
{
  "mcpServers": {
    "coordinare": {
      "type": "http",
      "url": "http://localhost:8091/mcp",
      "headers": { "Authorization": "Bearer <COORDINARE_MCP_TOKEN value>" }
    }
  }
}
```

OpenCode and any MCP-spec client take the same shape (streamable HTTP URL + bearer header). First tool call should be a read-only one:

```text
get_state
```

## 3. A five-minute tour

1. `get_state` — see phase, active card, sessions.
2. `get_config scope=all` — redacted config; note the `base_hash` in the result.
3. `validate_config payload=...` — validate a candidate change, nothing written.
4. `update_config` with the same `base_hash` — write; check `applied` in the result.
5. `control action=force_poll` — trigger a poll cycle, then `get_state` again.

## 4. Deployed (Helm)

```sh
# values.yaml additions (see FR7)
mcp:
  enabled: true
  agentToken: existing-secret-key-ref    # or existingSecret + key
```

```sh
helm template coordinare deploy/helm/coordinare -f values-overrides.yaml | less   # verify sidecar, Service port, Ingress /mcp
```

When `mcp.enabled`, the statefulset gains the sidecar (same image, `python -m coordinare.mcp`), the Service gains port 8091, and the Ingress (if enabled) routes `/mcp` to it.

## 5. Verify the tests

```sh
make test          # includes the new test_551_* suites
make lint && make typecheck
make test-js       # unaffected, but cheap to confirm
```

## Troubleshooting

- **401 on every call**: the agent token does not match `COORDINARE_MCP_TOKEN`; tokens are constant-time compared, so an extra whitespace fails.
- **`daemon_unreachable`**: the daemon (or its dashboard API on 8090) is down; the MCP server is up but cannot proxy. Retry `get_state` first.
- **`conflict` on `update_config`**: someone changed config since your `get_config`. Re-read and re-apply.
- **`staged_restart` in results**: the changed field is restart-required (e.g. `dashboard_port`); bounce the daemon to apply.
