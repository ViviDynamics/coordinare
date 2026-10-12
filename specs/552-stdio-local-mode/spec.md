# 552 — MCP stdio local mode (phase 2)

Parent epic: 551 (merged, PR #77). GitHub issue: #79.

## Problem

Phase 1 ships the MCP management surface only as a streamable-HTTP sidecar: an
agent must reach port 8091 over the network and hold `COORDINARE_MCP_TOKEN`.
Local tooling (an agent running on the same machine as the daemon operator)
should not need a listener, a port, or a network hop.

## Goal

`python -m coordinare.mcp --transport stdio` serves the same 16-tool registry
over stdio, in-process with the operator's terminal.

## Functional requirements

- FR1: `McpServerConfig.transport` accepts `"stdio"`; `COORDINARE_MCP_TRANSPORT=stdio`
  round-trips through `from_env()`.
- FR2: `--transport stdio` boots the server over stdio and returns 0 on clean
  shutdown. The tool registry, audit emitter, and dashboard client are the
  phase 1 components, unchanged.
- FR3: stdio mode performs no bearer check: the transport is a same-machine
  pipe inherited from the operator's shell. The HTTP mode keeps its bearer gate.
- FR4: `--listen-host` / `--listen-port` are rejected in stdio mode with exit
  code 2: they describe a listener stdio does not have, and failing fast beats
  silently ignoring a misconfiguration.
- FR5: every mutating tool call still emits exactly one `mcp.tool_invoked`
  line. In stdio mode the caller fingerprint is the sha256 fingerprint of the
  configured agent token, so the audit vocabulary is uniform across transports.
- FR6: `docs/mcp.md` documents the stdio transport, its trust model, and the
  absence of the bearer gate.

## Constraints

- No Helm, Dockerfile, or chart changes: the sidecar stays HTTP. stdio is a
  local-run surface.
- No new environment variables beyond the existing `COORDINARE_MCP_*` set.
- The lint ratchet must not increase; no suppression markers.
