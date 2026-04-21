# Data Model: Performer Environment Isolation

## Config Changes

### BotIdentityConfig (new, src/coordinare/config.py)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | `"Coordinare Bot"` | Git author/committer name for performer commits |
| `email` | `str` | `"coordinare@localhost"` | Git author/committer email for performer commits |

### CoordinareConfig additions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `bot_identity` | `BotIdentityConfig` | see above | Git identity for all performer commits |
| `env_passthrough` | `list[str]` | `[]` | Host env var names to pass through to performer subprocess |

### Config YAML example

```yaml
bot_identity:
  name: coordinare-bot
  email: coordinare-bot@myorg.com

env_passthrough:
  - ANTHROPIC_API_KEY
  - OPENCODE_MODEL
```

### Env vars (pydantic-settings double-underscore nesting)

```
COORDINARE_BOT_IDENTITY__NAME=coordinare-bot
COORDINARE_BOT_IDENTITY__EMAIL=coordinare-bot@myorg.com
COORDINARE_ENV_PASSTHROUGH=["ANTHROPIC_API_KEY"]
```

---

## Subprocess Environment Shape

The minimal env passed to performer subprocesses:

| Var | Source | Always Present |
|-----|--------|----------------|
| `PATH` | host env | Yes (if set) |
| `HOME` | host env | Yes (if set) |
| `TMPDIR`/`TEMP`/`TMP` | host env | If set |
| `LANG`/`LC_ALL` | host env | If set |
| `GITHUB_TOKEN` | explicit PAT token only; App-mode tokens are delivered via the `dispatch_card` protocol payload | If explicit token provided |
| `GIT_AUTHOR_NAME` | `bot_identity.name` | Yes |
| `GIT_AUTHOR_EMAIL` | `bot_identity.email` | Yes |
| `GIT_COMMITTER_NAME` | `bot_identity.name` | Yes |
| `GIT_COMMITTER_EMAIL` | `bot_identity.email` | Yes |
| `GIT_TERMINAL_PROMPT` | hardcoded `"0"` | Yes |
| `<env_passthrough[i]>` | host env | If key set on host |

---

## State Changes

None. This is a subprocess launch configuration change only.
