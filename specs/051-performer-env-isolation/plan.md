# Implementation Plan: Performer Environment Isolation

**Branch**: `051-performer-env-isolation` | **Date**: 2026-04-21 | **Spec**: specs/051-performer-env-isolation/spec.md

## Summary

The performer subprocess currently inherits the host's full environment via `dict(os.environ)`. This causes commits attributed to the host user and leaks host credentials. This plan adds `bot_identity` and `env_passthrough` config fields and changes `workspace.py._make_git_env()` and `subprocess_transport.py._start()` to construct explicit, minimal environments instead of inheriting the host env.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: pydantic-settings (existing), asyncio (stdlib), os (stdlib)
**Storage**: N/A
**Testing**: pytest (existing)
**Target Platform**: Linux server (coordinare daemon)
**Performance Goals**: No perf impact — env construction is O(1)
**Constraints**: Must not break existing PAT-only setups; must preserve PATH/HOME for tools to work

## Constitution Check

| Gate | Status | Notes |
|------|--------|-------|
| Lint & Format | ✓ PASS | ruff enforced |
| Type Check | ✓ PASS | All new fields typed |
| Unit Tests | ✓ PASS | New tests for env construction |
| Coverage | ✓ PASS | env construction paths covered |
| No dead code | ✓ PASS | |

## Project Structure

```text
src/coordinare/
├── config.py                          # Add BotIdentityConfig + env_passthrough to CoordinareConfig
├── workspace.py                       # _make_git_env() uses minimal env + bot identity
└── transport/subprocess_transport.py  # _start() constructs explicit env, not dict(os.environ)

tests/unit/
├── test_workspace.py                  # Test git env includes GIT_AUTHOR/COMMITTER vars
└── test_subprocess_transport.py       # Test subprocess env doesn't include arbitrary host vars
```

**Structure Decision**: Single project, all changes in existing files.

## Implementation Notes

### 1. New config in `config.py`

```python
class BotIdentityConfig(BaseModel):
    name: str = "Coordinare Bot"
    email: str = "coordinare@localhost"

# In CoordinareConfig:
bot_identity: BotIdentityConfig = Field(default_factory=BotIdentityConfig)
env_passthrough: list[str] = Field(default_factory=list)
```

### 2. `workspace.py._make_git_env()` — minimal env with identity

Replace `env = dict(os.environ)` with:
```python
env: dict[str, str] = {}
for key in ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL"):
    if key in os.environ:
        env[key] = os.environ[key]
env["GIT_TERMINAL_PROMPT"] = "0"
# Bot identity (WorkspaceManager stores config ref)
identity = getattr(self._config, "bot_identity", None) if hasattr(self, "_config") else None
name = (identity.name if identity else None) or "Coordinare Bot"
email = (identity.email if identity else None) or "coordinare@localhost"
env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name
env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = email
for var in list(getattr(self._config, "env_passthrough", None) or []):
    if var in os.environ:
        env[var] = os.environ[var]
```

WorkspaceManager already holds `_config` — verify and add it if missing.

### 3. `subprocess_transport.py._start()` — explicit env

Add a `_build_subprocess_env()` helper that constructs a clean env dict with PATH/HOME, `GITHUB_TOKEN` (from an explicit PAT only — never from the host env; App-mode tokens are delivered via the dispatch payload), GIT_AUTHOR/COMMITTER identity, and configured pass-through vars. Pass `env=self._build_subprocess_env()` to `create_subprocess_exec`. Log a DEBUG event `subprocess_transport.env_constructed` listing var names (not values).

SubprocessTransport constructor must accept and store a `config` reference.

## Complexity Tracking

No violations.
