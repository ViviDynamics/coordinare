# Plan — 552 stdio local mode

Issue #79. Decisions the implementation makes, and why.

## Shape

The `mcp` SDK's `MCPServer` exposes `run_stdio_async()` beside
`streamable_http_app()`, so both transports share one assembled server:

```
__main__.py (transport dispatch)
    ├─ http:  build_asgi_app(config)  → BearerAuthMiddleware(streamable_http_app)  [phase 1]
    └─ stdio: run_stdio(config)       → MCPServer.run_stdio_async()                [new]
                     │
                     └─ _build_mcp_server(config)   [shared: registry + audit]
```

## Decisions

- **Widen, don't fork, the config.** `transport: Literal["http", "stdio"]`.
  `from_env` already reads `COORDINARE_MCP_TRANSPORT`; no new fields. The port
  validator keeps validating (it guards HTTP mode; stdio ignores the field).
- **Reject listener flags in stdio mode** (exit 2) rather than ignore: a
  silently ignored `--listen-port` looks like a listening server.
- **No bearer gate in stdio.** The stream is inherited from the operator's
  shell; anyone who can write to it is already the operator. Authentication
  happens at the shell, not in the pipe.
- **Caller fingerprint in stdio mode** = `credential_fingerprint(agent_token)`:
  the audit line identifies which agent token the process runs with, and the
  `mcp.tool_invoked` vocabulary needs no transport-specific branches.
- **Registry untouched.** Tools, annotations, and the dashboard client are
  phase 1 code paths; the transport seam is only in `server.py`/`__main__.py`.

## Tasks

- [ ] 1. Config: accept `transport="stdio"` — test that
      `McpServerConfig(transport="stdio", ...)` validates and `from_env` round-trips it.
- [ ] 2. Dispatch: `--transport stdio` runs `run_stdio(config)` — test that the
      stdio runner is invoked and `main()` returns 0; test that
      `--listen-host`/`--listen-port` in stdio mode exit 2.
- [ ] 3. Server: `run_stdio(config)` builds the same 16-tool server without the
      bearer gate — test the stdio session serves the registry (in-process) and
      that the caller fingerprint is the agent-token fingerprint.
- [ ] 4. Docs: `docs/mcp.md` stdio section; quickstart note in the spec dir.
