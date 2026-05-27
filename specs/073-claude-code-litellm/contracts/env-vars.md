# Contract: Performer → claude CLI subprocess env

This feature adds no HTTP/RPC API. The only contract is the set of environment variables the performer injects into the claude CLI subprocess.

## Field Registry

| Field | Direction | Type | Required | Set when | Notes |
|-------|-----------|------|----------|----------|-------|
| `ANTHROPIC_BASE_URL` | performer → claude CLI | string (URL) | optional | `Settings.LITELLM_PROXY_BASE_URL` non-empty AND active backend is `claude_code` | Overrides the CLI's default `https://api.anthropic.com`. Sourced from `Settings.LITELLM_PROXY_BASE_URL`. |
| `ANTHROPIC_AUTH_TOKEN` | performer → claude CLI | string (bearer) | optional | `Settings.LITELLM_PROXY_AUTH_TOKEN` non-empty AND active backend is `claude_code` AND `ANTHROPIC_BASE_URL` set | Bearer credential the proxy expects. Sourced from `Settings.LITELLM_PROXY_AUTH_TOKEN`. SECRET — never logged. |

## Invariants

1. **No-injection baseline (FR-003, SC-002)**: When `LITELLM_PROXY_BASE_URL` is empty, the subprocess env dict MUST be byte-identical to the pre-feature baseline. No empty-string or sentinel values introduced.
2. **Backend scoping (FR-005)**: When the active backend is not `claude_code`, neither env var is set in the subprocess regardless of `Settings` values.
3. **Secret hygiene (FR-004)**: `ANTHROPIC_AUTH_TOKEN` MUST NOT appear in performer stdout, stderr, structured logs, CI artifacts, or persisted card state.
4. **Precedence**: When set, the proxy env vars override any values inherited from `os.environ` (i.e., they win in the dict merge).
