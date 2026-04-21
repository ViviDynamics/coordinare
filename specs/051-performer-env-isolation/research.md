# Research: Performer Environment Isolation

## Decision: Minimal env construction, not inherit-and-strip

- **Decision**: Build a minimal `dict` from scratch, copying only whitelisted keys from `os.environ`. Do NOT start from `dict(os.environ)` and strip.
- **Rationale**: Allowlist is safer than denylist. New env vars added to the host (e.g., `AWS_SECRET_ACCESS_KEY`) are automatically excluded without needing to update a denylist. Much simpler to audit.
- **Alternatives considered**: Strip-based (copy all, remove dangerous vars) — rejected; hard to maintain, always one var behind.

## Decision: GIT_AUTHOR/COMMITTER set in both workspace and subprocess transport

- **Decision**: Set git identity vars in both `_make_git_env()` (for git clone/push operations in workspace setup) AND `_build_subprocess_env()` (for the performer subprocess itself).
- **Rationale**: The workspace setup makes git calls directly (clone, branch, configure). The performer subprocess (opencode, claude_code) also runs git commands. Both need the correct identity. One place isn't enough.

## Decision: BotIdentityConfig as nested Pydantic model

- **Decision**: `BotIdentityConfig(BaseModel)` with `name` and `email` fields, nested in `CoordinareConfig`.
- **Rationale**: Consistent with existing patterns (`PriorityConfig`, `AdvocateConfig`). YAML nesting: `bot_identity:\n  name: ...\n  email: ...`. Env var: `COORDINARE_BOT_IDENTITY__NAME=...` (pydantic-settings double-underscore nesting).
- **Alternatives considered**: Flat fields `bot_name` / `bot_email` — simpler but doesn't scale if we add more identity fields later (e.g., GPG key).

## Decision: env_passthrough as list[str] on CoordinareConfig

- **Decision**: `env_passthrough: list[str] = []` — explicit list of var names to copy from host env to subprocess env.
- **Rationale**: Common need for API keys (ANTHROPIC_API_KEY, OPENCODE_MODEL, etc.) without opening the entire host env. Operator has full control over what passes through.
- **Alternatives considered**: Regex pattern matching — more flexible but harder to reason about security boundaries.

## Affected files

1. `src/coordinare/config.py` — new `BotIdentityConfig`, new fields on `CoordinareConfig`
2. `src/coordinare/workspace.py` — `_make_git_env()` rewrite
3. `src/coordinare/transport/subprocess_transport.py` — `_start()` + `_build_subprocess_env()`
4. `tests/unit/test_workspace.py` — new tests
5. `tests/unit/test_subprocess_transport.py` — new tests
