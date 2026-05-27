# Phase 1 Data Model

This feature adds no persistent storage. The "model" is two new config fields and one doc artifact.

## Config Entities

### `Settings.LITELLM_PROXY_BASE_URL`

- **Type**: `str`
- **Default**: `""` (empty string — treated as unset per `env_ignore_empty=True`)
- **Source**: env var `LITELLM_PROXY_BASE_URL` or performer config file
- **Validation**: When non-empty, MUST be a parseable HTTP(S) URL. Validation happens at first use (env-injection site), not at config-load time, to keep `Settings` cheap to construct and to match the existing pattern for optional URL-like settings.
- **Semantics**: Mapped to `ANTHROPIC_BASE_URL` in the claude CLI subprocess env when set AND active backend is `claude_code`.

### `Settings.LITELLM_PROXY_AUTH_TOKEN`

- **Type**: `str` (secret — see Research Decision 3)
- **Default**: `""`
- **Source**: env var `LITELLM_PROXY_AUTH_TOKEN` or performer config file
- **Validation**: Non-empty when `LITELLM_PROXY_BASE_URL` is set (warned at injection site; turn proceeds and lets the proxy surface auth-failure per FR-007 / Acceptance Scenario 1.3).
- **Semantics**: Mapped to `ANTHROPIC_AUTH_TOKEN` in the claude CLI subprocess env when both values are set. MUST NOT appear in logs, CI artifacts, or persisted card state (FR-004, SC-004).

## State Transitions

Trivial — both values are read-only at process startup (env-overridable per the existing `BaseSettings` pattern). No runtime mutation.

| Configured? | Active backend | Effect on subprocess env |
|-------------|---------------|--------------------------|
| Neither set | any           | No injection (FR-003)    |
| Only URL set | `claude_code` | No injection; warn at injection site (auth missing)  |
| Only token set | `claude_code` | No injection (URL is the gating value) |
| Both set | `claude_code`     | Inject `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` |
| Both set | other backend     | No injection (FR-005)    |

## Documentation Entity: Validated Model Matrix

Lives in `docs/operators/litellm-proxy.md` (not code). Schema:

| Column | Description |
|--------|-------------|
| Model name | Name the claude CLI sends in its `model` field |
| Downstream provider | What the LiteLLM proxy routes that name to (OpenAI, Anthropic direct, local, etc.) |
| Date validated | YYYY-MM-DD |
| Card scenario | Brief description of the card the model completed end-to-end |

Initial seed row TBD by /speckit.tasks (one validated non-Anthropic model required by SC-003).
