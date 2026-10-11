# Phase 0 Research: MCP management layer

Feature: `551-mcp-management-layer` | Branch: `551-mcp-management-layer` | Date: 2026-10-11

## R1: MCP SDK choice

**Decision**: Official `mcp` Python SDK (modelcontextprotocol), currently 2.3.0 on PyPI.

**Rationale**: Verified on PyPI 2026-10-11: `mcp 2.3.0` requires Python >= 3.10 (repo is 3.12+), is built on pydantic >= 2.12, and ships `pyjwt[crypto]`, starlette, and uvicorn, which the repo already carries for the dashboard and OIDC verification. Provides both transports we need (streamable HTTP, stdio) plus tool annotations. The repo already uses PyJWT for ID-token verification (`dashboard_oidc.py`), so the crypto path is familiar.

**Alternatives considered**:
- Hand-rolled JSON-RPC server: reimplements the protocol; rejected.
- FastMCP standalone package: overlaps the SDK's own FastMCP support; adds a second dependency for no capability gain.
- No SDK, direct SSE plumbing: maximum control, maximum maintenance; rejected.

**Notes for implementation**: the SDK's dependency set brings its own pinned transitives; add `mcp` to pyproject and re-run `make typecheck` early. The SDK ships type hints; verify `py.typed` behavior under the strict mypy 433 ratchet and, if the SDK has gaps, use the existing per-module allowlist in `pyproject.toml` rather than a global exemption.

## R2: Transport strategy

**Decision**: Streamable HTTP in phase 1 (this spec), stdio in phase 2 (`specs/552-mcp-stdio-local/`). Both transports run off the same tool registry and REST client; the transport is a startup flag, not a code fork.

**Rationale**: The daemon's Helm topology already has one pod with the daemon and (with this feature) one sidecar. Streamable HTTP is the only transport that works for a remote agent against a cluster deployment. stdio serves the future same-host mode with zero additional protocol code.

**Alternatives considered**: in-daemon MCP routes (rejected in brainstorming: crash-domain coupling, blocks stdio); SSE-only legacy MCP transport (deprecated in the MCP spec).

## R3: Auth model, phase 1

**Decision**: The MCP server requires a dedicated bearer token (`COORDINARE_MCP_TOKEN`) on every request, constant-time compared. It holds the dashboard operator token (`COORDINARE_MCP_DASHBOARD_TOKEN`) as its own environment secret for the internal REST hop. Phase 3 (`specs/553-mcp-oauth21/`) upgrades to the MCP-spec OAuth 2.1 resource-server model against the existing OIDC provider; bearer mode remains as a config flag fallback.

**Rationale**: Two distinct secrets mean a leaked agent credential cannot be replayed against the dashboard directly, and the agent token rotates without touching the dashboard. This matches the existing threat model (all-or-nothing operator authority) without new crypto. Constant-time compare follows the dashboard's own token check pattern (`dashboard_auth.py`).

**Alternatives considered**: shared single token (rejected: blast radius); OAuth 2.1 immediately (rejected: coding agents are the phase-1 client and cannot drive interactive browser flows; phased per user decision).

## R4: Deployment shape

**Decision**: Sidecar container in the existing Helm statefulset pod, same image as the daemon (`Dockerfile.daemon`), different command (`python -m coordinare.mcp`). Sidecar port 8091 (dashboard 8090 and health 8080 are taken). The ClusterIP Service gains an MCP port; Ingress routes `/mcp` to it. Ingress stays off by default per the existing unauthenticated-dashboard warning.

**Rationale**: `localhost:8090` reachability inside the pod avoids new NetworkPolicy paths and new TLS between containers. Single replica (already enforced) means no split-brain. One image keeps release pipelines (Docker, Chart jobs) unchanged in shape.

**Alternatives considered**: separate Deployment + Service (two images, more RBAC/policy surface, version-skew risk); MCP server hitting the health port (wrong server: `/metrics` lives there but the management API does not).

## R5: Tool surface consolidation

**Decision**: 16 tools in five domains (config, symphonies, personas, lifecycle, state inspection) with `action` enums where REST has sibling endpoints; MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`) on every tool. Registry completeness is asserted in tests against an explicit defer-list.

**Rationale**: Full operator parity is required by the spec (US1-US6), but 40+ one-to-one tools degrade agent tool-selection. Consolidation keeps the mapping to REST mechanical and testable while keeping tool count low.

**Alternatives considered**: one tool per endpoint (rejected: noisy); fewer domains with mega-tools (rejected: hides destructive semantics inside a single tool's annotations).

## R6: REST client

**Decision**: `client.py` is a thin async httpx client, per-call connections, bounded timeouts (10 s reads / 60 s lifecycle, config-overridable), no retry logic (errors map to structured tool results; retries are the agent's decision).

**Rationale**: httpx is already a repo dependency. Stateless per-call connections make the sidecar restart-safe by construction (SC4). Retrying mutating REST calls automatically risks double-execution (e.g. duplicate `control` actions); the existing dispatch-guard work (spec 076) demonstrates why duplicate dispatches are harmful.

**Alternatives considered**: connection pooling with health checks (stateful complexity for marginal gain at this scale); automatic retry with idempotency keys (the dashboard REST has no idempotency-key contract).

## R7: Audit logging

**Decision**: One structured-log line per mutating tool call, event `mcp.tool_invoked`, keys: tool, action, result status, client credential fingerprint (phase 1) / `sub` (phase 3). Read-only calls are not logged individually (they are visible in the existing dashboard request log).

**Rationale**: Follows the repo's structlog vocabulary (`snake_case.dot.separated`) so operator grep recipes stay uniform. A fingerprint rather than the token itself avoids logging secrets.

## R8: Unresolved items

None. All `NEEDS CLARIFICATION` items from the brainstorming session were resolved interactively with the operator (client type: remote-first with phased local; auth: bearer first, OAuth 2.1 phase 3; scope: full operator surface; approach: REST-proxy architecture) and are recorded in the spec.
