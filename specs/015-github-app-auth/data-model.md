# Data Model: GitHub App Auth, Polling Config, Webhook Config

**Branch**: `015-github-app-auth` | **Date**: 2026-03-12 | **Phase**: 1

---

## Entities

### GitHubAuth (Protocol)

Abstract interface that hides all credential lifecycle detail from callers.

| Field / Method | Type | Description |
|---|---|---|
| `get_token()` | `async () -> str` | Returns a current, valid GitHub bearer token. Blocks if a refresh is needed. |

**Implementations**: `PatAuth`, `AppAuth`

---

### PatAuth

Satisfies `GitHubAuth` using a static personal access token. No refresh logic.

| Field | Type | Constraints |
|---|---|---|
| `_token` | `SecretStr` | Non-empty; must not be an unresolved `${...}` placeholder |

**Behaviour**:
- `get_token()` returns `_token.get_secret_value()` immediately.
- No background tasks.

---

### AppAuth

Satisfies `GitHubAuth` by managing JWT generation, installation token acquisition, and automatic refresh.

| Field | Type | Constraints |
|---|---|---|
| `_app_id` | `int` | Positive integer |
| `_private_key_pem` | `bytes` | Valid RSA PEM key, readable at construction time |
| `_installation_id` | `int` | Positive integer |
| `_cached_token` | `str \| None` | Internal; None before first acquisition |
| `_token_expires_at` | `datetime \| None` | UTC datetime; None before first acquisition |
| `_lock` | `asyncio.Lock` | Serializes concurrent refresh attempts |

**Behaviour**:
- `get_token()` checks if `_cached_token` is present and `_token_expires_at - now > 5 minutes`. If so, returns the cached token.
- Otherwise, acquires `_lock`, re-checks (double-checked locking), generates a new JWT (`iss`, `iat`, `exp`) signed with `RS256`, POSTs to `/app/installations/{id}/access_tokens`, caches the new token and its expiry, releases the lock.
- Startup: the private key file is read once at construction; if the file does not exist or is unreadable, construction raises `ValueError` with an actionable message.

**State transitions**:
```
uninitialized  →  [get_token called]  →  fetching  →  ready (cached)
ready          →  [expires_at - now < 5min]  →  fetching  →  ready (refreshed)
fetching       →  [HTTP error]  →  raises TransientGitHubError (caller retries)
```

---

### PollingConfig

Controls the daemon's polling cadence.

| Field | Type | Default | Constraints |
|---|---|---|---|
| `interval_seconds` | `int` | `30` | `ge=0`, `le=3600`; `0` = polling disabled |

**Behaviour**: When `interval_seconds == 0`, no timer-based cycle is scheduled. The daemon loop blocks on `_webhook_trigger` exclusively.

---

### WebhookConfig

Controls the optional webhook endpoint.

| Field | Type | Default | Constraints |
|---|---|---|---|
| `enabled` | `bool` | `False` | — |
| `secret` | `SecretStr \| None` | `None` | Required when `enabled=True` |
| `path` | `str` | `"/webhook/github"` | Must start with `/` |

**Behaviour**: When `enabled=False`, no endpoint is registered. When `enabled=True`, the FastAPI dashboard app mounts a POST route at `path` that validates `X-Hub-Signature-256` before setting `_webhook_trigger`.

---

### ProjectConfiguration (changes)

The existing `ProjectConfiguration(BaseSettings)` gains new fields. Existing fields are unchanged unless noted.

#### Changed fields

| Field | Before | After | Reason |
|---|---|---|---|
| `github_token` | `SecretStr` (required) | `SecretStr \| None = None` | App mode needs no PAT |
| `poll_interval_seconds` | `Field(ge=10, le=300)` | `Field(ge=0, le=3600)` | Allow disable (`0`) and slow long-poll |

#### New fields

| Field | Type | Default | Description |
|---|---|---|---|
| `github_auth` | `Literal["pat", "app"]` | `"pat"` | Auth mode selector |
| `github_app_id` | `int \| None` | `None` | GitHub App numeric ID (app mode only) |
| `github_private_key_path` | `Path \| None` | `None` | Path to RSA PEM key file (app mode only) |
| `github_installation_id` | `int \| None` | `None` | Installation numeric ID (app mode only) |
| `webhooks` | `WebhookConfig` | `WebhookConfig()` | Webhook endpoint configuration |

#### Model validators (new / updated)

| Validator | Trigger | Rule |
|---|---|---|
| `_validate_auth_config` | `mode="after"` | If `github_auth=="pat"`: `github_token` must be non-None and non-empty; if `github_auth=="app"`: `app_id`, `private_key_path`, `installation_id` all required. |
| `_validate_retry_backoff_caps` (updated) | `mode="after"` | Guard `if self.poll_interval_seconds == 0: return self` before the `wait_max_seconds > poll` comparison. |
| `_validate_webhook_config` | `mode="after"` | If `webhooks.enabled`: `webhooks.secret` must be non-None and non-empty. |

---

## Config YAML Surface (reference)

```yaml
github_org: my-org
github_project_number: 1
human_reviewers: [alice]
project_name: my-project

# Auth mode — "pat" (default) or "app"
github_auth: app
github_token: null              # omit in app mode
github_app_id: 12345
github_private_key_path: /secrets/coordinare.pem
github_installation_id: 67890

poll_interval_seconds: 30       # 0 = polling disabled

webhooks:
  enabled: true
  secret: whsec_changeme
  path: /webhook/github
```

---

## File / Module Layout

```text
src/coordinare/
├── auth/
│   ├── __init__.py          # exports: GitHubAuth, PatAuth, AppAuth, build_auth
│   ├── protocol.py          # GitHubAuth Protocol definition
│   ├── pat.py               # PatAuth implementation
│   └── app.py               # AppAuth implementation + JWT/token logic
├── config.py                # PollingConfig, WebhookConfig added; fields updated
├── daemon.py                # _webhook_trigger event; poll-or-wait loop
├── services/
│   └── github.py            # accept GitHubAuth instead of str token
└── dashboard/
    └── app.py               # webhook endpoint registration (when enabled)

tests/unit/
├── auth/
│   ├── test_pat_auth.py
│   └── test_app_auth.py
├── config/
│   └── test_config_auth.py  # new validator coverage
└── daemon/
    └── test_daemon_webhook.py  # webhook trigger + poll=0 behaviour
```
