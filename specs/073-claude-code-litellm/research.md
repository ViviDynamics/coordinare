# Phase 0 Research: Claude Code Backend via LiteLLM Proxy

## Decision 1: Env-var names the claude CLI honors for proxy redirection

**Decision**: Use `ANTHROPIC_BASE_URL` (proxy URL) and `ANTHROPIC_AUTH_TOKEN` (bearer credential).

**Rationale**: The claude CLI (Claude Code) reads `ANTHROPIC_BASE_URL` to override the default `https://api.anthropic.com` endpoint, and `ANTHROPIC_AUTH_TOKEN` as the bearer credential sent to that endpoint. LiteLLM's OpenAI-compatible Anthropic-pass-through accepts the same bearer pattern. This is the documented Claude Code mechanism for self-hosted/proxy deployments — no CLI flag changes required (satisfies FR-002).

**Alternatives considered**:
- `ANTHROPIC_API_KEY` override → rejected: that var is also used to talk directly to Anthropic and is special-cased in performer secret-handling; conflating the two would break operators who run mixed deployments (Edge Case: "Mixed deployments").
- Custom env vars (`LITELLM_*`) consumed by the claude CLI → rejected: CLI does not read those; would require a CLI fork.
- CLI flags (`--base-url`) → rejected: out of scope per FR-002 ("no claude-CLI command-line flag changes required").

## Decision 2: Where to thread the env vars into the subprocess

**Decision**: Add an internal helper that returns a `dict[str, str]` of proxy env vars (empty when unset), and merge it into the existing `env=` kwarg at `agent/performer/src/performer/backends/claude_code.py:180`:

```python
env={**os.environ, **self._cache_env, **self._git_env, **self._tool_env, **self._proxy_env}
```

**Rationale**: The merge site already exists and already inherits `os.environ`. Adding one more dict preserves the precedence chain and keeps the change one line at the call site plus a small helper. The helper returns `{}` when the proxy URL is unset, so FR-003 (no injection when unset) is enforced structurally, not by branching at the call site.

**Alternatives considered**:
- Set vars in `os.environ` at performer startup → rejected: pollutes the parent process and leaks across backend switches in long-running performer processes; also leaks the secret into any child process the performer spawns for non-LLM reasons.
- Pass via CLI args → rejected, see Decision 1.

## Decision 3: Secret handling for the proxy auth token

**Decision**: Store the token in `Settings` as a plain `str` field with a default of `""`. Apply the same logging-redaction pattern already in use for `ANTHROPIC_API_KEY` (the performer never logs `Settings` directly; structured-log emit sites whitelist non-secret fields). Add `LITELLM_PROXY_AUTH_TOKEN` to the existing secret-redaction allowlist used by structlog processors (or equivalent).

**Rationale**: Matches existing convention (FR-004 explicitly says "Existing secret-handling conventions for ANTHROPIC_API_KEY apply"). Avoids introducing `pydantic.SecretStr`, which would require unwrapping at the env-injection site and is not used elsewhere in the codebase.

**Alternatives considered**:
- `pydantic.SecretStr` → rejected: not used in existing `Settings`; introducing it just for this field is inconsistency-for-its-own-sake.
- Loading from a file path → rejected: out of scope; operators can use env-var injection from their existing secret store.

## Decision 4: Scoping config to `claude_code` only

**Decision**: The env-injection helper is called only from `claude_code.py`. Other backend modules never read the LiteLLM settings. The settings themselves remain global on `Settings` but are silently unused by other backends.

**Rationale**: Satisfies FR-005 (no effect on other backends, no startup error). Localizes the integration to one file and keeps the config schema simple (no per-backend nested settings).

**Alternatives considered**:
- Per-backend nested config (`Settings.claude_code.litellm_proxy_*`) → rejected: would require restructuring the existing flat `Settings` class for one feature.
- Runtime backend-active check inside a shared helper → rejected: unnecessary — only `claude_code.py` imports the helper.

## Decision 5: Validated Model Matrix location and format

**Decision**: Ship as `docs/operators/litellm-proxy.md` with a "Validated Models" section that is a Markdown table: `| Model name (claude CLI side) | Downstream provider | Date validated | Card scenario |`.

**Rationale**: Satisfies FR-006 + SC-003. Lives in the existing docs surface (Assumption #4 in spec). Plain Markdown table keeps it grep-able and PR-reviewable.

**Alternatives considered**:
- YAML manifest checked into code → rejected: implies machine-readability that the spec does not require; out of scope.
- External wiki → rejected per Assumption #4.

## Open Questions

None. All FRs and SCs have a concrete implementation path.
